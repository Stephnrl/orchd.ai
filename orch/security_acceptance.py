"""Executable mapping from the mandatory security criteria to the tests that exercise them.

`docs/security.md` states ten acceptance criteria as prose. This module binds each one to
the exact tests that exercise it, runs them, and reports what local evidence does and does
not establish. It never claims a criterion is met beyond what those tests check: container
isolation depends on the opt-in Docker gates, browser-rendering checks belong to other
release stages, and several criteria remain deferred to a deployment host or an
independent review. No report here authorizes a live provider or an external action.
"""
from datetime import datetime, timezone
import io
from pathlib import Path
import re
import sys
import unittest

from .contracts import Rejected, now
from .readiness import EXPECTED

DOCKER_GATES = tuple(EXPECTED)   # The five opt-in container gates, as readiness names them.

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs/security.md"
TESTS = ROOT / "tests"

# Each criterion lists the tests that actually exercise it. `docker` tests run only with the
# opt-in image and are reported as deferred otherwise. `other_stages` names release stages
# that cover part of the criterion but are not run here. `deferred` states what no local
# test can establish; those sentences are the honest limit of this report.
CRITERIA = (
    {"id": "SEC-01",
     "summary": "Server-enforced transitions and approval consumption; stale, forged, expired, cross-task and replayed approvals fail without execution",
     "offline": ("test_workflow.WorkflowTests.test_state_matrix",
                 "test_workflow.WorkflowTests.test_important_guards",
                 "test_workflow.WorkflowTests.test_api_auth_and_closed_approval_boundary",
                 "test_workflow.WorkflowTests.test_approval_rejection_and_duplicate",
                 "test_boundaries.BoundaryTests.test_expired_and_stale_approval",
                 "test_approval_renewal.ApprovalRenewalTests.test_current_request_stale_revision_and_cross_task_are_rejected",
                 "test_repository_tasks.RepositoryTaskTests.test_stale_and_cross_task_approval_and_batch_rejection",
                 "test_repository_tasks.RepositoryTaskTests.test_standalone_pilot_run_cannot_bypass_kernel_approval",
                 "test_pilot.PilotTests.test_second_connection_cannot_reuse_approval",
                 "test_management.ManagementTests.test_approval_evidence_does_not_approve_and_stale_revision_fails",
                 "test_agent_sessions.AgentSessionTests.test_a_claim_must_match_the_role_the_next_step_needs",
                 "test_agent_sessions.AgentSessionTests.test_the_role_follows_the_task_through_the_workflow",
                 "test_agent_api.AgentApiTests.test_the_secret_decides_which_agent_is_calling",
                 "test_agent_api.AgentApiTests.test_an_agent_may_only_claim_the_roles_it_was_enrolled_for",
                 "test_agent_api.AgentApiTests.test_a_tampered_body_never_reaches_the_store",
                 "test_task_drafts.DraftSpecTests.test_a_specification_revised_after_it_was_read_cannot_be_confirmed",
                 "test_task_drafts.DraftSpecTests.test_a_confirmation_does_not_survive_a_return_to_draft",
                 "test_task_drafts.DraftSpecTests.test_nothing_may_be_drafted_once_the_specification_is_confirmed",
                 "test_dispatch.DispatchTests.test_only_the_approved_request_is_sent",
                 "test_dispatch.DispatchTests.test_a_credential_is_never_sent_to_an_origin_it_does_not_name",
                 "test_dispatch.DispatchTests.test_a_request_may_not_carry_its_own_authentication",
                 "test_dispatch.BrokerDispatchTests.test_a_request_that_is_not_the_approved_one_never_reaches_the_origin"),
     "docker": (), "other_stages": ("browser",),
     "deferred": ("The single local operator is trusted; no multi-user authorization or corporate identity is exercised.",
                  "A specification is confirmed by a human naming its hash, but the operator drafting and the operator confirming are the same trusted local account; separating those two people is not exercised.",
                  "Outbound dispatch is fenced to an approved request hash and a credential-bound origin, but it is not yet wired to the task workflow: reaching PR_CREATED still requires a simulated receipt, and live use remains behind the outbound_dispatch and credentials gates.",
                  "An agent container's role is enforced by the loopback service that authenticates it; a session claimed directly against the store is still cooperative, which is the operator's own path.",
                  "Agent secrets are files on one host readable by the account running each container: this is not multi-user authentication, corporate identity or a credential vault.")},
    {"id": "SEC-02",
     "summary": "Models never author trusted command or test receipts; actual argv, exit status, timing and bounded output are independently recorded",
     "offline": ("test_runtime.RuntimeTests.test_broker_subprocess_provenance_and_cleanup",
                 "test_runtime.RuntimeTests.test_tampered_envelope_and_active_broker_rejected",
                 "test_provenance_audit.ProvenanceAuditTests.test_envelope_bindings_reject_even_when_artifact_and_envelope_hashes_match",
                 "test_provenance_audit.ProvenanceAuditTests.test_request_row_bindings_and_missing_request_reject",
                 "test_boundaries.BoundaryTests.test_process_timeout_receipt",
                 "test_workflow.WorkflowTests.test_duplicate_worker_receipt",
                 "test_broker_service.BrokerServiceTests.test_result_route_never_executes_and_reports_missing_results",
                 "test_broker_service.BrokerServiceTests.test_profile_workspace_and_recipe_are_broker_policy"),
     "docker": (), "other_stages": (),
     "deferred": ("Receipts are provenance within a trusted host OS identity, not signed evidence; a host administrator can still rewrite them.",)},
    {"id": "SEC-03",
     "summary": "Workers cannot reach host files beyond assigned mounts, Docker, credentials, the orchestrator database, other tasks or the network",
     "offline": ("test_boundaries.BoundaryTests.test_docker_command_restrictions_and_untrusted_script",
                 "test_pilot_workers.PilotWorkerTests.test_mount_entrypoint_and_resource_policy",
                 "test_runtime.RuntimeTests.test_cleanup_refuses_unexpected_content",
                 "test_broker_service.BrokerServiceTests.test_profile_workspace_and_recipe_are_broker_policy",
                 "test_pilot_broker.PilotBrokerTests.test_service_scope_cannot_run_directly_and_direct_scope_cannot_use_service"),
     "docker": DOCKER_GATES, "other_stages": (),
     "deferred": ("Offline tests check the requested container profile, not the kernel boundary. Isolation is established only by the Docker gates on a qualified host, and those gates are not a complete kernel-escape assessment.",)},
    {"id": "SEC-04",
     "summary": "Every write stays within approved spec, plan, base, path and budget scope; changed evidence invalidates what depends on it",
     "offline": ("test_boundaries.BoundaryTests.test_tampered_artifact_fails_closed",
                 "test_pilot.PilotTests.test_scope_tampering_is_rejected",
                 "test_pilot.PilotTests.test_workspace_tampering_is_visible",
                 "test_pilot.PilotTests.test_noop_unknown_fields_missing_checks_and_byte_budget",
                 "test_batches.BatchTests.test_scope_revision_source_runtime_and_expiry_are_checked",
                 "test_approval_renewal.ApprovalRenewalTests.test_changed_scope_is_not_silently_renewed",
                 "test_pilot_broker.PilotBrokerTests.test_redelivery_returns_retained_observation_and_foreign_scopes_are_refused"),
     "docker": (), "other_stages": (),
     "deferred": ("The admitted write scope is one fixture file and bounded JSON replacements; no general patch scope is exercised.",)},
    {"id": "SEC-05",
     "summary": "Plan approval precedes write-capable execution, specific actions need specific human approval, and unsupported operations stay denied",
     "offline": ("test_boundaries.BoundaryTests.test_no_external_effect_before_action_approval",
                 "test_boundaries.BoundaryTests.test_guardian_denies_arbitrary_commands_and_secrets",
                 "test_workflow.WorkflowTests.test_important_guards",
                 "test_repository_tasks.RepositoryTaskTests.test_reject_plan_or_result_never_creates_remote_effect",
                 "test_batches.BatchTests.test_serial_end_to_end_requires_each_individual_approval"),
     "docker": (), "other_stages": ("browser",),
     "deferred": ("Destructive, privilege, infrastructure, production and external-communication operations are not implemented, so their approval paths are unexercised.",)},
    {"id": "SEC-06",
     "summary": "Redaction, UI escaping, task artifact authorization and output or resource limits hold under adversarial input",
     "offline": ("test_boundaries.BoundaryTests.test_secrets_redacted_and_title_rejected",
                 "test_runtime.RuntimeTests.test_capture_bounds_output_and_deadline",
                 "test_runtime.RuntimeTests.test_quotas_and_orphan_collection_preserve_evidence",
                 "test_provenance_audit.ProvenanceAuditTests.test_hash_trust_and_cross_task_artifact_reject",
                 "test_management.ManagementTests.test_task_bound_records_artifacts_and_integrity",
                 "test_http_hardening.HTTPHardeningTests.test_ambiguous_framing_and_json_reject_without_task_creation",
                 "test_storage_usage.StorageUsageTests.test_missing_and_wrong_size_are_reported_without_content_exposure",
                 "test_worker_admission.WorkerAdmissionTests.test_the_bound_refuses_new_work_rather_than_failing_halfway",
                 "test_worker_admission.WorkerAdmissionTests.test_a_claim_left_by_a_crash_does_not_hold_a_slot",
                 "test_dispatch.DispatchTests.test_the_token_never_appears_in_the_receipt",
                 "test_dispatch.DispatchTests.test_a_request_carrying_a_secret_never_leaves",
                 "test_dispatch.BrokerDispatchTests.test_the_broker_dispatches_and_the_caller_never_sees_the_token"),
     "docker": (), "other_stages": ("tests/test_ui_*.cjs", "browser"),
     "deferred": ("Redaction recognises configured values and common token shapes; it cannot recognise every transformed secret. Rendering safety is checked by the UI script and browser stages, not here.",
                  "A dispatch credential is kept out of its receipts and off the caller's side of the broker, but it is a file on one host readable by the broker account: that is process separation, not a credential vault.")},
    {"id": "SEC-07",
     "summary": "Audit replay and restore reconstruct the lifecycle; crash injection across dispatch and receipt boundaries neither repeats nor loses effects",
     "offline": ("test_workflow.WorkflowTests.test_straight_through_and_reconstruction",
                 "test_workflow.WorkflowTests.test_crash_after_receipt_resumes_without_execution",
                 "test_workflow.WorkflowTests.test_external_duplicate_and_crash_recovery",
                 "test_boundaries.BoundaryTests.test_new_id_cannot_duplicate_worker_operation",
                 "test_runtime.RuntimeTests.test_operator_recovery_fences_late_results",
                 "test_runtime.RuntimeTests.test_restore_drill_continues_approval_paused_task",
                 "test_provenance_audit.ProvenanceAuditTests.test_corrupt_provenance_blocks_restore_even_with_updated_manifest",
                 "test_pilot.PilotTests.test_crash_after_reservation_never_retries",
                 "test_broker_service.BrokerServiceTests.test_crash_after_execution_before_journal_blocks_without_relaunch",
                 "test_journal_succession.GitHubSuccessionTests.test_successor_refuses_an_identifier_its_chain_already_holds",
                 "test_journal_succession.GitHubSuccessionTests.test_an_interrupted_retirement_leaves_the_journal_open_and_the_heir_inert",
                 "test_journal_succession.JiraSuccessionTests.test_successor_refuses_an_identifier_its_chain_already_holds",
                 "test_journal_succession.JiraSuccessionTests.test_an_interrupted_retirement_leaves_the_journal_open_and_the_heir_inert",
                 "test_journal_revision.GitHubRevisionTests.test_a_consumed_attempt_can_never_be_withdrawn",
                 "test_journal_revision.GitHubRevisionTests.test_storage_refuses_a_second_live_operation_and_any_rewrite",
                 "test_journal_revision.JiraRevisionTests.test_a_consumed_attempt_can_never_be_withdrawn",
                 "test_journal_revision.JiraRevisionTests.test_storage_refuses_a_second_live_operation_and_any_rewrite",
                 "test_task_ownership.TaskOwnershipTests.test_takeover_never_repeats_an_interrupted_operation",
                 "test_task_ownership.TaskOwnershipTests.test_two_workers_advance_different_tasks_at_once"),
     "docker": (), "other_stages": ("browser",),
     "deferred": ("External effects are simulated, so exactly-once behaviour against a real remote system is untested.",
                  "Chain-wide admission refuses a second attempt slot within one journal chain; it cannot prove a first attempt's outcome at the remote.",
                  "Withdrawal releases a task whose attempt was never consumed; no local rule can release one that was, because the remote outcome is unknown.",
                  "Task ownership is enforced by advisory file locks on one host; nothing here establishes safety between two machines.")},
    {"id": "SEC-08",
     "summary": "The reviewer is a separate invocation with only approved evidence; ACCEPT never overrides deterministic failures, and both rejection routes retry",
     "offline": ("test_workflow.WorkflowTests.test_reviewer_context",
                 "test_workflow.WorkflowTests.test_needs_changes_full_loop",
                 "test_workflow.WorkflowTests.test_review_failure_and_reject_exhaust_budget",
                 "test_workflow.WorkflowTests.test_actual_test_failure_then_retry"),
     "docker": (), "other_stages": (),
     "deferred": ("The reviewer is a deterministic fixture; no live model review is exercised, and fixture agreement is not evidence about a real provider.",)},
    {"id": "SEC-09",
     "summary": "Provider tool-disable capability is verified before any host CLI use; unverified corporate behaviour blocks real provider enablement",
     "offline": ("test_cli_provider.CliProviderTests.test_live_providers_never_launch",
                 "test_copilot.CopilotProtocolTests.test_fixed_config_and_no_live_admission",
                 "test_copilot.CopilotProtocolTests.test_rejects_protocol_noise_tools_and_binding_mismatches",
                 "test_copilot.ProviderInventoryTests.test_cli_reports_unavailable_without_opening_workflow_storage",
                 "test_deployment.DeploymentTests.test_missing_prerequisites_do_not_probe_or_create_storage",
                 "test_deployment.DeploymentTests.test_matching_binary_and_current_worker_evidence_never_authorize"),
     "docker": (), "other_stages": (),
     "deferred": ("Tool-disable behaviour has not been verified against any deployed provider CLI. These tests prove only that live launch stays disabled and that the assessment never authorizes it.",)},
    {"id": "SEC-10",
     "summary": "Image and dependency digests are recorded before execution; network-disabled tests run from a trusted recipe against the reviewed snapshot",
     "offline": ("test_pilot_workers.PilotWorkerTests.test_profile_image_daemon_and_downgrade_fences",
                 "test_pilot_workers.PilotWorkerTests.test_mount_entrypoint_and_resource_policy",
                 "test_pilot_workers.PilotWorkerTests.test_missing_daemon_rejects_preparation",
                 "test_boundaries.BoundaryTests.test_docker_command_restrictions_and_untrusted_script",
                 "test_readiness.ReadinessTests.test_no_image_pull_or_docker_install",
                 "test_readiness.ReadinessTests.test_daemon_failure_windows_mode_and_image_mismatch",
                 "test_readiness.ReadinessTests.test_environment_drift_or_skips_fail_verification"),
     "docker": DOCKER_GATES, "other_stages": (),
     "deferred": ("No dependency or image vulnerability scanning is implemented. A pinned digest identifies an image; it does not establish that the image is safe.",)},
)


