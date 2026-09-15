# Bounded local Git repository pilot

This milestone admits an operator-supplied, ordinary local Git checkout for a
small JSON data change. It captures a real commit baseline, prepares an exact
diff, requires explicit scope/revision approval, writes a disposable workspace,
runs trusted JSON assertions and retains a durable result. An optional
[restricted Docker worker profile](pilot-container-workers.md) now runs edit and
test in separate containers with retained dispatch and cleanup evidence. The source checkout
is never modified. No repository programs, package managers, hooks, model
providers or integration writes run.

This is a separate CLI pilot journal, not a generalized `Engine` task. It does
not produce fixture TaskState, broker PatchReceipt or GitHub approval authority.
The existing fixture API/UI and serial batches remain scoped to fixture tasks.

## Supported policy

- An ordinary Git SHA-1 checkout at the exact supplied HEAD, with an unchanged
  index and tracked bytes and no non-ignored untracked files.
- Up to 128 tracked files and 2 MiB total baseline bytes; at most 1 MiB per file.
  Symlinks, junctions, hardlinked tracked files, submodules and case collisions
  are rejected. Git filters are never invoked. Checkout bytes must match Git
  blob bytes, so configure disposable repositories with `core.autocrlf=false`.
- One to eight existing non-executable `.json` files, portable non-hidden paths,
  at most 64 KiB replacement bytes in total. No creation/deletion/renaming.
  Every replacement must change bytes. JSON must have unique keys and finite
  values; files are UTF-8.
- One to 32 fixed JSON checks, with an explicit object-key sequence and expected
  value. Every edited file has at least one check. These checks are reviewed
  operator policy, not tests supplied by a model or executable repository code.
- A workspace contains only the selected replacement files. It is a data-only
  snapshot, not a full build checkout. Passing checks establish only the supplied
  JSON assertions, not broader application correctness.

Git itself and the local operator account are trusted. This is not admission of
arbitrary hostile repositories or a host sandbox. Keep the journal outside the
repository. Ignored source files are neither copied nor executed. Local evidence
hashes detect accidental alteration; they are not signed provenance against a
malicious host administrator.

## Review and run

Create a disposable local repository with a committed `settings.json` containing
`{"service":{"retries":1}}` and set its `core.autocrlf=false` before committing.
Save an intent outside the repository (replace the path and commit):

```json
{
  "repository": "C:/disposable/pilot-repository",
  "base_commit": "REPLACE_WITH_EXACT_40_CHARACTER_COMMIT",
  "replacements": {"settings.json": "{\"service\":{\"retries\":3}}\n"},
  "checks": [{"path": "settings.json", "keys": ["service", "retries"], "equals": 3}]
}
```

```text
python -m orch pilot-prepare --data .runtime/pilot --intent intent.json
python -m orch pilot-inspect --data .runtime/pilot --pilot-id ID
python -m orch pilot-run --data .runtime/pilot --pilot-id ID --expected-sha256 REVIEWED_SCOPE_SHA256 --expected-revision 0
python -m orch pilot-inspect --data .runtime/pilot --pilot-id ID
```

Review `scope` and `diff`, including the full replacement text and checks. The
run command is the explicit local operator approval. Scope binds the baseline,
paths, before/after bytes, checks, policy, runtime source, journal location and
unique operation ID, and expires after 30 minutes. A changed baseline, source,
scope hash or revision blocks admission. Inspection never authorizes execution.
CLI JSON can report `failed` even after the command completed successfully;
consumers must check `status` and `matches_receipt`.

An unused preparation can be consumed without execution:

```text
python -m orch pilot-abandon --data .runtime/pilot --pilot-id ID --expected-sha256 REVIEWED_SCOPE_SHA256 --expected-revision 0
```

## Durable recovery

SQLite serializes admission across processes. `prepared` becomes `reserved` and
commits before workspace effects. Only then are replacement bytes written and
flushed, checked and recorded as `passed` or `failed`. Each mutation increments
the journal revision. A scope can be used only once.

An interruption leaves `reserved`, possibly with partial workspace files.
Restart and inspect: `observed_files` shows retained hashes and `recovery` says
`retain_and_inspect`. Inspection does not infer success, adopt partial effects or
retry. Keep that workspace for investigation; prepare and review a new unique
scope if another attempt is appropriate. Completed receipts bind the scope and
actual file hashes; changed or unexpected files make `matches_receipt` false.
Docker reservations additionally support explicit, ownership-checked container
reconciliation, ending in `interrupted` once absence is confirmed. This never
retries work. No workspace cleanup, archive, resume or automatic retention deletion is enabled.
The journal caps retained preparations at 128. Standard workflow backup commands
do not include this separate pilot database or its workspaces.

## Acceptance and next boundary

`python scripts/pilot_acceptance.py` creates a disposable Git repository and
exercises the real CLI prepare/review/run/inspect cycle, restart, injected
interruption and rejection of a repeated attempt. The offline/full release lane
includes this acceptance. Unit tests cover Git filters, stale baseline/index,
approval/source/expiry fences, byte budgets, path constraints and tampering.

The optional Docker profile has separate opt-in host/image qualification checks.
Next, integrate an explicitly selected workload with the kernel and broker,
qualify the deployment's trusted test image, and add an independently reviewed proposal-only
provider. Custom executable tests require a separate containment milestone.

## Rivet references and language decision

[Rivet Actors](https://github.com/rivet-dev/actors) supplies a useful design
reference for durable state, queues and per-workspace ownership. This pilot uses
the project's existing SQLite approach and explicit uncertain-effect handling.
[Rivet Skills](https://github.com/rivet-dev/skills) contains generated product
documentation skills; it is not an orchestration runtime dependency.

Keep the control plane in Python. At the custom-image/worker milestone, evaluate
a Rust supervisor if a standalone binary, process lifecycle control or measured
resource requirements justify maintaining a second language. Rust alone does not
establish container isolation or protect credentials. No Rust runtime is admitted
by this milestone.
