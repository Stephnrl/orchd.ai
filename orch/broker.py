"""Single-host broker journal and fenced envelopes. No shared credentials or sockets."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import sys

from .contracts import Rejected, canonical, digest, uid
from .process import capture
from jsonschema import Draft202012Validator
from .contracts import FORMATS

VERSION = "1.0.0"
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
             for name in ("broker.py", "broker_process.py", "execution.py", "process.py", "fixtures.py")}
    files["wire_schema"] = digest(WIRE_SCHEMA)
    return digest(files)


class BrokerClient:
    def __init__(self, store, mode, image):
        self.store, self.mode, self.image = store, mode, image
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

    def dispatch_result(self, operation_id, generation):
        result = self.result(operation_id, generation)
        if result["failure"] in ("cleanup_uncertain", "unreaped_stream"):
            raise BrokerUncertain("Execution cleanup requires reconciliation")
        return result

    def result(self, operation_id, generation):
        safe_id(operation_id)
        op = self.store.db.execute("SELECT generation,status FROM operations WHERE id=?", (operation_id,)).fetchone()
        if not op or op["generation"] != generation or op["status"] != "started":
            raise Rejected("Stale execution fence")
        row = self.store.db.execute("SELECT request FROM broker_requests WHERE operation_id=? AND generation=?", (operation_id, generation)).fetchone()
        if not row:
            raise Rejected("No durable broker request")
        request = json.loads(row[0])
        path = self.root / (operation_id + ".json")
        with file_lock(self.root / (operation_id + ".lock")):
            if not path.is_file() or path.is_symlink() or path.stat().st_size > 1024 * 1024:
                raise Rejected("Broker result unavailable")
            try:
                envelope = json.loads(path.read_text())
            except (ValueError, OSError) as exc:
                raise Rejected("Unreadable broker journal") from exc
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
        saved = self.store.artifact(request["task_id"], envelope, producer="action_broker", trust="trusted_receipt")
        self.store.db.execute("INSERT OR IGNORE INTO execution_provenance VALUES(?,?,?,?)", (operation_id, generation, digest(envelope), canonical(saved).decode()))
        return result
