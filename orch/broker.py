"""Single-host broker journal, fenced envelopes and the authenticated loopback service client."""
import contextlib
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import platform
import re
import secrets
import sys
import threading
import time

from .contracts import Rejected, canonical, digest, now, uid
from .process import capture
from jsonschema import Draft202012Validator
from .contracts import FORMATS

VERSION = "1.0.0"
SECRET_MAX_AGE_SECONDS = 30 * 86400   # Reported by broker-check; never enforced by refusing service.
MAX_MESSAGE = 16384        # Largest authenticated request body; matches the subprocess stdin bound.
MAX_REPLY = 1024 * 1024    # Largest authenticated reply; matches the journal size bound.
ROUTES = {"identity": ("IdentityRequest", "IdentityReply"), "execute": ("ExecuteRequest", "ExecuteReply"),
          "result": ("ExecuteRequest", "ExecuteReply"), "retire": ("RetireRequest", "RetireReply"),
          "reconcile": ("ReconcileRequest", "ReconcileReply"),
          "pilot-profile": ("PilotProfileRequest", "PilotProfileReply"),
          "pilot-execute": ("PilotExecuteRequest", "PilotExecuteReply"),
          "pilot-reconcile": ("PilotReconcileRequest", "PilotReconcileReply")}
# Pilot scopes carry the retained baseline bytes, so those routes accept larger bodies.
LIMITS = {"pilot-execute": MAX_REPLY, "pilot-reconcile": MAX_REPLY}
WIRE_SCHEMA = json.loads((Path(__file__).resolve().parents[1] / "contracts/broker-v1.schema.json").read_text())


def validate_wire(value, kind):
    schema = {**WIRE_SCHEMA, "oneOf": [{"$ref": "#/$defs/" + kind}]}
    if not Draft202012Validator(schema, format_checker=FORMATS).is_valid(value):
        raise Rejected("Invalid broker wire contract")


class BrokerUncertain(Rejected):
    pass


def safe_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
        raise Rejected("Invalid broker operation identifier")
    return value


@contextlib.contextmanager
def file_lock(path):
    if path.is_symlink():
        raise Rejected("Broker lock link")
    with path.open("a+b") as stream:
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise Rejected("Broker still running") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def atomic_json(path, data):
    if path.is_symlink():
        raise Rejected("Journal link rejected")
    temp = path.with_suffix("." + uid())
    with temp.open("xb") as stream:
        stream.write(canonical(data))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def source_digest():
    base = Path(__file__).parent
    files = {name: hashlib.sha256((base / name).read_bytes()).hexdigest()
             for name in ("broker.py", "broker_process.py", "broker_service.py", "execution.py", "process.py", "fixtures.py")}
    files["wire_schema"] = digest(WIRE_SCHEMA)
    return digest(files)


def identity():
    """The OS account this process runs as. Names are hashed so evidence never lists accounts.

    Environment variables are ignored: they are operator-controlled and prove nothing.
    """
    if os.name == "nt":
        try:
            account = os.getlogin()  # GetUserNameW: the account of the calling process.
        except OSError as exc:
            raise Rejected("Broker account identity unavailable") from exc
        uid_, gid_ = None, None
    else:
        import pwd
        uid_, gid_ = os.getuid(), os.getgid()
        try:
            account = pwd.getpwuid(uid_).pw_name
        except KeyError:
            account = "uid:" + str(uid_)
    if not isinstance(account, str) or not account:
        raise Rejected("Broker account identity unavailable")
    report = {"system": platform.system(), "account_sha256": hashlib.sha256(account.encode()).hexdigest(),
              "uid": uid_, "gid": gid_}
    validate_wire(report, "Identity")
    return report


def separate(broker, orchestrator):
    """True only when both reports name different accounts on the same kind of host."""
    validate_wire(broker, "Identity")
    validate_wire(orchestrator, "Identity")
    if broker["system"] != orchestrator["system"] or broker["account_sha256"] == orchestrator["account_sha256"]:
        return False
    if broker["uid"] is not None and orchestrator["uid"] is not None and broker["uid"] == orchestrator["uid"]:
        return False
    return True


