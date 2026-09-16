import argparse
import json
from pathlib import Path

from .api import serve
from .engine import Engine
from .execution import Executor
from .provider import Scenario
from .provider import MockProvider
from .cli_provider import FixtureCliProvider


def main():
    parser = argparse.ArgumentParser(description="Offline orchd.ai fixture workflow")
    parser.add_argument("command", choices=["broker-serve", "broker-check", "broker-rotate-secret", "broker-fetch", "repository-task-create", "repository-task-run", "repository-task-approve", "agent-serve", "agent-claim", "agent-renew", "agent-release", "agent-sessions", "skill-export", "task-open", "spec-draft", "spec-ask", "spec-answer", "spec-confirm", "spec-show", "pilot-reconcile", "pilot-usage", "pilot-reclaim", "pilot-journal-audit", "pilot-journal-retire", "pilot-journal-chain", "pilot-journal-backup", "pilot-verify-journal-backup", "pilot-compare-journal-backup", "pilot-prepare", "pilot-inspect", "pilot-run", "pilot-abandon", "demo", "serve", "history", "backup", "verify-backup", "restore", "gc", "storage-usage", "audit", "recover", "retry-cleanup", "cancel", "renew-approval", "doctor", "verify-runtime", "provider-check", "deployment-check", "github-preview", "github-issue-read-plan", "github-issue-preview", "github-issue-reconcile", "github-issue-duplicate-plan", "github-issue-duplicates", "github-check-refs", "github-reconcile", "github-stage", "github-inspect", "github-reconcile-journal", "github-journal-usage", "github-journal-audit", "github-journal-retire", "github-journal-chain", "github-journal-upgrade", "github-journal-withdraw", "github-journal-backup", "github-verify-journal-backup", "github-compare-journal-backup", "github-journal-recovery-drill", "github-approval-preview", "github-check-approval", "github-check-evidence", "github-assess-approval", "github-check-evidence-claims", "github-check-record-claims", "github-list-evidence-records", "github-resolve-evidence-selection", "github-export-evidence-bundle", "github-verify-evidence-bundle", "github-bundle-approval-preview", "github-assess-bundle-approval", "github-ref-observation-snapshot", "github-preflight", "github-ref-read-plan", "github-ref-transcript-snapshot", "github-recovery-read-plan", "github-reconcile-transcript", "jira-review-issue", "jira-comment-preview", "jira-comment-read-plan", "jira-reconcile-comments", "jira-stage", "jira-inspect", "jira-journal-usage", "jira-journal-audit", "jira-journal-retire", "jira-journal-chain", "jira-journal-upgrade", "jira-journal-withdraw", "jira-journal-read-plan", "jira-reconcile-journal", "jira-journal-backup", "jira-verify-journal-backup", "jira-compare-journal-backup", "jira-journal-recovery-drill", "jira-action-read-plan", "jira-action-preview", "jira-stage-action", "jira-approval-preview", "jira-check-approval", "jira-preflight-read-plan", "jira-preflight", "jira-deployment-check", "verify-release", "check-release", "batch-create", "batch-inspect", "batch-list", "batch-run", "batch-abandon"])
    parser.add_argument("--data", default=".runtime/phase2")
    parser.add_argument("--trusted-fixture", action="store_true", help="Local fixed test programs only; NOT a sandbox")
    parser.add_argument("--fixture-provider", choices=["in-process", "cli"], default="in-process", help="Offline provider fixture transport")
    parser.add_argument("--image", help="Preloaded digest-pinned Python image for Docker")
    parser.add_argument("--task")
    parser.add_argument("--operation-id", help="Operation to inspect or retry workspace cleanup for")
    parser.add_argument("--request-id", help="Expired pending approval request ID")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--destination")
    parser.add_argument("--dry-run", action="store_true", help="For GC and pilot-reclaim, report eligible files without deleting them")
    parser.add_argument("--expected-revision", type=int)
    parser.add_argument("--reason", help="One-line audited operator reason for cancel or a live pilot-reclaim (1–500 characters)")
    parser.add_argument("--provider", choices=["github_copilot_cli", "abc_binary_ai_placeholder"])
    parser.add_argument("--executable", help="Absolute provider executable path for read-only inventory")
    parser.add_argument("--expected-sha256", help="Expected executable, operation scope, preview or evidence bundle digest")
    parser.add_argument("--runtime-report", help="Existing runtime evidence to revalidate for deployment-check; may query Docker")
    parser.add_argument("--intent", help="Local JSON intent for offline GitHub or Jira preparation")
    parser.add_argument("--allow-repository", help="Exact owner/name allowed for the GitHub preview")
    parser.add_argument("--repository-id", type=int, help="Expected numeric GitHub repository identity")
    parser.add_argument("--observations", help="Offline JSON observations for GitHub or Jira assessment")
    parser.add_argument("--journal", help="Separate local operation journal database for the selected integration")
    parser.add_argument("--records", help="Explicit task-owned patch/test/review/policy document references")
    parser.add_argument("--evidence", help="Offline approval evidence digest envelope for the selected integration")
    parser.add_argument("--approval-preview", help="Saved GitHub or Jira approval preview JSON")
    parser.add_argument("--bundle", help="Portable offline GitHub evidence bundle file")
    parser.add_argument("--expected-bundle-sha256", help="Expected whole-file digest for journal-bound bundle review")
    parser.add_argument("--expected-observations-sha256", help="Expected ref observation snapshot binding digest")
    parser.add_argument("--observed-at", help="Supplied observation time in UTC, such as 2026-01-01T00:00:00Z")
    parser.add_argument('--transcript', help='Offline JSON response capture for the selected GitHub or Jira read plan')
    parser.add_argument('--jira-target', help='Explicit Jira Data Center instance/project/issue target JSON')
    parser.add_argument('--jira-author-key', help='Explicit expected Jira comment author key; never inferred from captured comments')
    parser.add_argument('--jira-profile', help='Explicit Jira deployment mapping JSON; no credentials')
    parser.add_argument('--github-profile', help='Explicit GitHub issue destination JSON: repository and its numeric ID; no credentials')
    parser.add_argument('--terms', help='GitHub issue search terms for a duplicate read plan')
    parser.add_argument('--release-lane', choices=['offline', 'browser', 'full'], default='full', help='Exact required release acceptance lane')
    parser.add_argument('--release-report', help='Saved release verification report; requires its independently retained digest')
    parser.add_argument('--batch-id', help='Retained serial workflow batch identifier')
    parser.add_argument('--after-batch', help='Exclusive batch-list cursor from its previous next value')
    parser.add_argument('--expected-snapshot-sha256', help='Reviewed current task snapshot digest for batch abandonment')
    parser.add_argument("--pilot-id", help="Local repository pilot identifier")
    parser.add_argument("--pilot-executor", choices=["local-data", "docker"], default="local-data", help="Explicit pilot execution profile")
    parser.add_argument("--broker-endpoint", help="Loopback broker service endpoint 127.0.0.1:PORT, run under a separate OS account")
    parser.add_argument("--broker-secret", help="Private file holding the 64-hex shared secret of the broker service")
    parser.add_argument("--broker-root", help="Broker-owned journal directory for broker-serve")
    parser.add_argument("--workspaces", help="Orchestrator workspace parent directory that broker-serve may execute in")
    parser.add_argument("--dispatch-credential", help="Private origin= and token= file the broker alone reads for outbound dispatch; never given to an agent")
    parser.add_argument("--plan", help="A read plan JSON file whose reads broker-fetch performs")
    parser.add_argument("--pilot-root", help="Pilot journal directory whose Docker workers broker-serve may run; requires --image")
    parser.add_argument("--complete", action="store_true", help="For broker-rotate-secret, drop the previous secret after every process has re-read the file")
    parser.add_argument("--successor", help="New journal that continues a retired one: a directory for pilots, a database file for integrations")
    parser.add_argument("--agent", help="Name of the agent holding an agent session")
    parser.add_argument("--agents", help="Registry directory of agent role and secret files for agent-serve")
    parser.add_argument("--agent-endpoint", help="127.0.0.1:PORT of an agent service to call instead of opening the store")
    parser.add_argument("--agent-secret", help="This agent's secret file, for calling an agent service")
    parser.add_argument("--role", help="Role an agent session claims work as")
    parser.add_argument("--lease", type=int, help="Seconds an agent session lasts before it must be renewed")
    parser.add_argument("--to", help="Directory skill-export writes the agent protocol into")
    parser.add_argument("--layout", choices=["plain", "claude", "copilot"], default="plain",
                        help="Where skill-export places the protocol: beside the destination, under .claude/skills, or with a Copilot instructions pointer")
    parser.add_argument("--check", action="store_true", help="For skill-export, report whether a vendored copy is current without writing anything")
    parser.add_argument("--from", dest="source", help="JSON file holding a draft specification, questions or answers")
    parser.add_argument("--principal", help="Who a human boundary crossing is attributed to")
    parser.add_argument("--spec-sha256", help="The sha256 of the exact specification being confirmed")
    parser.add_argument("--title", default="Repository JSON validation")
    parser.add_argument("--decision", choices=["approve", "reject"])
    args = parser.parse_args()
    if args.command not in ("broker-serve", "broker-rotate-secret") and bool(args.broker_endpoint) != bool(args.broker_secret):
        parser.error("--broker-endpoint and --broker-secret are required together")
    broker_service = {"endpoint": args.broker_endpoint, "secret": args.broker_secret} if args.broker_endpoint else None
    if args.command == "broker-serve":
        if not (args.broker_root and args.broker_secret and args.workspaces):
            parser.error("broker-serve requires --broker-root, --broker-secret and --workspaces")
        if not args.trusted_fixture and not args.image:
            parser.error("broker-serve requires --trusted-fixture or a digest-pinned --image")
        from .broker_service import BrokerService
        from .contracts import Rejected
        try:
            service = BrokerService(args.broker_root, args.broker_secret, args.workspaces,
                                    "trusted-fixture" if args.trusted_fixture else "docker", args.image, args.port,
                                    args.pilot_root, args.dispatch_credential)
        except (Rejected, OSError):
            parser.error("Broker service rejected its configuration; check the journal, secret, workspace and profile")
        print(f"Broker service: http://127.0.0.1:{service.port}", flush=True)
        service.serve_forever()
        return
    if args.command == "broker-fetch":
        if not (args.plan and args.broker_endpoint and args.broker_secret):
            parser.error("broker-fetch requires --plan, --broker-endpoint and --broker-secret")
        from .broker import ServiceClient, VERSION as BROKER_VERSION
        from .github_preview import load_intent
        from .contracts import Rejected, canonical, uid
        try:
            plan = load_intent(args.plan)
            client = ServiceClient({"endpoint": args.broker_endpoint, "secret": args.broker_secret})
            reply = client.call("fetch", {"schema_version": BROKER_VERSION, "kind": "fetch", "nonce": uid(),
                                          "plan_sha256": plan["sha256"], "requests": plan["binding"]["requests"]})
        except (Rejected, OSError, KeyError, TypeError):
            parser.error("Broker fetch refused; check the plan, the endpoint, the secret and the broker's dispatch credential")
        # The capture alone, so it can be handed straight to the codec that asked for it.
        print(canonical(reply["capture"]).decode())
        return
    if args.command == "broker-rotate-secret":
        if not args.broker_secret:
            parser.error("broker-rotate-secret requires --broker-secret")
        from .broker import rotate_secret
        from .contracts import Rejected
        try:
            report = rotate_secret(args.broker_secret, complete=args.complete)
        except (Rejected, OSError):
            parser.error("Secret rotation refused; check the file's contents, links, permissions and current rotation stage")
        print(json.dumps(report, indent=2))
        return
    if args.command == "broker-check":
        if not broker_service:
            parser.error("broker-check requires --broker-endpoint and --broker-secret")
        from .broker import assess_identity
        from .contracts import Rejected
        try:
            report = assess_identity(broker_service)
        except (Rejected, OSError):
            parser.error("Broker service unreachable, unauthenticated or misconfigured")
        print(json.dumps(report, indent=2))
        raise SystemExit(0 if report["status"] == "separate" else 2)  # Shared identity is a blocked gate.
    if args.command.startswith('repository-task-'):
        from .contracts import Rejected
        from .github_preview import load_intent
        import sqlite3
        if args.command == 'repository-task-create' and not args.intent: parser.error('Repository creation requires --intent')
        if args.command != 'repository-task-create' and not args.task: parser.error('Repository operation requires --task')
        if args.command == 'repository-task-approve' and (not args.request_id or not args.decision or args.expected_revision is None):
            parser.error('Repository approval requires --request-id, --decision and --expected-revision')
        engine = None
        try:
            engine = Engine(args.data, Executor('trusted-fixture' if args.trusted_fixture else 'docker', args.image), broker_service=broker_service)
            if args.command == 'repository-task-create':
                task = engine.create_repository_task(load_intent(args.intent), args.title)
                result = engine.task(task)
            else:
                if engine.repository_tasks(args.task) is None: raise Rejected('Not a repository task')
                result = engine.run(args.task) if args.command == 'repository-task-run' else engine.approve(args.task, args.request_id, args.decision, args.expected_revision)
        except (Rejected, OSError, sqlite3.Error): parser.error('Repository task rejected; inspect current scope, state and approval')
        finally:
            if engine is not None: engine.close()
        print(json.dumps(result, indent=2))
        return
    if args.command in ('pilot-journal-retire', 'pilot-journal-chain'):
        from .contracts import Rejected, canonical
        from .pilot_succession import chain, retire
        import sqlite3
        retiring = args.command == 'pilot-journal-retire'
        if retiring and not (args.destination and args.expected_sha256 and args.successor and args.reason):
            parser.error('Retirement requires a verified --destination snapshot, its --expected-sha256, a --successor and an audited --reason')
        if not (Path(args.data) / 'pilot.sqlite').is_file():
            parser.error('Existing pilot journal required')
        try:
            report = (retire(args.data, args.destination, args.expected_sha256, args.successor, args.reason)
                      if retiring else chain(args.data))
        except (Rejected, OSError, sqlite3.Error):
            parser.error('Pilot journal succession rejected; check the snapshot digest, successor path, settled pilots and audited reason')
        print(canonical(report).decode())
        return
    if args.command in ('pilot-journal-audit', 'pilot-journal-backup', 'pilot-verify-journal-backup', 'pilot-compare-journal-backup'):
        from .contracts import Rejected, canonical
        from .pilot_backup import audit_journal, backup_journal, compare_journal_backup, verify_journal_backup
        import sqlite3
        auditing = args.command == 'pilot-journal-audit'
        verifying = args.command == 'pilot-verify-journal-backup'
        if not auditing and not args.destination:
            parser.error('Pilot journal backup commands require --destination')
        if verifying and not args.expected_sha256:
            parser.error('Pilot journal backup verification requires --expected-sha256')
        if args.command == 'pilot-compare-journal-backup' and not args.expected_sha256:
            parser.error('Pilot journal backup comparison requires --expected-sha256')
        if not auditing and not verifying and not (Path(args.data) / 'pilot.sqlite').is_file():
            parser.error('Existing pilot journal required')
        try:
            if auditing:
                report = audit_journal(args.data)
            elif args.command == 'pilot-journal-backup':
                report = backup_journal(args.data, args.destination)
            elif verifying:
                report = verify_journal_backup(args.destination, args.expected_sha256)
            else:
                report = compare_journal_backup(args.data, args.destination, args.expected_sha256)
        except (Rejected, OSError, sqlite3.Error):
            parser.error('Pilot journal backup operation rejected; check the journal, destination, digest and bundle integrity')
        print(canonical(report).decode())
        if report.get('status') == 'different':
            raise SystemExit(2)
        return
    if args.command.startswith('pilot-'):
        from .pilot import Pilot
        from .contracts import Rejected
        from .github_preview import load_intent
        import sqlite3
        if args.command == 'pilot-prepare' and not args.intent:
            parser.error('pilot-prepare requires --intent')
        if args.command not in ('pilot-prepare', 'pilot-usage') and not args.pilot_id:
            parser.error('Pilot operation requires --pilot-id')
        if args.command in ('pilot-run', 'pilot-abandon', 'pilot-reconcile', 'pilot-reclaim') and (not args.expected_sha256 or args.expected_revision is None):
            parser.error('Pilot mutation requires reviewed --expected-sha256 and --expected-revision')
        if args.command == 'pilot-reclaim' and not args.dry_run and args.reason is None:
            parser.error('A live pilot-reclaim requires an audited --reason; --dry-run does not')
        pilot = None
        try:
            if args.command != 'pilot-prepare' and not (Path(args.data) / 'pilot.sqlite').is_file():
                raise Rejected('Existing pilot journal required')
            from . import pilot_retention
            pilot = Pilot(args.data, read_only=args.command in ('pilot-inspect', 'pilot-usage'), executor=args.pilot_executor, image=args.image,
                          broker_service=broker_service)
            if args.command == 'pilot-prepare': report = pilot.prepare(load_intent(args.intent))
            elif args.command == 'pilot-inspect': report = pilot.inspect(args.pilot_id)
            elif args.command == 'pilot-run': report = pilot.run(args.pilot_id, args.expected_sha256, args.expected_revision)
            elif args.command == 'pilot-reconcile': report = pilot.reconcile(args.pilot_id, args.expected_sha256, args.expected_revision)
            elif args.command == 'pilot-usage': report = pilot_retention.usage(pilot)
            elif args.command == 'pilot-reclaim': report = pilot_retention.reclaim(pilot, args.pilot_id, args.expected_sha256, args.expected_revision, dry_run=args.dry_run, reason=args.reason)
            else: report = pilot.abandon(args.pilot_id, args.expected_sha256, args.expected_revision)
        except (Rejected, OSError, ValueError, sqlite3.Error):
            parser.error('Pilot rejected; inspect intent, baseline, scope, revision, retained recovery state and reclamation eligibility')
        finally:
            if pilot is not None: pilot.close()
        print(json.dumps(report, indent=2))
        return
    if args.command.startswith('batch-'):
        from .batches import Batches, REVIEW_STATES
        from .contracts import Rejected, canonical
        from .github_preview import load_intent
        import sqlite3
        if args.command == 'batch-create' and not args.intent:
            parser.error('batch-create requires an explicit --intent task list')
        if args.command not in ('batch-create', 'batch-list') and not args.batch_id:
            parser.error('Batch operation requires --batch-id')
        if args.command in ('batch-run', 'batch-abandon') and (not args.expected_sha256 or args.expected_revision is None):
            parser.error('Batch mutation requires reviewed scope --expected-sha256 and journal --expected-revision')
        if args.command == 'batch-abandon' and not (args.task and args.expected_snapshot_sha256):
            parser.error('Batch abandonment requires --task and reviewed --expected-snapshot-sha256')
        if not (Path(args.data) / 'orch.sqlite').is_file():
            parser.error('Batch operations require an existing workflow store')
        engine = None
        try:
            # Inspection opens a read-only store, without broker setup.
            if args.command in ('batch-inspect', 'batch-list'):
                from .storage import Store
                from types import SimpleNamespace
                store = Store(args.data, read_only=True)
                try:
                    inspector = SimpleNamespace(store=store, task=lambda task: dict(zip(('state', 'context'), store.task(task))))
                    report = Batches(inspector).list(args.after_batch) if args.command == 'batch-list' else Batches(inspector).inspect(args.batch_id)
                finally: store.close()
            else:
                engine = Engine(args.data, Executor('trusted-fixture' if args.trusted_fixture else 'docker', args.image),
                                provider_factory=FixtureCliProvider if args.fixture_provider == 'cli' else MockProvider, broker_service=broker_service)
                batches = Batches(engine)
                if args.command == 'batch-create': report = batches.create(load_intent(args.intent))
                elif args.command == 'batch-run': report = batches.run(args.batch_id, args.expected_sha256, args.expected_revision)
                else: report = batches.abandon(args.batch_id, args.expected_sha256, args.expected_revision, args.task, args.expected_snapshot_sha256)
        except (Rejected, OSError, sqlite3.Error):
            parser.error('Batch rejected; inspect scope, revisions, current task evidence and recovery state')
        finally:
            if engine is not None: engine.close()
        print(canonical(report).decode())
        if args.command == 'batch-run' and any(e['status'] == 'running' or (e['status'] == 'stopped' and e['result'] in REVIEW_STATES) for e in report['entries']):
            raise SystemExit(2)
        return
    if args.command in ('verify-release', 'check-release'):
        from .contracts import Rejected, canonical
        from .release import verify_release, check_release
        import sys
        if args.command == 'verify-release' and not args.destination:
            parser.error('verify-release requires a new --destination')
        if args.command == 'check-release' and not (args.release_report and args.expected_sha256):
            parser.error('check-release requires --release-report and --expected-sha256')
        try:
            report = (verify_release(args.destination, args.release_lane,
                                    progress=lambda stage: print('Release check: ' + stage, file=sys.stderr, flush=True))
                      if args.command == 'verify-release' else
                      check_release(args.release_report, args.expected_sha256, args.release_lane))
        except (Rejected, OSError):
            parser.error('Release verification rejected; check report path, lane, digest, source and expiry')
        print(canonical(report).decode())
        if report['status'] == 'failed': raise SystemExit(2)
        return
    if args.command in ('jira-action-read-plan', 'jira-action-preview', 'jira-stage-action', 'jira-deployment-check'):
        staging = args.command == 'jira-stage-action'
        if not args.jira_profile or (args.command != 'jira-deployment-check' and not args.intent):
            parser.error('Jira actions require --jira-profile and, except deployment checks, --intent')
        if args.command in ('jira-action-preview', 'jira-stage-action') and not args.transcript:
            parser.error('Jira action preparation requires --transcript')
        if staging and not all((args.journal, args.expected_sha256)):
            parser.error('Jira staging requires --journal and expected action preview --expected-sha256')
        from .jira_actions import read_plan, preview_action, action_scope
        from .jira_approval import deployment_check
        from .jira_journal import JiraJournal
        from .github_preview import load_intent
        from .contracts import Rejected, canonical
        import sqlite3
        journal = None
        try:
            profile = load_intent(args.jira_profile)
            if args.command == 'jira-deployment-check':
                report = deployment_check(profile)
            else:
                intent = load_intent(args.intent)
                if args.command == 'jira-action-read-plan':
                    report = read_plan(intent, profile)
                else:
                    capture = load_intent(args.transcript)
                    if staging:
                        action_scope(intent, profile, capture, args.expected_sha256)
                        journal = JiraJournal(args.journal)
                        report = journal.stage_action(intent, profile, capture, args.expected_sha256)
                    else:
                        report = preview_action(intent, profile, capture)
        except (Rejected, OSError, sqlite3.Error):
            parser.error('Offline Jira action rejected; check explicit mappings, capture, digest and journal')
        finally:
            if journal is not None: journal.close()
        print(canonical(report).decode())
        if report.get('status') == 'blocked': raise SystemExit(2)
        return
    if args.command in ('jira-approval-preview', 'jira-check-approval', 'jira-preflight-read-plan', 'jira-preflight'):
        preparing = args.command == 'jira-approval-preview'
        if not args.journal or not args.expected_sha256:
            parser.error('Jira review requires --journal and --expected-sha256')
        if preparing and not all((args.operation_id, args.evidence, args.jira_author_key)):
            parser.error('Jira approval preview requires operation ID, evidence and expected author key')
        if not preparing and not args.approval_preview:
            parser.error('Jira review requires --approval-preview')
        if args.command in ('jira-check-approval', 'jira-preflight') and not args.evidence:
            parser.error('Jira review requires --evidence')
        if args.command == 'jira-preflight' and not all((args.transcript, args.observed_at)):
            parser.error('Jira preflight requires --transcript and --observed-at')
        if any(value is not None for value in (args.intent, args.jira_profile, args.jira_target, args.observations)):
            parser.error('Jira review uses retained scope; overrides are not accepted')
        from .jira_approval import prepare_approval, check_approval, preflight_plan, assess_preflight
        from .jira_journal import JiraJournal
        from .github_preview import load_intent
        from .contracts import Rejected, canonical
        import sqlite3
        journal = None
        try:
            evidence = load_intent(args.evidence) if args.evidence else None
            journal = JiraJournal(args.journal, read_only=True)
            if preparing:
                report = prepare_approval(journal, args.operation_id, args.expected_sha256, evidence, args.jira_author_key)
            else:
                preview = load_intent(args.approval_preview)
                if args.command == 'jira-check-approval':
                    report = check_approval(journal, preview, args.expected_sha256, evidence)
                elif args.command == 'jira-preflight-read-plan':
                    report = preflight_plan(journal, preview, args.expected_sha256)
                else:
                    report = assess_preflight(journal, preview, args.expected_sha256, evidence,
                                              load_intent(args.transcript), args.observed_at)
        except (Rejected, OSError, sqlite3.Error):
            parser.error('Offline Jira review rejected; check retained scope, review digest, capture and evidence')
        finally:
            if journal is not None: journal.close()
        print(canonical(report).decode())
        if report.get('status') == 'blocked': raise SystemExit(2)
        return
    if args.command in ('task-open', 'spec-draft', 'spec-ask', 'spec-answer', 'spec-confirm', 'spec-show'):
        from .contracts import Rejected, canonical
        from . import task_drafts
        import sqlite3
        if args.command != 'task-open' and not args.task:
            parser.error('That command needs the --task it acts on')
        if args.command in ('spec-draft', 'spec-ask', 'spec-answer') and not args.source:
            parser.error('That command reads its content from a --from JSON file')
        if args.command in ('spec-answer', 'spec-confirm') and not args.principal:
            parser.error('A human boundary crossing needs the --principal it is attributed to')
        if args.command == 'spec-confirm' and not args.spec_sha256:
            parser.error('spec-confirm needs the --spec-sha256 of the specification you read')
        content = None
        if args.source:
            try:
                content = json.loads(Path(args.source).read_text(encoding='utf-8'))
            except (OSError, ValueError):
                parser.error('Could not read a JSON document from --from')
        engine = None
        try:
            engine = Engine(args.data, Executor('trusted-fixture' if args.trusted_fixture else 'docker', args.image))
            if args.command == 'task-open':
                task = engine.create_task(args.title, draft=True)
                report = task_drafts.show(engine, task)
            elif args.command == 'spec-draft':
                report = task_drafts.draft(engine, args.task, content, args.expected_revision)
            elif args.command == 'spec-ask':
                report = task_drafts.ask(engine, args.task, content, args.expected_revision)
            elif args.command == 'spec-answer':
                report = task_drafts.answer(engine, args.task, content, args.principal, args.expected_revision)
            elif args.command == 'spec-confirm':
                report = task_drafts.confirm(engine, args.task, args.spec_sha256, args.principal, args.expected_revision)
            else:
                report = task_drafts.show(engine, args.task)
        except (Rejected, OSError, sqlite3.Error) as exc:
            parser.error('Specification step refused: ' + str(exc))
        finally:
            if engine is not None: engine.close()
        print(canonical(report).decode())
        return
    if args.command == 'skill-export':
        from .contracts import Rejected, canonical
        from .skill import export
        if not args.to:
            parser.error('skill-export requires the --to directory it writes into')
        try:
            report = export(args.to, args.layout, args.check)
        except (Rejected, OSError) as exc:
            parser.error('Skill export refused: ' + str(exc))
        print(canonical(report).decode())
        if report['status'] == 'stale': raise SystemExit(2)
        return
    if args.command == 'agent-serve':
        from .agent_service import AgentService
        from .contracts import Rejected
        import sqlite3
        if not args.agents:
            parser.error('agent-serve requires an --agents registry directory')
        try:
            service = AgentService(args.data, args.agents, args.port or 0)
        except (Rejected, OSError, sqlite3.Error):
            parser.error('Agent service refused to start; check the store, the registry directory and each agent role and secret file')
        print(json.dumps({'kind': 'AgentServiceStarted', 'endpoint': '127.0.0.1:' + str(service.port),
                          'agents': sorted(service.agents), 'live_authorized': False}), flush=True)
        try:
            service.serve_forever()
        except KeyboardInterrupt:
            service.close()
        return
    if args.command in ('agent-claim', 'agent-renew', 'agent-release', 'agent-sessions'):
        from .agent_sessions import claim, release, renew, sessions
        from .contracts import Rejected, canonical
        import sqlite3
        listing = args.command == 'agent-sessions'
        through_service = bool(args.agent_endpoint or args.agent_secret)
        if not listing and not (args.task and (args.agent or through_service)):
            parser.error('Agent sessions require --task, and --agent unless a service names the agent by its secret')
        if args.command == 'agent-claim' and not args.role:
            parser.error('Claiming requires the --role this agent works as')
        if args.agent_endpoint or args.agent_secret:
            # Through the service: the agent never opens the store, and the secret it
            # signs with is what decides which agent it is.
            from .agent_client import AgentClient
            if not (args.agent_endpoint and args.agent_secret):
                parser.error('Calling an agent service needs both --agent-endpoint and --agent-secret')
            try:
                client = AgentClient(args.agent_endpoint, args.agent_secret)
                if listing:
                    report = client.sessions()
                elif args.command == 'agent-claim':
                    report = client.claim(args.task, args.role, args.lease, args.expected_revision)
                elif args.command == 'agent-renew':
                    report = client.renew(args.task, args.lease)
                else:
                    report = client.release(args.task)
            except (Rejected, OSError):
                parser.error('Agent service rejected the request; check the endpoint, the secret, the role this agent may claim and the task state')
            print(canonical(report).decode())
            return
        engine = None
        try:
            engine = Engine(args.data, Executor('trusted-fixture' if args.trusted_fixture else 'docker', args.image))
            if listing:
                report = sessions(engine.store)
            elif args.command == 'agent-claim':
                report = claim(engine, args.task, args.agent, args.role, args.lease, args.expected_revision)
            elif args.command == 'agent-renew':
                report = renew(engine, args.task, args.agent, args.lease)
            else:
                report = release(engine, args.task, args.agent)
        except (Rejected, OSError, sqlite3.Error):
            parser.error('Agent session rejected; check the task state, the role its next step needs, the holder and the lease')
        finally:
            if engine is not None: engine.close()
        print(canonical(report).decode())
        return
    if args.command in ('github-journal-upgrade', 'github-journal-withdraw',
                        'jira-journal-upgrade', 'jira-journal-withdraw'):
        from .contracts import Rejected, canonical
        from .journal_revision import upgrade, withdraw
        from .journal_succession import family
        import sqlite3
        label = 'GitHub' if args.command.startswith('github-') else 'Jira'
        upgrading = args.command.endswith('-upgrade')
        if not args.journal:
            parser.error('Journal revision commands require --journal')
        if upgrading and not (args.destination and args.expected_sha256):
            parser.error('Upgrading requires a verified --destination snapshot and its --expected-sha256')
        if not upgrading and not (args.operation_id and args.expected_sha256 and args.reason):
            parser.error('Withdrawal requires --operation-id, the retained scope --expected-sha256 and an audited --reason')
        if not Path(args.journal).is_file():
            parser.error('An existing journal is required')
        journal = None
        try:
            if upgrading:
                report = upgrade(args.journal, args.destination, args.expected_sha256, label)
            else:
                journal = family(label)[0](args.journal)
                report = withdraw(journal, args.operation_id, args.expected_sha256, args.reason)
        except (Rejected, OSError, sqlite3.Error):
            parser.error('Journal revision rejected; check the snapshot digest, schema version, retained scope digest, operation state and audited reason')
        finally:
            if journal is not None:
                journal.close()
        print(canonical(report).decode())
        return
    if args.command in ('github-journal-retire', 'github-journal-chain',
                        'jira-journal-retire', 'jira-journal-chain'):
        from .contracts import Rejected, canonical
        from .journal_succession import chain, retire
        import sqlite3
        label = 'GitHub' if args.command.startswith('github-') else 'Jira'
        retiring = args.command.endswith('-retire')
        if not args.journal:
            parser.error('Journal succession commands require --journal')
        if retiring and not (args.destination and args.expected_sha256 and args.successor and args.reason):
            parser.error('Retirement requires a verified --destination snapshot, its --expected-sha256, a --successor journal and an audited --reason')
        if not Path(args.journal).is_file():
            parser.error('An existing journal is required')
        try:
            report = (retire(args.journal, args.destination, args.expected_sha256, args.successor, args.reason, label)
                      if retiring else chain(args.journal, label))
        except (Rejected, OSError, sqlite3.Error):
            parser.error('Journal succession rejected; check the snapshot digest, successor path, chain links and audited reason')
        print(canonical(report).decode())
        return
    if args.command in ('jira-journal-backup', 'jira-verify-journal-backup',
                        'jira-compare-journal-backup', 'jira-journal-recovery-drill'):
        creating = args.command == 'jira-journal-backup'
        comparing = args.command == 'jira-compare-journal-backup'
        if not args.destination or ((creating or comparing) and not args.journal):
            parser.error('Jira backup commands require --destination; creation and comparison also require --journal')
        if not creating and not args.expected_sha256:
            parser.error('Jira backup verification, comparison and drills require --expected-sha256')
        if not creating and not comparing and args.journal:
            parser.error('Jira verification and recovery drills accept no active --journal')
        if any(value is not None for value in (args.intent, args.jira_profile, args.jira_target, args.observations, args.jira_author_key, args.transcript)):
            parser.error('Jira backup commands use retained records; scope overrides are not accepted')
        from .jira_backup import backup_journal, verify_journal_backup, compare_journal_backup, drill_journal_backup
        from .contracts import Rejected, canonical
        import sqlite3
        try:
            if creating:
                report = backup_journal(args.journal, args.destination)
            elif comparing:
                report = compare_journal_backup(args.journal, args.destination, args.expected_sha256)
            elif args.command == 'jira-journal-recovery-drill':
                report = drill_journal_backup(args.destination, args.expected_sha256)
            else:
                report = verify_journal_backup(args.destination, args.expected_sha256)
        except (Rejected, OSError, sqlite3.Error):
            parser.error('Jira backup operation rejected; check digest, bundle integrity, paths and retained journal')
        print(canonical(report).decode())
        if report.get('status') == 'different':
            raise SystemExit(2)
        return
    if args.command in ('jira-stage', 'jira-inspect', 'jira-journal-usage', 'jira-journal-audit',
                        'jira-journal-read-plan', 'jira-reconcile-journal'):
        staging = args.command == 'jira-stage'
        recovery = args.command in ('jira-journal-read-plan', 'jira-reconcile-journal')
        if not args.journal:
            parser.error('Jira journal commands require --journal')
        if staging:
            if not all((args.intent, args.jira_target, args.observations, args.expected_sha256, args.jira_author_key)):
                parser.error('Jira staging requires intent, target, issue observations, expected preview digest and author key')
        else:
            if any(value is not None for value in (args.intent, args.jira_profile, args.jira_target, args.observations, args.jira_author_key)):
                parser.error('Jira journal reads use retained scope; input overrides are not accepted')
            if (recovery or args.command == 'jira-inspect') and not args.operation_id:
                parser.error('Jira inspection and recovery require --operation-id')
            if recovery and not args.expected_sha256:
                parser.error('Jira recovery requires --expected-sha256 for the retained scope')
            if args.command == 'jira-reconcile-journal' and not args.transcript:
                parser.error('Jira reconciliation requires --transcript')
        from .jira_journal import JiraJournal, bound_scope
        from .github_preview import load_intent
        from .contracts import Rejected, canonical
        import sqlite3
        journal = None
        try:
            if staging:
                values = (load_intent(args.intent), load_intent(args.jira_target), load_intent(args.observations),
                          args.expected_sha256, args.jira_author_key)
                bound_scope(*values)  # Reject invalid input before creating any journal directory/file.
            capture = load_intent(args.transcript) if args.command == 'jira-reconcile-journal' else None
            journal = JiraJournal(args.journal, read_only=not staging)
            if staging:
                report = journal.stage(*values)
            elif args.command == 'jira-inspect':
                report = journal.inspect(args.operation_id)
            elif args.command == 'jira-journal-usage':
                report = journal.usage()
            elif args.command == 'jira-journal-audit':
                report = journal.audit()
            elif args.command == 'jira-journal-read-plan':
                report = journal.recovery_plan(args.operation_id, args.expected_sha256)
            else:
                report = journal.reconcile(args.operation_id, args.expected_sha256, capture)
        except (Rejected, OSError, sqlite3.Error):
            parser.error('Jira journal operation rejected; check scope digest, retained state, capture, capacity and integrity')
        finally:
            if journal is not None:
                journal.close()
        print(canonical(report).decode())
        if report.get('status') == 'unresolved':
            raise SystemExit(2)
        return
    if args.command in ('jira-comment-read-plan', 'jira-reconcile-comments'):
        assessing = args.command == 'jira-reconcile-comments'
        if not all((args.jira_target, args.observations, args.intent, args.expected_sha256, args.jira_author_key)) or (assessing and not args.transcript):
            parser.error('Jira comment recovery requires target, issue observations, intent, expected preview digest, author key and, for assessment, transcript')
        from .jira_reconcile import prepare_comment_read_plan, assess_comments
        from .github_preview import load_intent
        from .contracts import Rejected, canonical
        try:
            values = (load_intent(args.intent), load_intent(args.jira_target), load_intent(args.observations), args.expected_sha256, args.jira_author_key)
            report = assess_comments(*values, load_intent(args.transcript)) if assessing else prepare_comment_read_plan(*values)
        except (Rejected, OSError):
            parser.error('Offline Jira recovery rejected; check retained preview, author and captured pages')
        print(canonical(report).decode())
        if report.get('status') == 'unresolved':
            raise SystemExit(2)
        return
    if args.command in ('jira-review-issue', 'jira-comment-preview'):
        commenting = args.command == 'jira-comment-preview'
        if not args.jira_target or not args.observations or (commenting and not args.intent):
            parser.error('Jira review requires --jira-target and --observations; comment preview also requires --intent')
        from .jira_preview import review_issue, prepare_comment
        from .github_preview import load_intent
        from .contracts import Rejected, canonical
        try:
            target, observations = load_intent(args.jira_target), load_intent(args.observations)
            report = prepare_comment(load_intent(args.intent), target, observations) if commenting else review_issue(target, observations)
        except (Rejected, OSError):
            parser.error('Offline Jira request rejected; check explicit target, saved issue and comment intent')
        print(canonical(report).decode())
        if report.get('status') == 'blocked':
            raise SystemExit(2)
        return
    if args.command in ('github-recovery-read-plan', 'github-reconcile-transcript'):
        importing = args.command == 'github-reconcile-transcript'
        if not args.journal or not args.operation_id or not args.expected_sha256 or (importing and not args.transcript):
            parser.error('Recovery reads require --journal, --operation-id, --expected-sha256 and, for assessment, --transcript')
        if any(value is not None for value in (args.intent, args.allow_repository, args.repository_id)):
            parser.error('Recovery uses retained scope; repository and intent overrides are not accepted')
        from .github_recovery_reads import prepare_recovery_plan, reconcile_transcript
        from .github_journal import GitHubJournal
        from .github_preview import load_intent
        from .contracts import Rejected, canonical
        import sqlite3
        journal = None
        try:
            journal = GitHubJournal(args.journal, read_only=True)
            report = (reconcile_transcript(journal, args.operation_id, args.expected_sha256, load_intent(args.transcript))
                      if importing else prepare_recovery_plan(journal, args.operation_id, args.expected_sha256))
        except (Rejected, OSError, sqlite3.Error):
            parser.error('Recovery read contract rejected; check transcript and retained reservation')
        finally:
            if journal is not None:
                journal.close()
        print(canonical(report).decode())
        if report.get('status') == 'unresolved':
            raise SystemExit(2)
        return
    if args.command in ('github-ref-read-plan', 'github-ref-transcript-snapshot'):
        importing = args.command == 'github-ref-transcript-snapshot'
        if not args.journal or not args.operation_id or not args.expected_sha256:
            parser.error('Read plan requires --journal, --operation-id and --expected-sha256')
        if importing and (not args.transcript or not args.observed_at):
            parser.error('Transcript snapshot requires --transcript and --observed-at')
        from .github_reads import prepare_read_plan, snapshot_from_transcript
        from .github_journal import GitHubJournal
        from .github_preview import load_intent
        from .contracts import Rejected, canonical
        import sqlite3
        journal = None
        try:
            journal = GitHubJournal(args.journal, read_only=True)
            if importing:
                report = snapshot_from_transcript(journal, args.operation_id, args.expected_sha256,
                                                  load_intent(args.transcript), args.observed_at)
            else:
                report = prepare_read_plan(journal, args.operation_id, args.expected_sha256)
        except (Rejected, OSError, sqlite3.Error):
            parser.error('Offline read contract rejected; check transcript, digest, time and prepared journal scope')
        finally:
            if journal is not None:
                journal.close()
        print(canonical(report).decode())
        return
    if args.command in ("github-ref-observation-snapshot", "github-preflight"):
        preparing = args.command == "github-ref-observation-snapshot"
        if not args.journal or not args.expected_sha256 or not args.observations:
            parser.error("Offline preflight requires --journal, --expected-sha256 and --observations")
        if preparing and (not args.operation_id or not args.observed_at):
            parser.error("Observation snapshot requires --operation-id and --observed-at")
        if not preparing and (not args.approval_preview or not args.bundle or not args.expected_bundle_sha256 or not args.expected_observations_sha256):
            parser.error("Preflight requires --approval-preview, --bundle, --expected-bundle-sha256 and --expected-observations-sha256")
        from .github_preflight import prepare_observation_snapshot, assess_preflight
        from .github_journal import GitHubJournal
        from .github_preview import load_intent
        from .contracts import Rejected, canonical
        import sqlite3
        journal = None
        try:
            journal = GitHubJournal(args.journal, read_only=True)
            if preparing:
                report = prepare_observation_snapshot(journal, args.operation_id, args.expected_sha256, load_intent(args.observations), args.observed_at)
            else:
                report = assess_preflight(journal, load_intent(args.approval_preview), args.expected_sha256,
                                          args.bundle, args.expected_bundle_sha256, args.observations, args.expected_observations_sha256)
        except (Rejected, OSError, sqlite3.Error):
            parser.error("Offline preflight failed; check inputs, expected digests, timestamps and journal scope")
        finally:
            if journal is not None:
                journal.close()
        print(canonical(report).decode() if preparing else json.dumps(report, indent=2))
        if report.get('status') == 'blocked':
            raise SystemExit(2)
        return
    if args.command in ("github-bundle-approval-preview", "github-assess-bundle-approval"):
        if not args.journal or not args.bundle or not args.expected_bundle_sha256 or not args.expected_sha256:
            parser.error("Bundle review requires --journal, --bundle, --expected-bundle-sha256 and --expected-sha256")
        preparing = args.command == "github-bundle-approval-preview"
        if (preparing and not args.operation_id) or (not preparing and not args.approval_preview):
            parser.error("Bundle preview requires --operation-id; assessment requires --approval-preview")
        from .github_bundle_approval import prepare_bundle_approval, assess_bundle_approval
        from .github_journal import GitHubJournal
        from .github_preview import load_intent
        from .contracts import Rejected
        import sqlite3
        journal = None
        try:
            journal = GitHubJournal(args.journal, read_only=True)
            if preparing:
                report = prepare_bundle_approval(journal, args.operation_id, args.expected_sha256, args.bundle, args.expected_bundle_sha256)
            else:
                preview = load_intent(args.approval_preview)
                report = assess_bundle_approval(journal, preview, args.expected_sha256, args.bundle, args.expected_bundle_sha256)
        except (Rejected, OSError, sqlite3.Error):
            parser.error("Bundle review failed; check bundle, preview, expected digests and journal scope")
        finally:
            if journal is not None:
                journal.close()
        print(json.dumps(report, indent=2))
        if report.get('status') == 'blocked':
            raise SystemExit(2)
        return
    if args.command in ("github-export-evidence-bundle", "github-verify-evidence-bundle"):
        from .github_bundle import export_bundle, verify_bundle
        from .github_preview import load_intent
        from .storage import Store
        from .contracts import Rejected
        import sqlite3
        store = None
        try:
            if args.command == "github-export-evidence-bundle":
                if not args.records or not args.destination:
                    parser.error("Bundle export requires --records, --destination and existing --data")
                selection = load_intent(args.records)
                store = Store(args.data, read_only=True)
                report = export_bundle(store, selection, args.destination)
            else:
                if not args.bundle or not args.expected_sha256:
                    parser.error("Bundle verification requires --bundle and --expected-sha256")
                report = verify_bundle(args.bundle, args.expected_sha256)
        except (Rejected, OSError, sqlite3.Error):
            parser.error("Evidence bundle failed; check selection, contents, digest, limits and destination")
        finally:
            if store is not None:
                store.close()
        print(json.dumps(report, indent=2))
        if report['status'] == 'blocked':
            raise SystemExit(2)
        return
    if args.command == "github-resolve-evidence-selection":
        if not args.records:
            parser.error("github-resolve-evidence-selection requires --records and existing --data")
        from .github_evidence import resolve_record_selection
        from .github_preview import load_intent
        from .storage import Store
        from .contracts import Rejected
        import sqlite3
        store = None
        try:
            anchors = load_intent(args.records)
            store = Store(args.data, read_only=True)
            selection = resolve_record_selection(store, anchors)
        except (Rejected, OSError, sqlite3.Error):
            parser.error("Cannot resolve evidence selection; check chosen records and their links")
        finally:
            if store is not None:
                store.close()
        print(json.dumps(selection, indent=2))
        return
    if args.command == "github-list-evidence-records":
        if not args.task:
            parser.error("github-list-evidence-records requires --task and existing --data")
        from .github_evidence import list_evidence_records
        from .storage import Store
        from .contracts import Rejected
        import sqlite3
        store = None
        try:
            store = Store(args.data, read_only=True)
            report = list_evidence_records(store, args.task)
        except (Rejected, OSError, sqlite3.Error):
            parser.error("Cannot catalog evidence records; check task, storage and record limits")
        finally:
            if store is not None:
                store.close()
        print(json.dumps(report, indent=2))
        return
    if args.command == "github-assess-approval":
        if not args.journal or not args.approval_preview or not args.expected_sha256 or not args.evidence:
            parser.error("github-assess-approval requires --journal, --approval-preview, --expected-sha256, --evidence and existing workflow --data")
        from .github_evidence import assess_approval_evidence
        from .github_journal import GitHubJournal
        from .github_preview import load_intent
        from .storage import Store
        from .contracts import Rejected
        import sqlite3
        journal = store = None
        try:
            preview, evidence = load_intent(args.approval_preview), load_intent(args.evidence)
            journal = GitHubJournal(args.journal, read_only=True)
            store = Store(args.data, read_only=True)
            report = assess_approval_evidence(journal, store, preview, args.expected_sha256, evidence)
        except (Rejected, OSError, sqlite3.Error):
            parser.error("Combined approval assessment failed; check preview, evidence, journal and workflow storage")
        finally:
            if store is not None:
                store.close()
            if journal is not None:
                journal.close()
        print(json.dumps(report, indent=2))
        raise SystemExit(0 if report['status'] == 'current' else 2)
    if args.command in ("github-check-evidence", "github-check-evidence-claims", "github-check-record-claims"):
        record_mode = args.command == "github-check-record-claims"
        source = args.records if record_mode else args.evidence
        if not source:
            parser.error("Evidence checks require --records for record claims or --evidence for artifact claims, and existing --data")
        from .github_evidence import check_evidence, check_evidence_contents, check_record_claims
        from .github_preview import load_intent
        from .storage import Store
        from .contracts import Rejected
        import sqlite3
        store = None
        try:
            evidence = load_intent(source)
            store = Store(args.data, read_only=True)
            check = check_record_claims if record_mode else check_evidence_contents if args.command == "github-check-evidence-claims" else check_evidence
            report = check(store, evidence)
        except (Rejected, OSError, sqlite3.Error):
            parser.error("Evidence check failed; check task ownership, metadata, artifact bytes and existing storage")
        finally:
            if store is not None:
                store.close()
        print(json.dumps(report, indent=2))
        if report['status'] == 'blocked':
            raise SystemExit(2)
        return
    if args.command == "github-check-approval":
        if not args.journal or not args.approval_preview or not args.expected_sha256 or not args.evidence:
            parser.error("github-check-approval requires --journal, --approval-preview, --expected-sha256 and --evidence")
        from .github_approval import check_approval
        from .github_journal import GitHubJournal
        from .github_preview import load_intent
        from .contracts import Rejected
        import sqlite3
        journal = None
        try:
            preview, evidence = load_intent(args.approval_preview), load_intent(args.evidence)
            journal = GitHubJournal(args.journal, read_only=True)
            report = check_approval(journal, preview, args.expected_sha256, evidence)
        except (Rejected, OSError, sqlite3.Error):
            parser.error("Cannot check GitHub approval; check saved preview, digest, evidence and journal integrity")
        finally:
            if journal is not None:
                journal.close()
        print(json.dumps(report, indent=2))
        raise SystemExit(0 if report['status'] == 'current' else 2)
    if args.command == "github-approval-preview":
        if not args.journal or not args.operation_id or not args.expected_sha256 or not args.evidence:
            parser.error("github-approval-preview requires --journal, --operation-id, --expected-sha256 and --evidence")
        from .github_approval import prepare_approval
        from .github_journal import GitHubJournal
        from .github_preview import load_intent
        from .contracts import Rejected
        import sqlite3
        journal = None
        try:
            evidence = load_intent(args.evidence)
            journal = GitHubJournal(args.journal, read_only=True)
            report = prepare_approval(journal, args.operation_id, args.expected_sha256, evidence)
        except (Rejected, OSError, sqlite3.Error):
            parser.error("Cannot prepare GitHub approval preview; check scope, evidence and journal integrity")
        finally:
            if journal is not None:
                journal.close()
        print(json.dumps(report, indent=2))
        return
    if args.command == "github-journal-recovery-drill":
        if not args.destination or not args.expected_sha256 or args.journal:
            parser.error("Recovery drill requires --destination and --expected-sha256, and accepts no active --journal")
        from .github_backup import drill_journal_backup
        from .contracts import Rejected
        import sqlite3
        try:
            report = drill_journal_backup(args.destination, args.expected_sha256)
        except (Rejected, OSError, sqlite3.Error):
            parser.error("Journal recovery drill failed; check bundle integrity, digest and scratch storage")
        print(json.dumps(report, indent=2))
        return
    if args.command == "github-compare-journal-backup":
        if not args.journal or not args.destination or not args.expected_sha256:
            parser.error("github-compare-journal-backup requires --journal, --destination and --expected-sha256")
        from .github_backup import compare_journal_backup
        from .contracts import Rejected
        import sqlite3
        try:
            report = compare_journal_backup(args.journal, args.destination, args.expected_sha256)
        except (Rejected, OSError, sqlite3.Error):
            parser.error("Cannot compare journal backup; check bundle digest, integrity and journal availability")
        print(json.dumps(report, indent=2))
        raise SystemExit(0 if report['status'] == 'matches' else 2)
    if args.command in ("github-journal-backup", "github-verify-journal-backup"):
        if not args.destination or (args.command == "github-journal-backup" and not args.journal):
            parser.error("Journal backups require --destination; creation also requires --journal")
        from .github_backup import backup_journal, verify_journal_backup
        from .contracts import Rejected
        import sqlite3
        try:
            report = backup_journal(args.journal, args.destination) if args.command == "github-journal-backup" else verify_journal_backup(args.destination)
        except (Rejected, OSError, sqlite3.Error):
            parser.error("Journal backup failed; check source integrity, budgets and destination; incomplete output is not a verified backup")
        print(json.dumps(report, indent=2))
        return
    if args.command == "github-reconcile-journal":
        if not args.journal or not args.operation_id or not args.expected_sha256 or not args.observations:
            parser.error("github-reconcile-journal requires --journal, --operation-id, --expected-sha256 and --observations")
        if args.intent or args.allow_repository or args.repository_id is not None:
            parser.error("Journal reconciliation uses retained scope; intent and repository overrides are not accepted")
        from .github_journal import GitHubJournal
        from .github_preview import load_intent
        from .contracts import Rejected
        import sqlite3
        journal = None
        try:
            observations = load_intent(args.observations)
            journal = GitHubJournal(args.journal, read_only=True)
            report = journal.reconcile(args.operation_id, args.expected_sha256, observations)
        except (Rejected, OSError, sqlite3.Error):
            parser.error("Cannot reconcile GitHub journal; check retained reservation, expected digest and observations")
        finally:
            if journal is not None:
                journal.close()
        print(json.dumps(report, indent=2))
        raise SystemExit(0 if report['status'] == 'candidate_observed' else 2)
    if args.command in ("github-inspect", "github-journal-usage", "github-journal-audit"):
        if not args.journal or (args.command == "github-inspect" and not args.operation_id):
            parser.error("Journal reads require --journal; github-inspect also requires --operation-id")
        from .github_journal import GitHubJournal
        from .contracts import Rejected
        import sqlite3
        journal = None
        try:
            journal = GitHubJournal(args.journal, read_only=True)
            report = journal.audit() if args.command == "github-journal-audit" else journal.usage() if args.command == "github-journal-usage" else journal.get(args.operation_id)
            print(json.dumps(report, indent=2))
        except (Rejected, OSError, sqlite3.Error):
            parser.error("Cannot inspect GitHub operation; check existing journal, identity and record integrity")
        finally:
            if journal is not None:
                journal.close()
        return
    if args.command == "github-stage":
        if not args.intent or not args.allow_repository or args.repository_id is None or not args.journal:
            parser.error("github-stage requires --intent, --allow-repository, --repository-id and --journal")
        from .github_preview import load_intent
        from .github_journal import GitHubJournal, bound_scope
        from .contracts import Rejected
        import sqlite3
        journal = None
        try:
            intent = load_intent(args.intent)
            bound_scope(intent, args.allow_repository, args.repository_id)
            journal = GitHubJournal(args.journal)
            print(json.dumps(journal.stage(intent, args.allow_repository, args.repository_id), indent=2))
        except (Rejected, OSError, sqlite3.Error):
            parser.error("Cannot stage GitHub intent; check scope, journal identity, capacity and availability")
        finally:
            if journal is not None:
                journal.close()
        return
    if args.command in ("github-check-refs", "github-reconcile"):
        if not args.intent or not args.allow_repository or args.repository_id is None or not args.observations:
            parser.error(args.command + " requires --intent, --allow-repository, --repository-id and --observations")
        from .github_preview import load_intent
        from .github_refs import assess_refs
        from .github_reconcile import reconcile_pull_request
        from .contracts import Rejected
        try:
            assess = assess_refs if args.command == "github-check-refs" else reconcile_pull_request
            report = assess(load_intent(args.intent), args.allow_repository, args.repository_id, load_intent(args.observations))
        except Rejected:
            parser.error("Invalid GitHub intent, identity or observations")
        print(json.dumps(report, indent=2))
        raise SystemExit(0 if report["status"] in ("matches", "candidate_observed") else 2)
    if args.command in ('github-issue-read-plan', 'github-issue-preview', 'github-issue-reconcile',
                        'github-issue-duplicate-plan', 'github-issue-duplicates'):
        searching = args.command in ('github-issue-duplicate-plan', 'github-issue-duplicates')
        if not args.github_profile:
            parser.error('GitHub issue commands require the --github-profile naming the destination')
        if searching and not args.terms:
            parser.error('A duplicate search requires the --terms to look for')
        if not searching and not args.intent:
            parser.error('That command requires the --intent it acts on')
        if args.command in ('github-issue-preview', 'github-issue-reconcile', 'github-issue-duplicates') and not args.transcript:
            parser.error('That command requires the --transcript of the reads its plan asked for')
        from . import github_issues
        from .github_preview import load_intent
        from .contracts import Rejected
        try:
            profile = load_intent(args.github_profile)
            if searching:
                plan = github_issues.duplicate_plan(profile, args.terms)
                report = plan if args.command == 'github-issue-duplicate-plan' else github_issues.duplicates(
                    plan, load_intent(args.transcript), profile)
            else:
                intent = load_intent(args.intent)
                if args.command == 'github-issue-read-plan':
                    report = github_issues.read_plan(intent, profile)
                elif args.command == 'github-issue-preview':
                    report = github_issues.preview_issue(intent, profile, load_intent(args.transcript))
                else:
                    report = github_issues.reconcile_issue(intent, profile, load_intent(args.transcript))
        except (Rejected, OSError):
            parser.error('Offline GitHub issue step rejected; check the profile, intent, capture and its binding')
        print(json.dumps(report, indent=2))
        return  # Success means data was prepared or read back, never a remote effect.
    if args.command == "github-preview":
        if not args.intent or not args.allow_repository:
            parser.error("github-preview requires --intent and --allow-repository")
        from .github_preview import load_intent, prepare_pull_request
        from .contracts import Rejected
        try:
            preview = prepare_pull_request(load_intent(args.intent), args.allow_repository)
        except Rejected:
            parser.error("GitHub intent rejected; check its schema, scope and content")
        print(json.dumps(preview, indent=2))
        return  # Success means a preview was prepared, never a remote effect.
    if args.command == "verify-backup":
        if args.destination or args.task:
            parser.error("verify-backup checks the whole backup and accepts no destination or task")
        from .maintenance import verify_backup
        report = verify_backup(args.data)
        print(json.dumps(report, indent=2))
        raise SystemExit(0 if report["status"] == "passed" else 2)
    if args.command == "retry-cleanup":
        if not args.task or not args.operation_id or args.expected_revision is None:
            parser.error("retry-cleanup requires --task, --operation-id and --expected-revision")
        from .maintenance import plain
        if not plain(Path(args.data).absolute() / "orch.sqlite").is_file():
            parser.error("retry-cleanup requires an existing workflow database")
    if args.command in ("storage-usage", "audit"):
        from .maintenance import plain, storage_usage, integrity_report
        from .storage import Store
        root = plain(Path(args.data).absolute())
        if not plain(root / "orch.sqlite").is_file():
            parser.error(args.command + " requires an existing workflow database")
        if args.command == "audit" and args.task:
            parser.error("audit checks the complete store; --task is not supported")
        store = Store(root, read_only=True)
        try:
            report = integrity_report(store) if args.command == "audit" else storage_usage(store, args.task)
            print(json.dumps(report, indent=2))
            if args.command == "audit" and report["status"] != "passed":
                raise SystemExit(2)
        finally:
            store.close()
        return
    if args.command == "renew-approval" and (not args.task or not args.request_id or args.expected_revision is None):
        parser.error("renew-approval requires --task, --request-id and --expected-revision")
    if args.command == "renew-approval" and not (Path(args.data) / "orch.sqlite").is_file():
        parser.error("renew-approval requires an existing workflow database")
    if args.command in ("backup", "restore") and not args.destination:
        parser.error(args.command + " requires --destination")
    if args.command in ("backup", "gc"):
        from .maintenance import plain
        if not plain(Path(args.data).absolute() / "orch.sqlite").is_file():
            parser.error(args.command + " requires an existing workflow database")
    if args.command == "cancel" and (not args.task or args.expected_revision is None or args.reason is None):
        parser.error("cancel requires --task, --expected-revision and --reason")
    if args.command == "cancel" and not (Path(args.data) / "orch.sqlite").is_file():
        parser.error("cancel requires an existing workflow database")
    if args.command == "deployment-check":
        if not args.provider:
            parser.error("deployment-check requires --provider")
        if args.runtime_report and not args.image:
            parser.error("--runtime-report requires --image")
        from .deployment import deployment_check
        print(json.dumps(deployment_check(args.provider, args.executable, args.expected_sha256, args.image, args.runtime_report, broker_service), indent=2))
        raise SystemExit(2)  # An assessment is never live-provider admission.
    if args.command == "provider-check":
        if not args.provider:
            parser.error("provider-check requires --provider")
        from .copilot import provider_check
        print(json.dumps(provider_check(args.provider, args.executable, args.expected_sha256), indent=2))
        raise SystemExit(2)  # No live provider is admitted in this milestone.
    if args.command == "doctor":
        from .readiness import doctor
        report = doctor(args.image)
        print(json.dumps(report, indent=2))
        raise SystemExit(0 if report["status"] == "ready" else 2)
    if args.command == "verify-runtime":
        if not args.image or not args.destination:
            parser.error("verify-runtime requires --image and --destination")
        from .readiness import verify_runtime
        report = verify_runtime(args.image, args.destination)
        print(json.dumps(report, indent=2))
        raise SystemExit(0 if report["status"] == "passed" else 2)
    if args.command == "restore":
        from .maintenance import restore
        print(json.dumps(restore(args.data, args.destination)))
        return
    if args.command in ("backup", "gc"):
        from .storage import Store
        from .maintenance import backup, collect_orphans
        store = Store(args.data, read_only=True)
        try:
            print(json.dumps(backup(store, args.destination) if args.command == "backup" else collect_orphans(store, dry_run=args.dry_run)))
        finally:
            store.close()
        return
    engine = Engine(args.data, Executor("trusted-fixture" if args.trusted_fixture or args.command in ("cancel", "renew-approval", "retry-cleanup") else "docker", args.image), provider_factory=FixtureCliProvider if args.fixture_provider == "cli" else MockProvider, broker_service=broker_service)
    try:
        if args.command == "serve":
            serve(engine, args.port)
        elif args.command == "history":
            print(json.dumps([{ "event": e, "payload": json.loads(engine.store.read_artifact(e["payload"], args.task))} for e in engine.events(args.task)], indent=2))
        elif args.command == "recover":
            print(json.dumps(engine.recover(args.task, args.expected_revision)))
        elif args.command == "retry-cleanup":
            print(json.dumps(engine.retry_cleanup(args.task, args.operation_id, args.expected_revision)))
        elif args.command == "cancel":
            print(json.dumps(engine.cancel(args.task, args.expected_revision, args.reason)))
        elif args.command == "renew-approval":
            print(json.dumps(engine.renew_approval(args.task, args.request_id, args.expected_revision)))
        else:
            task = engine.create_task(scenario=Scenario(reviews=("NEEDS_CHANGES", "ACCEPT")))
            engine.run(task)
            print("Paused:", engine.task(task)["state"]["state"])
            # Explicit demo script represents the two human boundary calls; run() never approves.
            for _ in range(2):
                state = engine.task(task)["state"]
                engine.approve(task, state["pending_approval"]["id"], "approve", state["revision"])
                engine.run(task)
                print("State:", engine.task(task)["state"]["state"])
            print("Task:", task)
            print("Events:", len(engine.events(task)))
            print("Data:", engine.store.root)
            print("Simulation:", engine.task(task)["context"]["external"])
    finally:
        engine.close()


if __name__ == "__main__":
    main()
