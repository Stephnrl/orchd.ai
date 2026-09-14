# Offline release verification

Run the complete local acceptance package against a trusted checkout with Python,
Node, the locked development dependencies and Playwright Chromium already installed:

```sh
python -m orch verify-release --destination .runtime/release.json
```

The runner never installs dependencies or pulls an image. It runs fixed commands
without a shell and reports progress on stderr. Its final JSON contains the result
and the report's whole-file SHA-256. Keep that digest separately from the report.
Use a new destination for each run; existing evidence is never overwritten.

The default `full` lane runs contract validation, the entire Python offline suite,
the synthetic Jira and serial-batch acceptance scenarios, JavaScript syntax checking, every
`tests/test_ui_*.cjs` script, and the Chromium operator suite. CI uses the same
runner with explicit `--release-lane offline` and `--release-lane browser` lanes
to retain separate Windows/Linux offline and Linux browser jobs. An individual
lane cannot satisfy a request to check a full report; reports are not combined
automatically across hosts or source versions.

Python acceptance requires exactly the five named Docker skips and rejects expected
failures as well as ordinary failures. Browser acceptance requires a nonempty run,
no runner errors, and exactly one passing result per reported test: skipped,
expected-failure, retried and flaky results cannot pass. UI scripts are discovered
from the bound source inventory, so adding a script does not require another CI step.

Each stage has a ten-minute deadline and a 4 MiB limit per output stream. The runner
continues other stages after a failure to collect all failing stage IDs, then exits
with code 2. Reports retain status, test counts and skip identities, never raw
subprocess output, tokens or exception text. To diagnose a failed stage, rerun its
fixed command directly in the trusted development environment:

| Stage | Command |
| --- | --- |
| `contracts` | `python scripts/validate_contracts.py` |
| `python` | `python scripts/ci_tests.py` |
| `jira_acceptance` | `python scripts/jira_acceptance.py` |
| `batch_acceptance` | `python scripts/batch_acceptance.py` |
| `javascript_syntax` | `node --check orch/ui/app.js` |
| `tests/test_ui_*.cjs` | `node` followed by the reported script path |
| `browser` | `node node_modules/@playwright/test/cli.js test --reporter=json --forbid-only` |

## Checking retained evidence

```sh
python -m orch check-release --release-report .runtime/release.json --expected-sha256 RETAINED_REPORT_SHA256
```

For a partial CI report, explicitly supply its exact lane. The checker is read-only
and launches no tests, provider, Docker operation or network request. It requires
canonical bounded JSON, the retained digest, a complete successful stage inventory,
the same source digest and a report less than 24 hours old. Future timestamps,
changed deadlines, unexpected skips, omitted stages and authority claims are rejected.

The source digest covers `orch`, `tests`, `scripts`, `contracts`, `examples`, `docs`,
`.github`, README, development requirements, npm manifest/lock and Playwright config.
Byte changes, added/removed files and dependency-policy changes invalidate evidence.
Generated Python caches and `.runtime` outputs are excluded. Links and junctions in
the bound tree are rejected. Outputs cannot be written into bound source directories.
The runner compares inventories before and after acceptance and fails on drift.
Do not edit the checkout during verification; an edit reverted before the final
snapshot cannot be detected by these snapshots.

The report records the observed OS, architecture and Python version; verification
can be performed on a different host with byte-identical source. It does not certify
that host's runtime. Dependency locks are source-bound, but the report does not
attest installed dependency bytes or executable provenance. Subprocesses are trusted
test programs, not sandboxed workloads.

This is unsigned local evidence. A separately retained digest detects changes to
that report; it does not authenticate its producer or make a supplied passing report
trustworthy. Host administrators remain trusted. Reports always state
`live_authorized: false`. Docker host verification, provider containment review,
credential/identity review and live integration admission remain deferred gates.
Use [runtime verification](runtime-readiness.md) and
[deployment checks](deployment-readiness.md) for those separate requirements.