def endpoint(value):
    match = re.fullmatch(r"127\.0\.0\.1:([0-9]{1,5})", value) if isinstance(value, str) else None
    if not match or not 1 <= int(match.group(1)) <= 65535:
        raise Rejected("Broker endpoint must be 127.0.0.1:PORT")
    return "127.0.0.1", int(match.group(1))


def read_secrets(path):
    """One or two 64-hex secrets, current first, from a small unlinked private file.

    A second line is the previous secret, accepted during a rotation overlap so that
    a process which has not yet re-read the file is not cut off mid-operation.
    """
    path = Path(path)
    for component in (path, *path.parents):
        if component.is_symlink() or (hasattr(component, "is_junction") and component.is_junction()):
            raise Rejected("Broker secret path contains a link")
    if not path.is_file():
        raise Rejected("Broker secret file missing")
    info = path.stat()
    if info.st_nlink != 1 or info.st_size > 256:
        raise Rejected("Broker secret must be a small unlinked file")
    if os.name != "nt" and info.st_mode & 0o007:
        raise Rejected("Broker secret must not be world-accessible")
    try:
        text = path.read_text(encoding="ascii")
    except (UnicodeError, OSError) as exc:
        raise Rejected("Unreadable broker secret") from exc
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not 1 <= len(lines) <= 2 or any(not re.fullmatch("[a-f0-9]{64}", line) for line in lines):
        raise Rejected("Broker secret must be one or two lines of 64 lowercase hex characters")
    if len(lines) == 2 and hmac.compare_digest(lines[0], lines[1]):
        raise Rejected("A rotation overlap requires two different secrets")
    return [bytes.fromhex(line) for line in lines], info


def load_secret(path):
    """The current secret only; callers that must survive a rotation use SecretFile."""
    return read_secrets(path)[0][0]


class SecretFile:
    """Reads the shared secret on demand, so rotation needs no restart.

    The file is re-read whenever its identity, size or modification time changes.
    A read that fails refuses the request rather than reusing a secret that the
    operator may have just withdrawn.
    """

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._stamp, self._keys, self._mtime = None, (), 0.0
        self.refresh()

    def refresh(self):
        with self._lock:
            try:
                info = self.path.stat()
            except OSError as exc:
                raise Rejected("Broker secret file missing") from exc
            stamp = (info.st_mtime_ns, info.st_size, info.st_ino)
            if stamp != self._stamp:
                keys, info = read_secrets(self.path)
                self._keys, self._stamp, self._mtime = tuple(keys), stamp, info.st_mtime
            return self._keys

    @property
    def current(self):
        """The secret to sign with. Replies are verified with the same one."""
        return self.refresh()[0]

    @property
    def accepted(self):
        """Every secret a message may be signed with, current first."""
        return self.refresh()

    def state(self, *, now_seconds=None):
        keys = self.refresh()
        age = max(0, int((now_seconds if now_seconds is not None else time.time()) - self._mtime))
        return {"accepted": len(keys), "rotating": len(keys) > 1,
                "age_seconds": age, "stale": age > SECRET_MAX_AGE_SECONDS}


def sign(key, domain, route, body):
    return hmac.new(key, b"orchd-broker-" + domain.encode() + b"\n" + route.encode() + b"\n" + body, "sha256").hexdigest()


def verify(key, domain, route, body, tag):
    return (isinstance(tag, str) and re.fullmatch("[a-f0-9]{64}", tag) is not None
            and hmac.compare_digest(sign(key, domain, route, body), tag))


def authenticate(keys, domain, route, body, tag):
    """The accepted secret this message was signed with, or None. Every key is tried."""
    matched = None
    for key in keys:
        if verify(key, domain, route, body, tag):
            matched = key
    return matched


