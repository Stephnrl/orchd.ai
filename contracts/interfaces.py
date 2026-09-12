"""Design-only protocol boundary; no provider, broker or workflow implementation."""
from dataclasses import dataclass
from typing import Literal, Mapping, Protocol, TypeAlias

JSONValue: TypeAlias = None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]


@dataclass(frozen=True)
class ValidatedContract:
    """Boundary codec must validate payload against named v1 schema before construction.

    This wrapper is illustrative, not an authentication or validation mechanism.
    Runtime domain types must be generated or checked against the schemas.
    """
    contract_type: str
    payload: Mapping[str, JSONValue]


@dataclass(frozen=True)
class ProviderCapabilities:
    proposal_only_verified: bool
    structured_output: bool
    cancellation: bool
    usage_reporting: bool
    supported_roles: tuple[str, ...]


@dataclass(frozen=True)
class HealthResult:
    status: Literal["ready", "unavailable", "authentication_required", "unsupported"]
    adapter_version: str
    detail: str  # Redacted; never credentials or complete environment dumps.


class AgentProvider(Protocol):
    async def invoke(self, request: ValidatedContract) -> ValidatedContract:
        """AgentInvocation -> AgentInvocationReceipt; bounded output and deadline."""
        ...

    def capabilities(self) -> ProviderCapabilities: ...
    async def health_check(self) -> HealthResult: ...
    async def cancel(self, invocation_id: str) -> None: ...


class Guardian(Protocol):
    def evaluate(self, request: ValidatedContract) -> ValidatedContract:
        """ToolRequest -> ToolDecision; loads trusted identity/scope from persistence."""
        ...


class ActionBroker(Protocol):
    async def execute(self, operation_id: str, fencing_token: int) -> ValidatedContract:
        """Load durable approved request, recheck guards, execute or return existing receipt."""
        ...

    async def reconcile(self, operation_id: str) -> ValidatedContract: ...


class ActionHookAdapter(Protocol):
    def pre_action(self, request: ValidatedContract) -> bytes:
        """Encode optional version-pinned ACS request; no workflow dependency on ACS."""
        ...

    def decode_decision(self, response: bytes) -> ValidatedContract:
        """Validate/map wire decision; unsupported semantics deny."""
        ...

    def post_action(self, receipt: ValidatedContract) -> bytes: ...
