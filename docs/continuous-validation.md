# Continuous validation

The `Validation` GitHub Actions workflow runs on pull requests, pushes to `main`,
and manual dispatch. It has four independent checks:

- `Offline (ubuntu-24.04)`
- `Offline (windows-2022)`
- `Docker (Linux)`
- `Browser (Linux)`

The offline jobs use Python 3.12 and Node 22, install the declared test dependencies,
validate contracts, run the Python suite, run all six UI regression scripts, compile
Python files and check whitespace. `scripts/ci_tests.py` explicitly removes Docker
opt-in from its environment and permits exactly the five named Docker test skips.
Missing gates, additional skips, failures and empty runs fail the job. The UI tests
use DOM stubs to exercise targeted response ordering and guard behavior.

The browser job uses pinned Playwright with Chromium against the real loopback Python
server. Each test starts a separate trusted-fixture server on an OS-assigned port and
uses a fresh temporary store, then stops that process and removes only its test store.
It reads the generated session from the server pipe without printing it or weakening
production authentication. It checks invalid-session rejection, disconnect, rendered
approval guards, evidence inspection, refresh review reset, separate approval/execution,
literal task-title rendering and distinct selection of tasks sharing a title,
and storage/integrity reports. Completion and cancellation tests also stop the server,
restart it against the same test store, reject the old session, and reconnect with the
new session. They compare persisted state/context, replayed event labels, a contract
record and the final event's artifact content before and after restart. Cancellation
retains its reason, and terminal controls remain unavailable. This exercises restart
between operations, not a crash during a broker operation or Docker recovery.
Uncaught page errors fail the tests. These are functional
Chromium checks, not a visual-layout audit or coverage of every recovery/pagination race.
Docker and live providers are not used by this job.

To run locally with Python and Node on PATH:

```sh
python -m pip install -r requirements-dev.txt
npm ci --ignore-scripts
npx playwright install chromium
npm run test:browser
```

Set `ORCH_TEST_PYTHON` to a Python executable path if it is not named `python`.
Linux hosts may need `npx playwright install --with-deps chromium`.
Traces are disabled because they can retain the disposable session token. Test output
directories are ignored by Git. The setup follows Playwright's official
[fixture](https://playwright.dev/docs/test-fixtures) and
[CI](https://playwright.dev/docs/ci) guidance.

The separate Linux job preloads the same digest-pinned Python image used by local
Docker validation, then runs `python -m orch verify-runtime`. All five gates must pass
without skips, and the host/image/source checks must remain consistent. The JSON
report is printed in the job log. It is temporary host-bound evidence, expires after
24 hours and always reports `provider_authorized: false`. A CI result does not qualify
a different workstation, WSL environment or corporate provider deployment.

Actions are pinned to full commit hashes resolved from the official checkout v7.0.1,
setup-python v7.0.0 and setup-node v7.0.0 tags. All three actions declare the Node 24
runtime, avoiding the deprecated Node 20 action runtime. This is separate from the
Node 22 interpreter installed for the UI tests. Automatic package-manager caching is
explicitly disabled; the browser job installs npm dependencies from the committed lockfile.
The action runtime requires Actions Runner 2.327.1 or newer; this workflow uses
GitHub-hosted runners. See the official [checkout release](https://github.com/actions/checkout/releases/tag/v7.0.1),
[setup-python release](https://github.com/actions/setup-python/releases/tag/v7.0.0),
and [setup-node release](https://github.com/actions/setup-node/releases/tag/v7.0.0).
Repository token permissions are limited to
`contents: read`, checkout does not persist credentials, and jobs use hosted runners
with 15-minute timeouts. No repository secrets, provider credentials, external action
brokers, publishing steps or `pull_request_target` execution are configured. Dependency
installation, Chromium installation and the explicit image preload require network access. This is separate
from runtime tests, which still never download their own worker image.

The workflow follows GitHub's [workflow syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax)
and [token permission guidance](https://docs.github.com/en/actions/tutorials/authenticate-with-github_token).
It does not change repository branch protection or Actions billing/enablement settings.
Once the checks have run successfully in the repository, they can be selected as
required checks in repository policy. Actions availability and hosted minutes remain
subject to the repository's existing settings.

Restart milestone verification on September 13, 2026: all four Chromium tests passed
on Windows, including completed and cancelled workflow restarts. JavaScript syntax
and whitespace checks passed. The existing Linux browser CI job discovers these tests
automatically; hosted results have not been awaited.

Initial browser milestone verification on September 13, 2026: both Chromium tests passed
on Windows, as did all six DOM-stub scripts and six focused HTTP/management Python
tests. JavaScript syntax and whitespace checks passed. Hosted Linux browser results
remain separate from this local verification.

Earlier baseline verification on September 12, 2026: the offline entry point ran 115 tests with
110 passing and exactly five expected Docker skips. Separately, all five Docker gates
passed on Windows 11 / Docker Desktop Linux engine. Both Node UI checks, contract
validation (19 examples, 366 negative cases) and workflow YAML/security-configuration
checks passed. Action hashes were resolved directly from the official repositories.
Hosted Windows/Linux job results must be read from the PR checks; these local results
do not establish that those hosted jobs passed.
