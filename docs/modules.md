# What each module is for

Sixty modules in one flat package is a lot to scan, and subpackages would be churn with real
risk: `broker.source_digest` names module files as an integrity input, so moving them changes
what a broker reports itself to be. The cheaper answer is a map.

Every line below is the module's own opening sentence, so this cannot drift into describing
something the code stopped doing. `tests/test_module_map.py` fails when a module is added,
removed or reworded without regenerating:

```sh
python scripts/module_map.py
```

| Module | What it is for |
| --- | --- |
| `orch/__main__.py` | Every offline command this control plane offers, and nothing that decides anything. |
| `orch/actors.py` | Replaceable deterministic policy and external-action actors; no state authority. |
| `orch/agent_client.py` | The agent's side of the loopback API: a secret, a socket, and nothing else. |
| `orch/agent_queue.py` | The work an agent could take, which until now it had no way to find. |
| `orch/agent_service.py` | Loopback agent API so agent containers never touch the store. |
| `orch/agent_sessions.py` | A role-scoped, expiring claim that an agent holds across separate invocations. |
| `orch/api.py` | Small localhost HTTP boundary. Session identity never comes from request JSON. |
| `orch/batches.py` | Operator-started, serial fixture batches. Never approve or replay uncertain work. |
| `orch/broker.py` | Single-host broker journal, fenced envelopes and the authenticated loopback service client. |
| `orch/broker_process.py` | Trusted broker execution shared by the short-lived subprocess and the loopback service. |
| `orch/broker_service.py` | Loopback broker service so the host action broker can run under its own OS account. |
| `orch/cli_provider.py` | Provider-neutral JSON adapter; only the bundled fixture transport is admitted. |
| `orch/contracts.py` | Schema validation and canonical, task-bound contract helpers. |
| `orch/copilot.py` | Documented Copilot text protocol preparation; no live executable launcher. |
| `orch/deployment.py` | Deployment gap assessment; this report cannot authorize a live provider. |
| `orch/dispatch.py` | The one place in this control plane that talks to something outside it. |
| `orch/engine.py` | The only writer of workflow state. All actors return evidence to this service. |
| `orch/execution.py` | Fixed-recipe process execution. Local mode is ONLY for trusted test fixtures. |
| `orch/fixtures.py` | Trusted constant fixture inputs, never provider-selected executables. |
| `orch/github_approval.py` | Offline approval-scope preparation; no decisions, credentials or dispatch authority. |
| `orch/github_backup.py` | Verified journal snapshot bundles. No restore, replacement or dispatch authority. |
| `orch/github_bundle.py` | Portable, bounded local evidence; never a workflow restore or dispatch input. |
| `orch/github_bundle_approval.py` | Journal-bound portable review previews; no approval decision or dispatch authority. |
| `orch/github_evidence.py` | Task-owned local artifact byte checks; no semantic approval or execution authority. |
| `orch/github_issues.py` | Offline GitHub issue codec: request data only, never live dispatch. |
| `orch/github_journal.py` | Durable offline GitHub intent/reservation journal, separate from workflow storage. |
| `orch/github_preflight.py` | Compose local review and supplied ref observations; never contact GitHub. |
| `orch/github_preview.py` | Pure GitHub draft-PR preparation. No transport, credentials or approval authority. |
| `orch/github_reads.py` | Offline contract for a future GitHub reader. No sockets, credentials or retries. |
| `orch/github_reconcile.py` | Offline uncertain-result classification; no retry or success authority. |
| `orch/github_recovery_reads.py` | Offline paginated recovery captures, bound to a retained uncertain operation. |
| `orch/github_refs.py` | Compare offline GitHub observations; never authenticate them or dispatch requests. |
| `orch/jira_actions.py` | Bounded Jira REST v2 action codecs; request data only, never live dispatch. |
| `orch/jira_approval.py` | Offline Jira review and freshness diagnostics; no approval admission. |
| `orch/jira_backup.py` | Verified journal snapshot bundles. No restore, replacement or dispatch authority. |
| `orch/jira_journal.py` | Retained offline Jira comment operations. No dispatch or retry authority. |
| `orch/jira_preview.py` | Offline Jira Data Center issue review and explicit comment preparation. |
| `orch/jira_reconcile.py` | Offline comment recovery assessment; never adopt a comment or authorize a retry. |
| `orch/journal_revision.py` | Withdraw a prepared operation so its task can be prepared again, releasing no attempt. |
| `orch/journal_succession.py` | Retire a full integration operation journal into a successor that still refuses a second attempt. |
| `orch/maintenance.py` | Conservative offline lifecycle and verified backup/restore operations. |
| `orch/operator_session.py` | Process-local operator lease. Never persisted or distributed to workers. |
| `orch/pilot.py` | Local Git data-change pilot. Repository programs are never executed. |
| `orch/pilot_backup.py` | Verified pilot journal audits and snapshot bundles. No restore, retry or execution authority. |
| `orch/pilot_retention.py` | Read-only pilot usage and reviewed workspace reclamation. Journal rows are never deleted. |
| `orch/pilot_succession.py` | Retire a full pilot journal and start its successor, without deleting any row. |
| `orch/pilot_worker.py` | Fixed JSON-only Docker worker and ownership-checked container cleanup. |
| `orch/process.py` | Bounded capture for trusted executables; never a general shell interface. |
| `orch/provider.py` | Deterministic proposal-only implementation of the Phase 1 AgentProvider. |
| `orch/provider_fixture.py` | Stdlib-only fixture CLI: no tools, network, workspace editing or database access. |
| `orch/readiness.py` | Portable, fail-closed runtime evidence. Never installs Docker or pulls images. |
| `orch/release.py` | Fixed offline acceptance lanes and portable, non-authorizing evidence. |
| `orch/repository_tasks.py` | Task-kernel admission for the bounded pilot; no provider or remote action authority. |
| `orch/runtime_checks.py` | Machine-readable execution of the mandatory Docker gates, isolated from the API. |
| `orch/security_acceptance.py` | Executable mapping from the mandatory security criteria to the tests that exercise them. |
| `orch/skill.py` | Publish the agent protocol as plain context, in whatever shape an agent tool reads. |
| `orch/status.py` | What is happening right now, for the person who turned the containers on. |
| `orch/storage.py` | SQLite records/operations and content-addressed artifacts; no workflow logic. |
| `orch/task_drafts.py` | The project manager's step, which until now the kernel performed on their behalf. |
| `orch/task_issue.py` | The GitHub issue a confirmed specification implies, derived rather than written. |
