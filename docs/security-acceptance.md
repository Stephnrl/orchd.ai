# Security acceptance: criteria bound to the tests that exercise them

`docs/security.md` states ten mandatory acceptance criteria as prose. Nothing checked
that the tests behind them still existed, so a renamed or deleted test could quietly
remove coverage while the document went on asserting it. This milestone binds each
criterion to the exact tests that exercise it, runs them, and reports what that evidence
does and does not establish.

```text
python scripts/security_acceptance.py
python scripts/security_acceptance.py --mapping-only
python scripts/security_acceptance.py --image IMAGE@sha256:DIGEST --destination .runtime/security.json
```

The default run checks the mapping and then runs the named tests. `--mapping-only` checks
the mapping alone and is fast enough to use while editing. `--image` opts into the five
Docker gates on a qualified host. `--destination` retains the full report; an existing
path is never overwritten.

## What the mapping guarantees

`check_mapping()` refuses unless all of the following hold:

- The mapped criterion identifiers are exactly the `SEC-NN` identifiers the document
  lists, in the same order. Adding a criterion to the document without mapping it, or
  removing one, fails.
- Every criterion names at least one test and states at least one limit.
- Every named test still exists in the discovered suite. A renamed or deleted test fails
  the release lane rather than silently dropping coverage.
- No test is named twice within a criterion, and the Docker tests a criterion claims are
  exactly the five required container gates.

## What the report says, and what it does not

Each criterion reports four separate things, so coverage is never overstated:

| Field | Meaning |
| --- | --- |
| `offline_tests` | Tests run here, with their verdict. Anything but `passed` fails the criterion, including an unexpected skip. |
| `docker_tests` | The five container gates. Without `--image` they are `not_run`, and the criterion gains an explicit deferred line saying so. |
| `other_stages` | Release stages that cover part of the criterion but are not run here, such as the UI scripts and the Chromium suite for rendering safety. |
| `deferred` | What no local test can establish. These sentences are the honest limit of the report. |

Every report states `independent_review: false` and `live_authorized: false`. A `covered`
status means each criterion's named tests passed on this host against this source. It is
not an independent security review, a deployment authorization, or a claim about a live
provider. The deferred lines record, among others, that container isolation rests on the
Docker gates and is not a kernel-escape assessment, that receipts are provenance within a
trusted host identity rather than signed evidence, that destructive and production
operations are not implemented so their approval paths are unexercised, that the reviewer
is a deterministic fixture, that tool-disable behaviour has never been verified against a
deployed provider CLI, and that no dependency or image scanning exists.

## Boundaries

The mapping is a claim about which tests exercise a criterion; a reviewer should read it
as one. It does not measure coverage, prove the tests are sufficient, or replace the
threat model. Tests named here also run in the `python` stage, so the lane runs them
twice; that keeps this script independently runnable, which is how every other acceptance
script in the repository works. Adding a criterion to `docs/security.md` requires adding
its mapping in the same change, which is the point.
