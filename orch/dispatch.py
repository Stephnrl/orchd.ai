"""The one place in this control plane that talks to something outside it.

Everything else here prepares request data and refuses to pretend it sent anything. This
module sends it — and it lives on the broker side, under the broker's own OS account,
because a credential that an agent container can read is a credential that makes every
approval upstream of it decorative. An agent holds a secret that proves which agent it is
and grants nothing; the broker holds what can actually change the world.

Four properties are the whole point.

**The token names where it may go.** A credential file carries an origin as well as a
secret, and the dispatcher refuses to send it anywhere else. A profile is operator-supplied
and could name any destination; binding the credential to its origin means a misdirected
profile cannot carry a live token to a host that was never meant to see it.

**The request is fenced by hash.** The caller passes the sha256 the approval was granted
over, and the dispatcher sends only a request that hashes to exactly that. What a human
approved and what left the machine are the same bytes, and the receipt names the hash so
anyone can check it afterwards.

**Redirects are never followed.** A 3xx is recorded and stops there. Following one would
hand the Authorization header to whatever the redirect named, which is the classic way a
scoped token stops being scoped.

**Refused and uncertain are different answers.** A request refused before anything was sent
definitely did not happen and is safe to prepare again. A request that timed out after the
bytes went may well have happened, and retrying it is how duplicates get made — so it
returns `uncertain`, which is what reconciliation exists to resolve.
"""
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import socket
import ssl
from urllib.parse import urlsplit

from .contracts import Rejected, canonical, digest, now, redact

VERSION = "1.0.0"
MAX_RESPONSE = 65536
MAX_TOKEN = 512
MAX_READS = 8
MAX_CAPTURE = 65536
TIMEOUT = 30
METHODS = ("POST", "PATCH", "PUT")
# The broker adds authentication. A request that carries its own is either confused about
# where the credential comes from or trying to smuggle one past this boundary.
FORBIDDEN_HEADERS = ("authorization", "cookie", "proxy-authorization")


def origin_of(url):
    """Scheme, host and port, which is the whole of what "where this goes" means."""
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise Rejected("A dispatch URL must be http or https with a host")
    if parts.username or parts.password:
        raise Rejected("A dispatch URL must not carry credentials")
    if parts.fragment:
        raise Rejected("A dispatch URL must not carry a fragment")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return parts.scheme + "://" + parts.hostname.lower() + ":" + str(port)


def loopback(origin):
    host = urlsplit(origin).hostname
    return host in ("127.0.0.1", "::1", "localhost")


def read_credential(path):
    """An origin and a token, from a small unlinked private file the broker alone reads.

    The origin is not decoration. It is what stops an operator-supplied destination from
    being handed a token that was issued for somewhere else.
    """
    path = Path(path)
    for component in (path, *path.parents):
        if component.is_symlink() or (hasattr(component, "is_junction") and component.is_junction()):
            raise Rejected("Dispatch credential path contains a link")
    if not path.is_file():
        raise Rejected("Dispatch credential file missing")
    info = path.stat()
    if info.st_nlink != 1 or info.st_size > 2048:
        raise Rejected("Dispatch credential must be a small unlinked file")
    if os.name != "nt" and info.st_mode & 0o077:
        raise Rejected("Dispatch credential must be readable only by its owner")
    try:
        text = path.read_text(encoding="ascii")
    except (UnicodeError, OSError) as exc:
        raise Rejected("Unreadable dispatch credential") from exc
    fields = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or key.strip() in fields:
            raise Rejected("Dispatch credential must be origin= and token= lines")
        fields[key.strip()] = value.strip()
    if set(fields) != {"origin", "token"}:
        raise Rejected("Dispatch credential must be origin= and token= lines")
    token = fields["token"]
    if not 1 <= len(token) <= MAX_TOKEN or not re.fullmatch(r"[\x21-\x7e]+", token):
        raise Rejected("Dispatch credential token is empty or unusable")
    origin = origin_of(fields["origin"])
    if urlsplit(origin).scheme != "https" and not loopback(origin):
        # A token over plain http to a real host is a token on the wire.
        raise Rejected("A dispatch credential origin must be https unless it is loopback")
    return {"origin": origin, "token": token}


