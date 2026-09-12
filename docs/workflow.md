# Persisted workflow specification

Every transition below requires matching task revision, authenticated actor,
schema-valid evidence, same-task references and a transactional append-only event.
Unlisted edges are forbidden. Timeouts never imply human approval.

| From | To | Trigger and additional guard |
| --- | --- | --- |
| DRAFT_SPEC | AWAITING_CLARIFICATION | Lead emits explicit unanswered questions |
| DRAFT_SPEC | SPEC_READY | Human confirms exact spec hash; clarification unnecessary |
| AWAITING_CLARIFICATION | DRAFT_SPEC | Human answers; produce new spec revision for confirmation |
| SPEC_READY | PLANNING | Dispatch distinct planning invocation |
| PLANNING | AWAITING_PLAN_APPROVAL | Valid immutable plan bound to confirmed spec/base commit |
| AWAITING_PLAN_APPROVAL | READY_FOR_IMPLEMENTATION | Authenticated human approves exact spec/plan/base/policy scope |
| AWAITING_PLAN_APPROVAL | PLANNING | Human rejects plan, records reasons |
| READY_FOR_IMPLEMENTATION | IMPLEMENTING | Unexpired approval; broker creates isolated attempt and WorkOrder |
| IMPLEMENTING | TESTING | Frozen broker-computed patch within approved paths/budget |
| IMPLEMENTING | CHANGES_REQUESTED | Implementation invalid or outside scope; retain failed attempt |
| TESTING | REVIEWING | All required trusted test receipts pass for exact frozen patch |
| TESTING | CHANGES_REQUESTED | Test failure, timeout, missing receipt or invalid evidence |
| REVIEWING | AWAITING_ACTION_APPROVAL | Independent reviewer ACCEPT; deterministic scope/tests still pass |
| REVIEWING | CHANGES_REQUESTED | Reviewer REJECT or NEEDS_CHANGES, with findings |
| CHANGES_REQUESTED | READY_FOR_IMPLEMENTATION | Bounded retry within identical approved scope; fresh WorkOrder/attempt |
| CHANGES_REQUESTED | PLANNING | Plan change required; invalidate plan/action approvals |
| CHANGES_REQUESTED | DRAFT_SPEC | Requirements change; reconfirm spec and invalidate dependent approvals |
| AWAITING_ACTION_APPROVAL | READY_FOR_PR | Human approves exact simulated-PR payload and evidence hashes |
| AWAITING_ACTION_APPROVAL | CHANGES_REQUESTED | Human rejects action with remediation reason |
| READY_FOR_PR | PR_CREATED | Successful idempotent action receipt; simulated=true for first slice |
| PR_CREATED | COMPLETED | Receipt and final artifact manifest durably committed |

`PR_CREATED` means the selected PR adapter recorded success; UI must label a simulated
result and must never invent a live GitHub URL. Later real PR adapter requires deliberate
policy enablement and another review of credentials, approvals and reconciliation.

## Exceptional transitions

From any nonterminal state, allow BLOCKED on a classified recoverable dependency,
authentication or uncertain-operation problem; FAILED on a nonrecoverable integrity
error or exhausted retry budget; CANCELLED on an authenticated human cancellation.
COMPLETED, FAILED and CANCELLED have no outgoing transitions. Create a linked new task
to retry a terminal task. Cancellation first fences dispatch, stops running containers,
records actual outcomes and reconciles in-flight actions before committing CANCELLED.
It cannot undo an action already completed externally.

BLOCKED records `resume_state` and cause in TaskState. On explicit recovery, resume only
that stored prior state after revalidating all guards and reconciling in-flight work;
never take an arbitrary target from API input. If approvals expired, remain blocked
until a replacement approval is recorded against unchanged evidence. If evidence changed,
return to DRAFT_SPEC (spec changed) or PLANNING (plan/base changed), with invalidation
events. A block while already BLOCKED does not overwrite the original resume target.

For IMPLEMENTING/TESTING/REVIEWING interruption, the original operation either returns
its durable receipt or is stopped and fenced before a new attempt is scheduled in the
same state. No concurrent attempt is permitted. Default maximum is three implementation
attempts and two transient retries per provider invocation; operator policy may lower it.

## Evidence and approval invariants

Approval subjects are content hashes of canonical JSON (RFC 8785) plus task/revision,
spec, plan, base commit, policy version and action arguments as applicable. Bind PR
approval also to patch, passing tests, review decision, destination repo and branch.
Approval is single-use for an external operation, scoped and expiring. A new plan/base/
patch/action invalidates dependent evidence and approvals. Plan approval can authorize
bounded implementation retries within its unchanged WorkOrder scope until expiry.

Only authenticated human API principals may create ApprovalDecision. Client-provided
identity/time and model strings saying "approved" are not trusted. Server stamps identity,
time and subject and rechecks current revision in the same transaction that consumes
approval and creates dispatch intent. A policy denial is not bypassable by approval.
Modified tool requests require a new hash, policy evaluation and, when applicable, approval.

Test receipts are accepted only from the runner identity and must reference immutable
snapshot, test recipe digest and image digest. Review decisions bind exact evidence and
reviewer invocation. A reviewer ACCEPT cannot override failed tests or Guardian denial.
Retries get new invocation/operation IDs while duplicate delivery retains its original ID.

## Crash consistency and audit reconstruction

1. Commit operation intent/event/outbox atomically with expected task version.
2. Broker durably claims operation ID and lease/fencing token before execution.
3. Repeated delivery returns existing receipt; expired lease alone does not authorize
   a second running container. Reconcile or kill/reap the old execution first.
4. Persist outputs and receipt before acknowledging dispatch. Commit resulting state and
   receipt event atomically. Crash between receipt and event is repaired from operation ID.
5. Unknown external outcome becomes BLOCKED. Query external system by durable correlation
   marker/branch/ID before retry; exactly-once execution is not assumed. Simulation uses
   a unique operation constraint in the database.

The event trail includes spec confirmation, context manifests, invocation start/end,
requested and executed commands, policy decisions, approval request/decision/consumption/
invalidation, patch, tests, review, external intent/result, error and transition events.
Every event has task sequence, actor, origin role, provider/model where relevant, operation
or invocation correlation and artifact references. Replay reconstructs the task projection;
commands requested and commands actually executed remain separate evidence.
