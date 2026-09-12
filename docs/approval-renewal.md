# Renewing an expired pending approval

An expired pending plan or simulated-action request can now be replaced with a fresh
request for the same subject and evidence. The task stays at its approval pause.
Renewal never grants a decision or starts execution.

The local UI displays **Request fresh approval** when its clock indicates that the
pending request has expired. After renewal, inspect the new request and evidence,
check the review checkbox and explicitly approve or reject. The server's clock is
authoritative; a clock mismatch can cause a renewal request to be rejected.

The authenticated API accepts only the current request ID and task revision:

```text
POST /tasks/TASK_ID/renew-approval
Authorization: Bearer OPERATOR_SESSION
Content-Type: application/json

{"request_id": "EXPIRED_REQUEST_ID", "expected_revision": 3}
```

The equivalent CLI command requires an existing workflow database and no Docker image:

```sh
python -m orch renew-approval --data .runtime/managed --task TASK_ID --request-id EXPIRED_REQUEST_ID --expected-revision 3
```

Under the dispatcher lock and one transaction, the engine checks expiry, revision,
pending state, absence of unresolved execution, policy, subject and evidence bindings.
It writes a new immutable ApprovalRequest with a one-hour lifetime, advances the task
revision, and records an `approval_renewed` event with the trusted operator identity
and old/new request references. The old request remains evidence but is no longer
actionable. No expiry, identity or scope override is accepted from API callers.

An exact retry returns the same pending replacement without adding events. Once that
replacement is decided, cancelled or renewed again, the previous renewal request is
stale. The new request and terminal cancellation behavior survive backup/restore.

This applies only to expired, still-pending requests at the two approval pauses.
It does not extend an already granted plan/action decision, renew a WorkOrder deadline,
resume blocked execution or undo an external action. Those cases retain the existing
recovery/cancellation boundaries. Live providers and external integrations remain
disabled.

Validation on September 12, 2026: all 97 Python tests passed with zero skips,
including all five real Docker gates. The Node UI regression check passed:
`node tests/test_ui_approval_renewal.cjs`. It covers expired/fresh controls and request
binding using a DOM stub; it is not a rendered-browser test. Contract validation
passed 19 examples and 366 negative cases, alongside Python compilation, JavaScript
syntax and whitespace checks.