def documented_ids():
    """The criterion identifiers exactly as docs/security.md lists them."""
    try:
        text = DOC.read_text(encoding="utf-8")
    except OSError as exc:
        raise Rejected("The security criteria document is unreadable") from exc
    found = re.findall(r"^- (SEC-\d\d):", text, re.M)
    if not found:
        raise Rejected("The security criteria document lists no criteria")
    return tuple(found)


def discovered_ids():
    """Every test the offline suite can load, so a renamed or deleted test is visible."""
    for entry in (str(ROOT), str(TESTS)):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    loader = unittest.TestLoader()
    suite = loader.discover(str(TESTS))
    if loader.errors:
        raise Rejected("The test suite could not be loaded completely")

    def walk(item):
        for child in item:
            if isinstance(child, unittest.TestSuite):
                yield from walk(child)
            else:
                yield child.id()

    return frozenset(walk(suite))


def check_mapping():
    """Every documented criterion is mapped once and every mapped test still exists."""
    mapped = tuple(criterion["id"] for criterion in CRITERIA)
    if mapped != documented_ids():
        raise Rejected("The mapped criteria differ from the ones docs/security.md documents")
    if len(set(mapped)) != len(mapped):
        raise Rejected("A criterion is mapped more than once")
    available = discovered_ids()
    for criterion in CRITERIA:
        names = criterion["offline"] + criterion["docker"]
        if not names:
            raise Rejected("Criterion " + criterion["id"] + " names no test")
        if len(set(names)) != len(names):
            raise Rejected("Criterion " + criterion["id"] + " names a test twice")
        missing = sorted(name for name in names if name not in available)
        if missing:
            raise Rejected("Criterion " + criterion["id"] + " names tests that no longer exist: " + ", ".join(missing))
        unexpected = sorted(set(criterion["docker"]) - set(DOCKER_GATES))
        if unexpected:
            raise Rejected("Criterion " + criterion["id"] + " claims Docker gates that are not the required five")
    return available


