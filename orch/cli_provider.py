"""Provider-neutral JSON adapter; only the bundled fixture transport is admitted."""
import asyncio
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from typing import Protocol

from jsonschema import Draft202012Validator
from contracts.interfaces import HealthResult, ProviderCapabilities, ValidatedContract
from .contracts import Rejected, canonical, error, make, now, redact, ref, validate
from .fixtures import recipe
from .process import capture
from .provider import Scenario

ADAPTER_VERSION = "cli-json-v1"
PROMPT_VERSION = "role-json-v1"
ROLES = {"lead_planner": ("ImplementationPlan", ["TaskSpec"]),
         "junior": ("FixtureEdit-v1", ["TaskSpec", "ImplementationPlan", "WorkOrder"]),
         "reviewer": ("ReviewDecision", ["TaskSpec", "ImplementationPlan", "PatchReceipt", "diff", "TestReceipt", "ReviewRequest"])}
SCHEMA = json.loads((Path(__file__).resolve().parents[1] / "contracts/provider-config-v1.schema.json").read_text())
FIXTURE = Path(__file__).with_name("provider_fixture.py").resolve()


@dataclass(frozen=True)
class ProviderConfig:
    schema_version: str
    provider: str
    model: str | None
    executable: str
    executable_sha256: str
    arguments: tuple[str, ...]
    delivery: str
    timeout_seconds: int
    max_output_bytes: int
    max_prompt_bytes: int

    @classmethod
    def parse(cls, value):
        if not Draft202012Validator(SCHEMA).is_valid(value):
            raise Rejected("Invalid provider configuration")
        path = Path(value["executable"])
        if not path.is_absolute() or path.suffix.lower() in (".bat", ".cmd", ".ps1"):
            raise Rejected("Provider requires an absolute executable, not a shell script")
        arguments = value["arguments"]
        placeholders = sum(arg == "{prompt}" for arg in arguments)
        if any("{prompt}" in arg and arg != "{prompt}" for arg in arguments):
            raise Rejected("Prompt placeholder must occupy one complete argument")
        if placeholders != (1 if value["delivery"] == "prompt_argument" else 0):
            raise Rejected("Invalid prompt delivery template")
        if redact(canonical(value).decode()) != canonical(value).decode():
            raise Rejected("Recognized secret in provider configuration")
        return cls(**{**value, "arguments": tuple(arguments)})


class Transport(Protocol):
    def execute(self, config: ProviderConfig, prompt: bytes, timeout: int, limit: int, cancelled: threading.Event) -> dict: ...


class FixtureTransport:
    def __init__(self):
        self.fixture_sha256 = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()

    def execute(self, config, prompt, timeout, limit, cancelled):
        executable = Path(config.executable)
        args = list(config.arguments)
        if config.provider != "fixture_cli" or executable.resolve() != Path(sys.executable).resolve() or args[:2] != ["-I", str(FIXTURE)]:
            raise Rejected("Only the bundled fixture executable is admitted")
        tail = args[2:]
        if config.delivery == "prompt_argument":
            if tail[-2:] != ["-p", "{prompt}"]:
                raise Rejected("Invalid fixture argument delivery")
            tail = tail[:-2]
        if tail and (len(tail) != 2 or tail[0] != "--behavior" or tail[1] not in ("normal", "invalid-json", "nonzero", "flood", "wait", "secret", "wrong-task", "wrong-shape")):
            raise Rejected("Unexpected fixture arguments")
        if hashlib.sha256(executable.read_bytes()).hexdigest() != config.executable_sha256 or hashlib.sha256(FIXTURE.read_bytes()).hexdigest() != self.fixture_sha256:
            raise Rejected("Provider executable or fixture digest changed")
        argv = [config.executable, *(prompt.decode() if a == "{prompt}" else a for a in args)]
        env = {k: os.environ[k] for k in ("PATH", "SystemRoot", "WINDIR", "TEMP", "TMP") if k in os.environ}
        with tempfile.TemporaryDirectory(prefix="orch-provider-") as cwd:
            return capture(argv, cwd=cwd, env=env, timeout=timeout, limit=limit,
                           input_bytes=prompt if config.delivery == "stdin" else None, cancel_event=cancelled)


