# Consolidated offline Jira integration

This package completes the bounded offline Jira action set in one delivery. It includes
comment preparation/recovery, explicit Epic lookup, Epic/Story/Task creation, first Epic
attachment, issue links, status transitions and GitHub Issue/PR associations, with durable
scope, review/preflight diagnostics, backups and recovery drills. It makes no network calls.

**Phase 8 is not production-complete.** Live transport, deployment conformance, credential
isolation, authenticated approval admission, workflow evidence verification and authoritative
remote reconciliation remain blocked. They require the actual corporate deployment and
independent review. No configuration or successful offline report turns live access on.
This is the remaining offline package, not a promise to support every Jira feature.

## Capability and boundary matrix

| Capability | Implemented behavior | Deliberate boundary |
| --- | --- | --- |
| Issue/Epic lookup | Exact project, issue ID/key and configured issue-type checks | Explicit identity; no broad search, inferred adoption or automatic task intake |
| Epic/Story/Task creation | Metadata-bound POST preview; operation label; optional verified Epic relationship | No subtask, arbitrary custom fields, nested Epic or automatic creation |
| Status transition | Explicit source/destination status and available transition ID | No required transition-screen fields or self-transitions |
| Epic attachment | PUT preview for a configured Story/Task and verified Epic | First attachment only; no reparenting or removal |
| Jira issue link | Explicit type and direction, both endpoints verified | Same configured project; no self-link or implicit duplicate |
| GitHub association | Remote-link preview restricted to configured `github.com/owner/repo` | Issue/PR URL only; no arbitrary host, update or overwrite |
| Comments | Existing exact text, author and visibility binding | Existing comment codec and pagination limits remain |
| Persistence | All actions use the existing immutable journal and one-use reservation | One operation per local task, not a multi-action scheduler |
| Human review | Expiring preview and evidence-digest comparison | Review request only; no authenticated approval or evidence truth claim |
| Preflight | Freshness, refreshed context, author and permissions, final journal recheck | Supplied captures are unauthenticated; result never authorizes dispatch |
| Recovery | Operation-specific candidate assessment and backup drills | Candidates are observations, not proof of causality or delivery |
| Deployment | Explicit mapping validation and blocking-gate report | No live credentials, transport, restore admission or retries |

The stable journal schema and legacy comment records remain supported. New action records
use a distinct `JiraActionScope` shape which is fully reconstructed during reads. Existing
inspection, audit, capacity, reservation, backup and recovery commands apply to both shapes.
The one-operation-per-task restriction prevents a new operation ID bypassing a consumed
slot. A future multi-action workflow needs an explicit ordering/revision/admission contract;
these APIs do not invent tasks or claim the referenced workflow task exists.

## Explicit deployment profile

Start with [the synthetic profile](../examples/jira/profile.json). Its fields are closed:

| Field | Meaning |
| --- | --- |
| `schema_version` | `1.0.0` |
| `codec` | `jira-rest-8.5`, an explicit historical REST v2 codec reference |
| `base_url` | Exact HTTPS instance and optional context path, without credentials/query/fragment |
| `project_id`, `project_key` | Expected numeric project ID and uppercase key |
| `issue_types` | Distinct configured numeric IDs for `epic`, `story`, `task` |
| `epic_name_field`, `epic_link_field` | Distinct deployment-specific `customfield_N` IDs |
| `github_repository` | One allowed GitHub owner/repository |