def _run(names):
    """Outcomes by test id. Failure text is never returned; only identifiers and verdicts."""
    if not names:
        return {}
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromNames(sorted(names))
    result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
    outcomes = {name: "passed" for name in names}
    for verdict, entries in (("failed", result.failures), ("errored", result.errors),
                             ("skipped", result.skipped), ("unexpected_success", [(test, None) for test in result.unexpectedSuccesses])):
        for test, _ in entries:
            outcomes[test.id()] = verdict
    return outcomes


def assess(*, include_docker=False):
    """Run the mapped tests and report what they establish. Never an authorization."""
    check_mapping()
    started = now()
    selected = set()
    for criterion in CRITERIA:
        selected.update(criterion["offline"])
        if include_docker:
            selected.update(criterion["docker"])
    outcomes = _run(selected)
    criteria, failing = [], []
    for criterion in CRITERIA:
        offline = {name: outcomes[name] for name in criterion["offline"]}
        docker = {name: outcomes.get(name, "not_run") for name in criterion["docker"]}
        bad = sorted(name for name, verdict in {**offline, **docker}.items() if verdict not in ("passed", "not_run"))
        failing.extend(bad)
        deferred = list(criterion["deferred"])
        if criterion["docker"] and not include_docker:
            deferred.insert(0, "The required Docker gates were not run; rerun with the approved image on a qualified host.")
        criteria.append({"id": criterion["id"], "summary": criterion["summary"],
                         "status": "failed" if bad else "covered",
                         "offline_tests": offline, "docker_tests": docker,
                         "other_stages": list(criterion["other_stages"]), "deferred": deferred,
                         "unmet_tests": bad})
    ended = now()
    return {"schema_version": "1.0.0", "kind": "SecurityAcceptance", "started_at": started, "ended_at": ended,
            "criteria_documented": list(documented_ids()), "docker_gates_included": include_docker,
            "tests_run": len(outcomes), "criteria": criteria, "unmet_tests": sorted(failing),
            "status": "failed" if failing else "covered",
            "independent_review": False, "live_authorized": False,
            "meaning": "Each criterion's named tests passed on this host. That is local evidence about this source, "
                       "not an independent security review, a deployment authorization or a claim about a live provider."}