class JsonCliProvider:
    def __init__(self, store, config, transport=None):
        self.store, self.config, self.transport = store, config, transport
        # Parsing is repeated even for a manually constructed dataclass.
        ProviderConfig.parse({**asdict(config), "arguments": list(config.arguments)})
        self.active = {}

    def capabilities(self):
        fixture = self.config.provider == "fixture_cli" and type(self.transport) is FixtureTransport
        return ProviderCapabilities(fixture, True, fixture, False, tuple(ROLES))

    async def health_check(self):
        if not self.capabilities().proposal_only_verified:
            return HealthResult("unsupported", ADAPTER_VERSION, "Live provider admission not implemented")
        executable = Path(self.config.executable)
        if not executable.is_file() or hashlib.sha256(executable.read_bytes()).hexdigest() != self.config.executable_sha256:
            return HealthResult("unavailable", ADAPTER_VERSION, "Executable absent or changed")
        return HealthResult("ready", ADAPTER_VERSION, "Bundled offline fixture only; no authentication performed")

    async def cancel(self, invocation_id):
        if invocation_id in self.active:
            self.active[invocation_id].set()

    def prompt(self, invocation):
        role = invocation["role"]
        if role not in ROLES or invocation["expected_result_contract"] != ROLES[role][0]:
            raise Rejected("Unexpected role or result contract")
        if invocation["allowed_capabilities"] or invocation["workspace_id"] is not None:
            raise Rejected("Proposal-only invocations cannot receive tools or workspaces")
        context = []
        for artifact in invocation["supplied_artifacts"]:
            data = json.loads(self.store.read_artifact(artifact, invocation["task_id"]))
            if not isinstance(data, dict):
                raise Rejected("Structured role context required")
            if data.get("contract_type"):
                validate(data)
                if data["task_id"] != invocation["task_id"]:
                    raise Rejected("Cross-task context")
            elif set(data) != {"diff"} or not isinstance(data["diff"], str):
                raise Rejected("Unexpected untyped context")
            context.append({"trust": "untrusted_data", "reference": artifact, "contract_reference": ref(data) if data.get("contract_type") else None, "data": data})
        if [x["data"].get("contract_type", "diff") for x in context] != ROLES[role][1]:
            raise Rejected("Role context does not match allowlist")
        manifest = json.loads(self.store.read_artifact(invocation["context_manifest"], invocation["task_id"]))
        if manifest != {"role": role, "contracts": ROLES[role][1]}:
            raise Rejected("Context manifest mismatch")
        return {"prompt_version": PROMPT_VERSION, "invocation": invocation,
                "system_instructions": self.store.read_artifact(invocation["system_instructions"], invocation["task_id"]), "artifacts": context}

    def parse_result(self, raw, invocation, prompt):
        def unique(pairs):
            data = {}
            for key, value in pairs:
                if key in data:
                    raise Rejected("Duplicate JSON key")
                data[key] = value
            return data
        result = json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(Rejected("Non-JSON number")))
        role = invocation["role"]
        if role == "junior":
            if not isinstance(result, dict) or set(result) != {"content"} or result["content"] not in ("hello world\n", "wrong\n"):
                raise Rejected("Invalid fixture edit proposal")
            return result
        validate(result, invocation["expected_result_contract"])
        context = [x["data"] for x in prompt["artifacts"]]
        if result["task_id"] != invocation["task_id"]:
            raise Rejected("Cross-task provider result")
        if role == "lead_planner" and (result["spec"] != ref(context[0]) or result["planner_invocation_id"] != invocation["id"] or result["repository"] != context[0]["repository"]):
            raise Rejected("Plan binding mismatch")
        if role == "reviewer" and (result["request"] != ref(context[-1]) or result["reviewer_invocation_id"] != invocation["id"]):
            raise Rejected("Review binding mismatch")
        return result

    async def invoke(self, request):
        invocation = validate(dict(request.payload), "AgentInvocation")
        if invocation["id"] in self.active:
            raise Rejected("Invocation already running")
        started = now()
        outcome = {"code": None, "stdout": "", "stderr": "", "truncated": False, "failure": None}
        failure, result = None, None
        cancel = threading.Event()
        self.active[invocation["id"]] = cancel
        try:
            if (invocation["provider"], invocation["model"], invocation["adapter_version"], invocation["prompt_version"]) != (self.config.provider, self.config.model, ADAPTER_VERSION, PROMPT_VERSION):
                raise Rejected("Provider configuration mismatch")
            if not self.capabilities().proposal_only_verified:
                raise Rejected("Live provider admission not implemented")
            prompt = self.prompt(invocation)
            raw = canonical(prompt)
            if len(raw) > self.config.max_prompt_bytes or redact(raw.decode()) != raw.decode():
                raise Rejected("Prompt over budget or contains recognized secret")
            work = asyncio.create_task(asyncio.to_thread(self.transport.execute, self.config, raw,
                                      min(self.config.timeout_seconds, invocation["timeout_seconds"]),
                                      min(self.config.max_output_bytes, invocation["max_output_bytes"]), cancel))
            try:
                outcome = await asyncio.shield(work)
            except asyncio.CancelledError:
                cancel.set()
                await asyncio.shield(work)
                raise
            if outcome["failure"]:
                failure = outcome["failure"]
            elif outcome["code"] != 0:
                failure = "provider_exit_nonzero"
            elif redact(outcome["stdout"]) != outcome["stdout"]:
                failure = "sensitive_provider_output"
            else:
                result = self.parse_result(outcome["stdout"], invocation, prompt)
        except (Rejected, ValueError, OSError, KeyError, TypeError):
            failure = "provider_request_or_result_rejected"
        finally:
            self.active.pop(invocation["id"], None)
        task = invocation["task_id"]
        receipt = make("AgentInvocationReceipt", task, invocation_id=invocation["id"], provider=self.config.provider, model=self.config.model,
                       started_at=started, ended_at=now(), exit_code=outcome["code"],
                       stdout=self.store.artifact(task, outcome["stdout"], "text/plain", "provider_broker"),
                       stderr=self.store.artifact(task, outcome["stderr"], "text/plain", "provider_broker"),
                       output_truncated=outcome["truncated"], structured_result=self.store.artifact(task, result) if result is not None and failure is None else None,
                       input_tokens=None, output_tokens=None, cost=None, error=error(failure) if failure else None)
        return ValidatedContract("AgentInvocationReceipt", receipt)


class FixtureCliProvider(JsonCliProvider):
    provider_id = "fixture_cli"
    model_id = "fixture-v1"
    adapter_version = ADAPTER_VERSION
    prompt_version = PROMPT_VERSION

    def __init__(self, store, scenario=None, review_index=0, behavior="normal", delivery="stdin"):
        self.scenario, self.review_index = scenario or Scenario(), review_index
        config = ProviderConfig.parse({"schema_version": "1.0.0", "provider": self.provider_id, "model": self.model_id,
                                      "executable": sys.executable, "executable_sha256": hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest(),
                                      "arguments": ["-I", str(FIXTURE), "--behavior", behavior] + (["-p", "{prompt}"] if delivery == "prompt_argument" else []),
                                      "delivery": delivery, "timeout_seconds": 10, "max_output_bytes": 65536, "max_prompt_bytes": 65536})
        super().__init__(store, config, FixtureTransport())

    def prompt(self, invocation):
        value = super().prompt(invocation)
        value.update(fixture_scenario=asdict(self.scenario), fixture_review_index=self.review_index, fixture_tests=[recipe()])
        return value
