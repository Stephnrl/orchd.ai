"""Deployment gap assessment; this report cannot authorize a live provider."""
from .broker import assess_identity
from .contracts import Rejected, now
from .copilot import provider_check
from .readiness import require_current_report


def deployment_check(provider, executable=None, expected_sha256=None, image=None, runtime_report=None, broker=None):
    if runtime_report and not image:
        raise Rejected("A runtime report requires its digest-pinned image")
    inventory = provider_check(provider, executable, expected_sha256)
    runtime = {"status": "not_supplied", "reason": "Supply a current runtime report and its image"}
    if runtime_report:
        try:
            require_current_report(runtime_report, image)
            runtime = {"status": "current", "reason": None}
        except (Rejected, OSError):
            runtime = {"status": "rejected", "reason": "Runtime evidence is invalid, expired, or does not match the current source/host/image"}
    identity = {"status": "not_supplied", "reason": "Supply the broker service endpoint and its secret file to assess the broker OS identity"}
    outbound = False
    if broker is not None:
        try:
            assessment = assess_identity(broker)
            outbound = bool((assessment.get("profile") or {}).get("dispatch"))
            reasons = {"separate": None,
                       "secret_attention": "The broker shared secret is mid-rotation or older than the reported maximum age; see broker-check",
                       "source_mismatch": "The broker service runs different broker source than this orchestrator; see broker-check"}
            identity = {"status": assessment["status"],
                        "reason": reasons.get(assessment["status"], "The broker service runs as the orchestrator account; see broker-check")}
        except (Rejected, OSError):
            identity = {"status": "rejected", "reason": "The broker service is unreachable, unauthenticated or misconfigured"}
    gates = [
        {"id": "executable_digest", "status": "passed" if inventory["executable_status"] == "digest_match" else "blocked",
         "next_step": "Compare the explicit executable with an independently approved digest; a match alone does not establish origin or safe behavior"},
        {"id": "worker_runtime", "status": "passed" if runtime["status"] == "current" else "blocked",
         "next_step": "Run verify-runtime on the deployment host, then supply its current report and the same image"},
        {"id": "broker_identity", "status": "passed" if identity["status"] == "separate" else "blocked",
         "next_step": "Run broker-serve under a separate OS account with a private shared secret, then supply --broker-endpoint and --broker-secret"},
        {"id": "native_protocol", "status": "blocked",
         "next_step": "Verify the offline Copilot codec against the exact deployed CLI version" if inventory["protocol"] else "Document and implement the corporate CLI native protocol"},
        {"id": "provider_containment", "status": "blocked",
         "next_step": "Independently test native tool disablement, outbound access and process-tree termination for this provider; worker Docker tests are insufficient"},
        {"id": "outbound_dispatch",
         "status": "passed" if outbound and identity["status"] == "separate" else "blocked",
         # A dispatch credential only means anything when the broker is a different account.
         # On a shared account the orchestrator, and everything it runs, can read the file.
         "next_step": ("Give broker-serve a private --dispatch-credential naming its origin"
                       if not outbound else
                       "Run that broker under a separate OS account; a dispatch credential on the "
                       "orchestrator's own account is readable by everything the orchestrator runs")},
        {"id": "credentials", "status": "blocked",
         "next_step": "Review credential and SSO provisioning, isolation, revocation and redaction on the deployment host without including secrets in this report"},
        {"id": "live_admission", "status": "blocked",
         "next_step": "Implement and review the live launcher and admission policy after provider-specific evidence is accepted"},
    ]
    return {"schema_version": "1.0.0", "kind": "DeploymentReadiness", "generated_at": now(),
            "status": "blocked", "provider_authorized": False, "provider": inventory,
            "worker_runtime": runtime, "broker_identity": identity, "gates": gates,
            "blocking_gates": [gate["id"] for gate in gates if gate["status"] == "blocked"]}