def rotate_secret(path, *, complete=False):
    """Begin or finish a rotation by rewriting the secret file atomically and privately.

    Beginning writes a fresh current secret above the previous one, so both are
    accepted while every process re-reads the file. Completing drops the previous
    secret. Neither stage prints a secret or restarts anything.
    """
    keys, _ = read_secrets(path)
    path = Path(path)
    if complete and len(keys) == 1:
        raise Rejected("No rotation is in progress; begin one before completing it")
    if not complete and len(keys) == 2:
        raise Rejected("A rotation is already in progress; complete it before beginning another")
    lines = [keys[0].hex()] if complete else [secrets.token_hex(32), keys[0].hex()]
    temp = path.with_name(path.name + "." + uid())
    descriptor = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write("\n".join(lines) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return {"schema_version": VERSION, "kind": "BrokerSecretRotation", "path": str(path),
            "stage": "completed" if complete else "overlap", "accepted": len(lines),
            "rotating": not complete, "rotated_at": now(),
            "next_step": "Remove the previous secret with --complete once every process has re-read the file"
                         if not complete else "Rotation is complete; only the current secret is accepted"}


class ServiceClient:
    """Orchestrator side of the loopback broker service. It never reads the broker journal."""

    def __init__(self, config):
        if not isinstance(config, dict) or set(config) != {"endpoint", "secret"}:
            raise Rejected("Broker service needs exactly an endpoint and a secret file")
        self.host, self.port = endpoint(config["endpoint"])
        self.secret = SecretFile(config["secret"])

    def call(self, route, body):
        if route not in ROUTES:
            raise Rejected("Unknown broker route")
        request_kind, reply_kind = ROUTES[route]
        validate_wire(body, request_kind)
        raw = canonical(body)
        if len(raw) > LIMITS.get(route, MAX_MESSAGE):
            raise Rejected("Broker request too large")
        # Sign with the current secret and verify the reply with the same one, so a
        # rotation between request and reply cannot look like a forged answer.
        key = self.secret.current
        headers = {"Content-Type": "application/json", "X-Orch-Broker-Auth": sign(key, "request", route, raw)}
        connection = http.client.HTTPConnection(self.host, self.port, timeout=45)
        try:
            connection.request("POST", "/" + route, raw, headers)
            response = connection.getresponse()
            length = response.getheader("Content-Length", "")
            if not re.fullmatch("[0-9]{1,7}", length) or int(length) > MAX_REPLY:
                raise Rejected("Broker reply size rejected")
            data = response.read(int(length) + 1)
            if len(data) != int(length):
                raise Rejected("Incomplete broker reply")
            status, tag = response.status, response.getheader("X-Orch-Broker-Auth", "")
        except (OSError, http.client.HTTPException, ValueError) as exc:
            raise Rejected("Broker service unavailable") from exc
        finally:
            connection.close()
        if not verify(key, "reply", route, data, tag):
            raise Rejected("Broker reply authentication failed")
        if status != 200:
            raise Rejected("Broker service refused the request")
        try:
            reply = json.loads(data)
        except ValueError as exc:
            raise Rejected("Malformed broker reply") from exc
        validate_wire(reply, reply_kind)
        if reply["kind"] != body["kind"]:
            raise Rejected("Broker reply kind mismatch")
        return reply


def assess_identity(config):
    """Read-only: ask the service who it runs as and compare with this process. Never authorizes anything."""
    client = ServiceClient(config)
    nonce = uid()
    reply = client.call("identity", {"schema_version": VERSION, "kind": "identity", "nonce": nonce})
    if reply["nonce"] != nonce:
        raise Rejected("Broker identity reply nonce mismatch")
    local = identity()
    apart, same_source = separate(reply["identity"], local), reply["source_digest"] == source_digest()
    secret = reply["secret"]
    settled = not secret["rotating"] and not secret["stale"]
    status = ("source_mismatch" if not same_source else "shared" if not apart
              else "separate" if settled else "secret_attention")
    return {"schema_version": VERSION, "kind": "BrokerIdentityAssessment", "checked_at": now(),
            "broker": reply["identity"], "orchestrator": local, "profile": reply["profile"],
            "separate_identity": apart, "source_digest_match": same_source, "secret": secret,
            "secret_max_age_seconds": SECRET_MAX_AGE_SECONDS, "status": status, "live_authorized": False}


class BrokerClient:
    def __init__(self, store, mode, image, executor=None, service=None):
        self.store, self.mode, self.image, self.executor = store, mode, image, executor
        self.service = ServiceClient(service) if service is not None else None
        self.root = None
        if self.service is None:
            self.root = store.root / "broker"
            if self.root.is_symlink():
                raise Rejected("Broker directory link")
            self.root.mkdir(exist_ok=True)

    def run(self, workspace, recipe_name, args, operation_id):
        safe_id(operation_id)
        op = self.store.db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
        if not op or op["status"] != "started":
            raise Rejected("Execution requires a live durable operation")
        request = {"schema_version": VERSION, "operation_id": operation_id, "task_id": op["task_id"], "generation": op["generation"],
                   "nonce": uid(), "workspace": str(Path(workspace).resolve()), "recipe": recipe_name, "args": args,
                   "mode": self.mode, "image": self.image, "source_digest": source_digest()}
        validate_wire(request, "Request")
        previous = self.store.db.execute("SELECT request FROM broker_requests WHERE operation_id=? AND generation=?", (operation_id, op["generation"])).fetchone()
        if previous:
            request = json.loads(previous[0])
            try:
                return self.dispatch_result(operation_id, op["generation"])
            except Rejected as exc:
                raise BrokerUncertain(str(exc)) from exc
        self.store.db.execute("INSERT INTO broker_requests VALUES(?,?,?)", (operation_id, op["generation"], canonical(request).decode()))
        if self.service:
            try:
                reply = self.service.call("execute", {"schema_version": VERSION, "kind": "execute", "request": request})
            except Rejected as exc:
                raise BrokerUncertain("Broker completion uncertain; operator reconciliation required") from exc
            try:
                return self._dispatch(self._accept(operation_id, op["generation"], request, reply["envelope"], "service", reply["identity"]))
            except Rejected as exc:
                raise BrokerUncertain(str(exc)) from exc
        command = [sys.executable, "-m", "orch.broker_process", str(self.root)]
        env = {k: os.environ[k] for k in ("PATH", "SystemRoot", "WINDIR", "TEMP", "TMP") if k in os.environ}
        env["PYTHONPATH"] = os.pathsep.join(sys.path)
        response = capture(command, cwd=Path(__file__).resolve().parents[1], env=env, timeout=40, limit=1024 * 1024, input_bytes=canonical(request))
        if response["failure"] or response["code"] != 0:
            raise BrokerUncertain("Broker completion uncertain; operator reconciliation required")
        try:
            return self.dispatch_result(operation_id, op["generation"])
        except Rejected as exc:
            raise BrokerUncertain(str(exc)) from exc

    @staticmethod
    def _dispatch(result):
        if result["failure"] in ("cleanup_uncertain", "unreaped_stream"):
            raise BrokerUncertain("Execution cleanup requires reconciliation")
        return result

    def dispatch_result(self, operation_id, generation):
        return self._dispatch(self.result(operation_id, generation))

    def _request(self, operation_id, generation):
        safe_id(operation_id)
        op = self.store.db.execute("SELECT generation,status FROM operations WHERE id=?", (operation_id,)).fetchone()
        if not op or op["generation"] != generation or op["status"] != "started":
            raise Rejected("Stale execution fence")
        row = self.store.db.execute("SELECT request FROM broker_requests WHERE operation_id=? AND generation=?", (operation_id, generation)).fetchone()
        if not row:
            raise Rejected("No durable broker request")
        return json.loads(row[0])

    def result(self, operation_id, generation):
        """Accept an existing broker result. This never launches or repeats execution."""
        request = self._request(operation_id, generation)
        if self.service:
            reply = self.service.call("result", {"schema_version": VERSION, "kind": "result", "request": request})
            return self._accept(operation_id, generation, request, reply["envelope"], "service", reply["identity"])
        path = self.root / (operation_id + ".json")
        with file_lock(self.root / (operation_id + ".lock")):
            if not path.is_file() or path.is_symlink() or path.stat().st_size > 1024 * 1024:
                raise Rejected("Broker result unavailable")
            try:
                envelope = json.loads(path.read_text())
            except (ValueError, OSError) as exc:
                raise Rejected("Unreadable broker journal") from exc
        return self._accept(operation_id, generation, request, envelope, "subprocess", identity())

    def _accept(self, operation_id, generation, request, envelope, transport, broker):
        validate_wire(envelope, "Envelope")
        if set(envelope) != {"schema_version", "request_sha256", "operation_id", "task_id", "generation", "nonce", "source_digest", "result"}:
            raise Rejected("Malformed broker envelope")
        for key in ("schema_version", "operation_id", "task_id", "generation", "nonce", "source_digest"):
            if envelope[key] != request[key]:
                raise Rejected("Broker provenance mismatch")
        if envelope["request_sha256"] != digest(request) or request["source_digest"] != source_digest():
            raise Rejected("Broker request or runtime changed")
        result = envelope["result"]
        expected = {"command", "started", "ended", "code", "stdout", "stderr", "truncated", "failure"}
        if set(result) != expected or not isinstance(result["stdout"], str) or not isinstance(result["stderr"], str):
            raise Rejected("Malformed execution result")
        if len(result["stdout"].encode()) > 262144 or len(result["stderr"].encode()) > 262144:
            raise Rejected("Broker output exceeded bound")
        local = identity()
        record = {"schema_version": VERSION, "transport": transport, "broker": broker, "orchestrator": local,
                  "separate_identity": separate(broker, local), "recorded_at": now()}
        validate_wire(record, "ExecutionIdentity")
        saved = self.store.artifact(request["task_id"], envelope, producer="action_broker", trust="trusted_receipt")
        self.store.db.execute("INSERT OR IGNORE INTO execution_provenance VALUES(?,?,?,?)", (operation_id, generation, digest(envelope), canonical(saved).decode()))
        self.store.db.execute("INSERT OR IGNORE INTO broker_identities VALUES(?,?,?,?)", (operation_id, generation, canonical(record).decode(), digest(record)))
        return result

    def retire(self, operation_id, generation):
        """Durable tombstone before any late launch, then prove the container is gone. Docker authority stays with the broker."""
        safe_id(operation_id)
        if self.service:
            nonce = uid()
            reply = self.service.call("retire", {"schema_version": VERSION, "kind": "retire", "nonce": nonce,
                                                 "operation_id": operation_id, "generation": generation})
            if reply["nonce"] != nonce or reply["operation_id"] != operation_id or reply["generation"] != generation:
                raise Rejected("Broker retirement reply mismatch")
            return reply["container_absent"]
        with file_lock(self.root / (operation_id + ".lock")):
            if (self.root / (operation_id + ".json")).exists():
                raise Rejected("Invalid journal must be investigated, not discarded")
            atomic_json(self.root / (operation_id + ".retired"), {"generation": generation})
            return self.executor.reconcile(operation_id)

    def reconcile(self, operation_id):
        safe_id(operation_id)
        if self.service:
            nonce = uid()
            reply = self.service.call("reconcile", {"schema_version": VERSION, "kind": "reconcile", "nonce": nonce, "operation_id": operation_id})
            if reply["nonce"] != nonce or reply["operation_id"] != operation_id:
                raise Rejected("Broker reconciliation reply mismatch")
            return reply["container_absent"]
        return self.executor.reconcile(operation_id)