def checked_request(request, expected_sha256, credential):
    """The exact request that may be sent, or a refusal naming why it may not."""
    if not isinstance(expected_sha256, str) or not re.fullmatch("[0-9a-f]{64}", expected_sha256):
        raise Rejected("A dispatch needs the sha256 its approval was granted over")
    if not isinstance(request, dict) or not {"method", "url", "headers"} <= set(request) or not set(request) <= {"method", "url", "headers", "body"}:
        raise Rejected("Unsupported dispatch request shape")
    # The fence: what was approved and what leaves this machine are the same bytes.
    if digest(request) != expected_sha256:
        raise Rejected("This request is not the one that was approved")
    if request["method"] not in METHODS:
        raise Rejected("A dispatch performs a write, not a " + str(request["method"]))
    if not isinstance(request["headers"], dict):
        raise Rejected("Unsupported dispatch headers")
    for name in request["headers"]:
        if not isinstance(name, str) or name.lower() in FORBIDDEN_HEADERS:
            raise Rejected("A dispatch request may not carry its own authentication")
        if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", name) or not isinstance(request["headers"][name], str):
            raise Rejected("Unsupported dispatch header")
    origin = origin_of(request["url"])
    if origin != credential["origin"]:
        # The credential says where it may go, and this is not there.
        raise Rejected("This credential is not for " + origin)
    encoded = canonical(request).decode()
    if redact(encoded) != encoded:
        raise Rejected("A dispatch request must not contain a recognized secret")
    return origin


def _json(raw):
    """Parsed strictly: no duplicate keys, no NaN or Infinity, UTF-8 only."""
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise Rejected("Duplicate key in a fetched response")
            value[key] = item
        return value

    def invalid(_):
        raise Rejected("Non-JSON number in a fetched response")

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=invalid)
    except (ValueError, UnicodeError) as exc:
        raise Rejected("A fetched response was not JSON") from exc


def checked_reads(requests, credential):
    """The reads a plan asks for, each one a GET to the origin this credential is for."""
    if not isinstance(requests, dict) or not 1 <= len(requests) <= MAX_READS:
        raise Rejected("A read plan asks for 1 to " + str(MAX_READS) + " reads")
    for name, read in requests.items():
        if not isinstance(name, str) or not re.fullmatch(r"[a-z_]{1,64}", name):
            raise Rejected("Unsupported read name")
        if not isinstance(read, dict) or set(read) - {"method", "url", "headers"} or "url" not in read:
            raise Rejected("Unsupported read shape")
        if read.get("method", "GET") != "GET":
            # Reads have no effect, which is the only reason this route needs no approval.
            # A write smuggled in here would have one.
            raise Rejected("A fetch reads; it does not " + str(read.get("method")))
        headers = read.get("headers", {})
        if not isinstance(headers, dict):
            raise Rejected("Unsupported read headers")
        for header in headers:
            if not isinstance(header, str) or header.lower() in FORBIDDEN_HEADERS:
                raise Rejected("A read may not carry its own authentication")
        if origin_of(read["url"]) != credential["origin"]:
            raise Rejected("This credential is not for " + origin_of(read["url"]))
    return requests


def fetch(plan_sha256, requests, credential):
    """Perform a read plan and return the capture its codec expects.

    Reads are why this exists at all: every codec here prepares a plan naming exactly what
    must be observed, and until something could perform one, a preview needed a person to
    fetch each URL by hand and paste the JSON back. Nothing is approved here because nothing
    is changed; the fence is that only GETs, only to the credential's origin, are performed.

    The capture carries the plan hash it was asked for. The broker cannot check that hash —
    it does not hold the plan — but the codec that does will refuse a capture whose reads or
    hash do not match, so a capture cannot be passed off as answering a different question.
    """
    if not isinstance(plan_sha256, str) or not re.fullmatch("[0-9a-f]{64}", plan_sha256):
        raise Rejected("A fetch needs the sha256 of the plan it answers")
    checked_reads(requests, credential)
    responses, total = {}, 0
    for name, read in requests.items():
        parts = urlsplit(read["url"])
        headers = dict(read.get("headers", {}))
        headers["Authorization"] = "Bearer " + credential["token"]
        connection = _connect(credential["origin"])
        try:
            connection.request("GET", parts.path + ("?" + parts.query if parts.query else ""), None, headers)
            answer = connection.getresponse()
            status, raw = answer.status, answer.read(MAX_RESPONSE + 1)
        except (OSError, http.client.HTTPException) as exc:
            # A read that did not happen is simply absent; nothing was changed by trying.
            raise Rejected("Could not read " + name) from exc
        finally:
            connection.close()
        if len(raw) > MAX_RESPONSE:
            raise Rejected("The response to " + name + " exceeds the read budget")
        total += len(raw)
        if total > MAX_CAPTURE:
            raise Rejected("This plan's responses exceed the capture budget")
        responses[name] = {"url": read["url"],
                           # Never followed, so a 3xx is reported as what it is and the
                           # codec refuses it rather than reading a redirect's body.
                           "redirected": 300 <= status < 400,
                           "status": status, "body": _json(raw) if raw else None}
    return {"schema_version": VERSION, "plan_sha256": plan_sha256, "responses": responses}