The IDs in examples are fictional. Field names are not used to discover or guess mappings.
The codec uses Atlassian's [Jira REST v2 reference](https://docs.atlassian.com/software/jira/docs/api/REST/8.5.1/)
for create metadata, transitions, issue links and remote links, and the documented
[server REST examples](https://developer.atlassian.com/server/jira/platform/jira-rest-api-examples/)
for explicit issue/custom-field payloads. The codec label is not evidence of compatibility
with a newer or customized Data Center deployment. Capture shape and field support must
be reviewed against the deployed version before a future live adapter is admitted.

## Action contract and preparation

Every action intent contains exactly `schema_version`, `task_id`, `operation_id`, `action`,
`issue`, and `parameters`. IDs are lowercase 32-character hex strings. `issue` is null for
creation and otherwise exactly `{ "id": "200", "key": "ENG-7" }`. The profile binds its project.

| Action | Exact parameter fields |
| --- | --- |
| `create_issue` | `issue_type` (`epic`, `story`, `task`), `summary`, `description`, `epic_name`, `epic` |
| `transition` | `transition_id`, `from_status_id`, `to_status_id` |
| `epic_link` | `epic` (ID/key), `expected_epic_key` (must be null) |
| `issue_link` | `other_issue` (ID/key), `link_type_id`, `direction` (`inward` or `outward`) |
| `github_link` | `kind` (`pull` or `issues`), `number` (positive integer), `title` |

For creation, `epic_name` is required only for Epics and null for other types. `epic` is
null or an explicit existing Epic, and must be null when creating an Epic. Summary and
Epic name are limited to 255 UTF-8 bytes, description to 4 KiB. Proposal text rejects
recognized secret patterns and unsafe control characters; this is not comprehensive DLP.
Creation adds `orchd-op-OPERATION_ID` as the sole proposed label for bounded later lookup.

```sh
python -m orch jira-action-read-plan --jira-profile examples/jira/profile.json --intent examples/jira/create_issue-intent.json
python -m orch jira-action-preview --jira-profile examples/jira/profile.json --intent examples/jira/create_issue-intent.json --transcript examples/jira/create_issue-capture.json
python -m orch jira-stage-action --journal .runtime/jira.sqlite --jira-profile examples/jira/profile.json --intent examples/jira/create_issue-intent.json --transcript examples/jira/create_issue-capture.json --expected-sha256 ACTION_PREVIEW_SHA256
```

Equivalent intent/capture examples exist for all five action types. Review the exact write
request in `JiraActionPreview` before staging. Staging requires that preview's digest.
The saved record's `binding.sha256` is the **scope digest** used for journal operations;
its outer `sha256` identifies the inspection report. These digests are not interchangeable.

A read plan derives every URL. A capture has exactly `schema_version: "1.0.0"`,
`plan_sha256`, and `responses`; response keys must exactly match the plan's `requests`.
Each response has only `url`, `redirected: false`, integer `status: 200`, and `body`.
Redirects, failed reads, changed URLs, missing or extra envelopes and inputs over 64 KiB
are rejected. Bodies can contain additional server fields, still covered by the capture digest.

Creation checks the exact returned project and issue type, required fields, set operations,
and supported field schemas. Configured Epic fields must also match Jira's expected custom
field plugin types; an arbitrary text field is not treated as an Epic relationship. An
unknown required field is rejected rather than omitted.
Epic attachment also requires captured edit metadata for the configured field. Transition
metadata must expose the exact target with no required screen fields. Issue links verify
both endpoints and reject existing relationships. GitHub associations reject an existing
URL or operation global ID because Jira's remote-link POST can otherwise update a link.

## Approval preview and preflight

An evidence envelope contains exactly `task_id`, `review_sha256`, `policy_sha256`. These
are supplied references; this module neither reads those artifacts nor verifies their
contents or authors. Policy/evidence verification remains a live-admission gate.

```sh
python -m orch jira-approval-preview --journal .runtime/jira.sqlite --operation-id OPERATION_ID --expected-sha256 SCOPE_SHA256 --evidence jira-evidence.json --jira-author-key EXPECTED_USER_KEY
python -m orch jira-check-approval --journal .runtime/jira.sqlite --approval-preview jira-approval.json --expected-sha256 APPROVAL_SHA256 --evidence jira-evidence.json
python -m orch jira-preflight-read-plan --journal .runtime/jira.sqlite --approval-preview jira-approval.json --expected-sha256 APPROVAL_SHA256
python -m orch jira-preflight --journal .runtime/jira.sqlite --approval-preview jira-approval.json --expected-sha256 APPROVAL_SHA256 --evidence jira-evidence.json --transcript jira-preflight-capture.json --observed-at 2026-09-14T12:00:00Z
```

These read-only commands accept no replacement intent/profile/target. Approval previews
bind the prepared scope, exact action preview, expected author, evidence and a 15-minute
validity window. Existing comment scopes additionally require the retained author key.
The preflight plan refreshes original context and derives `myself` and scoped
`mypermissions` requests. It requires the active expected account, `BROWSE_PROJECTS` and
the action-specific permission. Permission responses are still supplied data, not authority.

Capture timestamps must be UTC, no earlier than review issuance, no later than the current
clock, and less than five minutes old. Refresh must reconstruct the exact original preview;
context or metadata drift blocks the result. A final check outside the refresh transaction
observes a subsequently consumed reservation and rechecks expiry. This does not eliminate
a race after the report is returned; a future live broker needs atomic admission.

Reports use `current` or `blocked` and retain `evidence_verified: false`,
`live_authorized: false`, `retry_allowed: false`, `remote_effect_confirmed: false`.
Invalid input or an already-invalid scope required to build a read plan exits 2 without
partial stdout. Blocked assessments exit 2 with JSON. No review command reserves a slot.

## Action-specific recovery and storage

Use the existing `jira-journal-read-plan` and `jira-reconcile-journal` commands after an
internal reservation becomes uncertain. Their nested plans now select the action codec.
Creation searches for the exact project and operation label, with at most two returned
issues; missing pages, duplicate IDs or multiple matching results remain unresolved.
Matching requires the expected issue type, text, labels and configured Epic fields.
Transition recovery checks destination status; Epic attachment checks the parent key;
links check direction/type/endpoints or the exact remote association payload.

An observed state may predate the operation or come from another actor. A matching label
or global ID is not trusted provenance. Empty results never authorize repeating a write.
Unsupported/malformed captures reject; valid captures without a unique match remain
unresolved. Recovery never changes journal state or marks an operation delivered.

All journal [capacity and integrity rules](jira-journal.md) and
[backup/drill safeguards](jira-backups.md) remain. New scopes retain original unredacted
captures and proposal text. Keep journals, bundles and temporary storage under approved
host access controls. No encryption or secure deletion is provided. Actual restoration
and monotonic admission require a separately approved operator recovery policy.

## Reproducible acceptance and remaining deployment work

```sh
python scripts/jira_acceptance.py
python scripts/jira_acceptance.py --destination .runtime/jira-acceptance-new
python -m orch jira-deployment-check --jira-profile examples/jira/profile.json
```

The acceptance script uses synthetic responses with socket/process launch blocked. It
prepares, stages, reviews, preflights, reserves and reconciles all five new action types,
then verifies a backup, compares it and runs a recovery drill. Optional output must be a
new directory and includes all reviewable JSON artifacts and the fixture database. Without
an output directory, it uses disposable storage. The dependency on test fixture helpers
makes this a repository acceptance tool, not a deployed production command.

The deployment report always remains blocked. To progress to live use, supply exact
deployment/version and mapping evidence, least-privilege service identity and revocation
requirements, trusted TLS transport and credential isolation, independently reviewed
containment, authenticated human approval with verified workflow evidence, and an
authoritative reconciliation/restore policy. The current CLI does not accept credentials.
The management UI and workflow engine continue their established fixture behavior; these
Jira commands do not imply a live UI integration or automatic project-manager execution.
