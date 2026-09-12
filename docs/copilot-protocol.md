# Copilot protocol and deployment prerequisites

This milestone implements a Copilot-specific **offline protocol codec**, not live
provider execution. Neither Copilot CLI nor the corporate CLI is available on the
development workstation. Tests use recorded deterministic fixture proposals; none
are described as captured Copilot responses or proof of native compatibility.

## Documented protocol

GitHub documents noninteractive prompt delivery using `-p` and response-only output
using `--silent`. The candidate profile selects nonstreaming text output and requires
one JSON object as the model response. JSONL event framing is deliberately not parsed:
the documented `--output-format=json` is an event stream, not an orchd.ai contract.
See the official [programmatic reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-programmatic-reference)
and [command reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference).

`orch/copilot.py` prepares an argv array with one prompt argument, an explicit model,
and fixed candidate controls for tools, custom instructions, remote access/export,
automatic updates and built-in MCPs. It does not execute that array. The profile
uses a list of documented tool exclusions; new or undocumented tools remain a reason
to block admission. No claim is made that this list establishes a zero-tool runtime.
These flags must be checked against the exact deployed version before use.

Prompts include the role's validated context, task request text marked untrusted,
the transitive result JSON Schema, reference-binding instructions and the fixed
greeting proposal policy. Junior proposals still contain only the bounded fixture
content. Planner proposals cannot expand the fixed path, test recipe or budgets.
Decoding rejects Markdown wrappers, commentary, JSONL envelopes, duplicate keys,
invalid numbers, cross-task/invocation results and recognized secrets, including
secrets escaped inside JSON strings. Unknown output is rejected rather than repaired.

`CopilotProvider.prepare(request, target_os)` returns reviewable data only;
`decode(raw, request)` checks a proposed response. Windows preparation also checks
the serialized UTF-16 command-line length. Linux preparation checks the configured
prompt byte budget; target-specific ARG_MAX/environment limits need runtime review.
`invoke()` returns an admission-failure receipt and the engine rejects this provider
factory before opening storage. Fixture provider behavior is unchanged.

## Inventory without an installation

```sh
python -m orch provider-check --provider github_copilot_cli
python -m orch provider-check --provider abc_binary_ai_placeholder
```

Both commands currently exit 2 and report `provider_authorized: false`. When an
approved executable exists, add `--executable ABSOLUTE_PATH --expected-sha256 DIGEST`.
The command reads/hashes the file only; it never runs version/help, logs in, accesses
credential stores or invokes a model. Even a matching hash leaves version,
authentication and containment unverified. A matching file is not proof of an
executable's origin, behavior or authorization. The command does not search home
directories or create workflow storage.

The user supplied `~/.abcplaceholder` as a WSL location hint. It could be a directory,
configuration location or launcher; its meaning is not established. No executable
name, native arguments or PowerShell equivalent is inferred. A future deployment
must provide an explicit path valid on that deployment host. The corporate provider
continues to report an unsupported protocol.

## Evidence needed on a deployment host

The operator/deployment owner will need to supply these non-secret details:

| Requirement | Evidence to obtain |
| --- | --- |
| Executable identity | Approved distribution, exact version, executable path, SHA-256 and any dependency/launcher identity |
| Native protocol | Version-specific help, documented arguments, sanitized successful/error output samples, exit codes |
| Model | An explicit approved model identifier and availability for that account |
| Tool boundary | Tests denying filesystem access, shells, delegation, MCPs, hooks, plugins and network tool calls, including adversarial prompts |
| Context and credentials | Isolated configuration/cache/work directory behavior, authentication method, credential access scope and approved source-sharing destinations |
| Runtime | Cancellation/timeout of the entire process tree, bounded logs/output, cleanup and applicable host/container evidence |
| Corporate CLI | Documentation establishing what `~/.abcplaceholder` contains, executable/launcher name, proposal-only support and authentication/SSO behavior |

GitHub's [configuration-directory documentation](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-config-dir-reference)
describes separate configuration and cache locations, hooks and installed extensions.
A fresh configuration directory alone is insufficient evidence that every extension,
policy, credential source or cache is isolated. The deployment review must account
for all of them. Never paste credentials into configuration examples, prompts, PRs
or conformance fixtures.

Once that evidence exists, the next implementation is a contained live launcher and
its admission policy, followed by opt-in nonsensitive provider conformance tests.
This codec provides the prompt/output boundary for that work; it is not permission
to launch the supplied executable. Python remains the implementation language.

## Validation record

On 2026-09-12, the full 70-test suite passed with zero skips, including both real
Docker checks. A subsequent six-test protocol run passed, including one newly added
CLI exit-code/no-storage test (71 distinct tests covered overall). Phase 1 validation
passed 19 examples and 366 negative cases; compilation and whitespace checks passed.
Both actual provider-check CLI smoke runs reported not configured and unauthorized.
No real provider process or authentication attempt was made.

Fresh runtime report `1e039f12174943de8a75b93a077b164c` passed with source digest
`38374f4062d1b639c5fb7d58ecab1f9a7e5da239bbc3e15b7b43aa0d5aef72b8` and
`provider_authorized: false`. Machine-local evidence is under ignored `.runtime/`;
it expires and must be regenerated after source/environment changes.