def _connect(origin):
    parts = urlsplit(origin)
    if parts.scheme == "https":
        context = ssl.create_default_context()
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        return http.client.HTTPSConnection(parts.hostname, parts.port, timeout=TIMEOUT, context=context)
    return http.client.HTTPConnection(parts.hostname, parts.port, timeout=TIMEOUT)


def _receipt(operation, expected_sha256, request, origin, outcome, started, **extra):
    return {"schema_version": VERSION, "kind": "DispatchReceipt", "operation_id": operation,
            "request_sha256": expected_sha256, "origin": origin, "method": request["method"],
            "url": request["url"], "outcome": outcome, "started_at": started, "ended_at": now(),
            # Not a simulation, and it says so. Nothing else in this control plane may.
            "simulated": False, **extra}


def perform(operation, request, expected_sha256, credential):
    """Send exactly the approved request, and say honestly what happened.

    A refusal before sending raises, because nothing happened and there is nothing to
    record. Everything after the bytes go **returns a receipt**, including `uncertain`:
    `Rejected` is a `ValueError` here, and a caller catching it broadly would throw away the
    only record saying an effect may exist. `uncertain` is what reconciliation resolves, and
    retrying it blindly is how duplicates are made.
    """
    started = now()
    try:
        origin = checked_request(request, expected_sha256, credential)
    except Rejected as exc:
        raise Rejected("Dispatch refused before sending: " + str(exc)) from exc
    body = canonical(request["body"]) if "body" in request else None
    headers = dict(request["headers"])
    headers["Authorization"] = "Bearer " + credential["token"]
    if body is not None:
        headers["Content-Length"] = str(len(body))
    parts = urlsplit(request["url"])
    target = parts.path + ("?" + parts.query if parts.query else "")
    connection = _connect(origin)
    try:
        connection.request(request["method"], target, body, headers)
        response = connection.getresponse()
        status = response.status
        raw = response.read(MAX_RESPONSE + 1)
        location = response.getheader("Location", "")
    except (socket.timeout, TimeoutError):
        # The bytes went. Whether the effect happened is not knowable from here, so this is
        # returned rather than raised: `Rejected` is a ValueError, and a caller catching it
        # broadly would drop the one record saying an effect may exist.
        return _receipt(operation, expected_sha256, request, origin, "uncertain", started,
                        error="timeout", response=None)
    except (OSError, http.client.HTTPException):
        return _receipt(operation, expected_sha256, request, origin, "uncertain", started,
                        error="connection_failed", response=None)
    finally:
        connection.close()
    if len(raw) > MAX_RESPONSE:
        return _receipt(operation, expected_sha256, request, origin, "uncertain", started,
                        error="response_too_large", response={"status": status, "body": "", "sha256": None})
    # A redirect is recorded and stops. Following it would hand this token to whatever the
    # redirect named, which is how a scoped credential stops being scoped.
    outcome = "succeeded" if 200 <= status < 300 else "failed"
    text = redact(raw.decode("utf-8", "replace"))
    seen = {"status": status, "body": text, "sha256": hashlib.sha256(raw).hexdigest()}
    if 300 <= status < 400:
        try:
            elsewhere = origin_of(location) if location else None
        except Rejected:
            elsewhere = "unparseable"
        return _receipt(operation, expected_sha256, request, origin, "failed", started,
                        error="redirect_not_followed", response=seen, redirect_to=elsewhere)
    return _receipt(operation, expected_sha256, request, origin, outcome, started,
                    error=None if outcome == "succeeded" else "http_" + str(status), response=seen)
