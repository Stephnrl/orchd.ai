# Deployment-readiness assessment

`deployment-check` combines provider inventory, optional worker evidence validation and
explicit remaining integration gates. It always exits **2** and reports `status: blocked`
and `provider_authorized: false`; the live launcher is not implemented or admitted.

```sh
python -m orch deployment-check --provider github_copilot_cli
python -m orch deployment-check --provider abc_binary_ai_placeholder
```

Without optional inputs it performs no runtime probes and opens no workflow store.
Add `--executable ABSOLUTE_PATH --expected-sha256 APPROVED_DIGEST` to inventory a file.
This reads/hashes the candidate without executing it, invoking help/version/login or
searching credential directories. A matching supplied digest is not provenance approval.

To assess existing worker evidence, add both `--runtime-report REPORT_PATH` and
`--image DIGEST_PINNED_IMAGE`. The existing runtime verifier checks expiry, required
test results and current source/host/daemon/image identity. This can run read-only Docker
info/image queries; it does not run isolation tests, pull images or create containers.
Rejected or missing reports remain blocking. Generate fresh evidence separately with
the [runtime-readiness commands](runtime-readiness.md).

The JSON report lists stable gate IDs and next steps for executable digest, worker
runtime, broker identity, native protocol, provider containment, credentials and live
admission. Add `--broker-endpoint 127.0.0.1:PORT --broker-secret FILE` to assess a
running [broker service](broker-service.md); the gate passes only when the broker
answers as a different OS account with the same source digest. Even matching
executable, worker and broker evidence leave the last four gates blocked. Docker worker
isolation does not establish containment for a native provider process. Corporate native
protocol guidance remains distinct from the available offline Copilot codec.

This assessment is informational, not an admission token or a review-signoff mechanism.
The engine never consumes it to enable execution. It neither accepts credentials nor
persists a report; stdout can be saved by the operator for review. See the
[progress assessment](progress.md) for the remaining overall scope.
