# Continuous validation

The `Validation` GitHub Actions workflow runs on pull requests, pushes to `main`,
and manual dispatch. It has three independent checks:

- `Offline (ubuntu-24.04)`
- `Offline (windows-2022)`
- `Docker (Linux)`

The offline jobs use Python 3.12 and Node 22, install the declared test dependencies,
validate contracts, run the Python suite, run both UI regression scripts, compile
Python files and check whitespace. `scripts/ci_tests.py` explicitly removes Docker
opt-in from its environment and permits exactly the five named Docker test skips.
Missing gates, additional skips, failures and empty runs fail the job. The UI tests
use DOM stubs; they do not provide rendered-browser coverage.

The separate Linux job preloads the same digest-pinned Python image used by local
Docker validation, then runs `python -m orch verify-runtime`. All five gates must pass
without skips, and the host/image/source checks must remain consistent. The JSON
report is printed in the job log. It is temporary host-bound evidence, expires after
24 hours and always reports `provider_authorized: false`. A CI result does not qualify
a different workstation, WSL environment or corporate provider deployment.

Actions are pinned to full commit hashes resolved from the official checkout v4,
setup-python v5 and setup-node v4 tags. Repository token permissions are limited to
`contents: read`, checkout does not persist credentials, and jobs use hosted runners
with 15-minute timeouts. No repository secrets, provider credentials, external action
brokers, publishing steps or `pull_request_target` execution are configured. Dependency
installation and the explicit image preload require network access. This is separate
from runtime tests, which still never download their own worker image.

The workflow follows GitHub's [workflow syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax)
and [token permission guidance](https://docs.github.com/en/actions/tutorials/authenticate-with-github_token).
It does not change repository branch protection or Actions billing/enablement settings.
Once the checks have run successfully in the repository, they can be selected as
required checks in repository policy. Actions availability and hosted minutes remain
subject to the repository's existing settings.

Local verification on September 12, 2026: the offline entry point ran 115 tests with
110 passing and exactly five expected Docker skips. Separately, all five Docker gates
passed on Windows 11 / Docker Desktop Linux engine. Both Node UI checks, contract
validation (19 examples, 366 negative cases) and workflow YAML/security-configuration
checks passed. Action hashes were resolved directly from the official repositories.
Hosted Windows/Linux job results must be read from the PR checks; these local results
do not establish that those hosted jobs passed.
