# Live integration runbook

This is for whoever wires orchd to a real GitHub and a real Jira — on a machine that can
reach both, which the development workstation cannot. It assumes you have the repository and
a Python 3.12 environment with `requirements-dev.txt` installed.

It is written to be worked through in order. Each part says what to do, what proves it
worked, and what to do when it does not.

## Rules that must not be broken

These are not style preferences. Breaking any one of them makes everything above it
meaningless, and the code will usually stop you — but an assistant trying to be helpful can
talk itself past them, so they are stated plainly.

1. **No token is ever mounted into an agent container.** Agents hold a secret that proves
   *which agent they are* and grants nothing. The broker holds credentials. An agent
   container with a GitHub token can act whenever it likes while the evidence records one
   approved call; see [outbound dispatch](outbound-dispatch.md).
2. **No token is ever pasted into a chat, a prompt, a commit, a test fixture, a PR, or an
   issue body.** Refer to credentials by *path*. If you need to check one, check its shape,
   not its value.
3. **A credential file names the origin it may be sent to.** Do not work around a
   "This credential is not for …" refusal by editing the origin to match the request. That
   refusal means the request is going somewhere the token was not issued for.
4. **Do not relax a refusal to make a step pass.** Every check here exists because of a
   specific failure. If one fires, the answer is upstream.
5. **Real dispatches have real effects.** A `dispatch` creates an issue, a ticket, a link.
   Use a scratch repository and a scratch Jira project until the whole path is proven.

## Part 1 — GitHub

**This part has been run end to end against a real repository.** On 2026-09-16 the whole
chain — specification confirmed by a human, duplicate search, read plan, preview, journal
staging, approval, reservation, dispatch — created
[issue #127](https://github.com/Stephnrl/orchd.ai/issues/127) on `Stephnrl/orchd.ai` with a
fine-grained token. Reconciliation then found that issue by its operation marker, and a second
attempt under the same operation id was refused rather than filing a duplicate. What follows
is that path, not a proposal for one.

### 1.1 Create the token

A **fine-grained personal access token**:

- Resource owner: the account or org that owns the repository
- Repository access: **Only select repositories** — name them; never "all repositories"
- Permissions:
  - **Metadata: Read** (GitHub requires it)
  - **Issues: Read and write**
- Nothing else. No Actions, no Administration, no Workflows, no organization permissions.
- Shortest expiry you can work with.

Add **Contents: Read** and **Pull requests: Read and write** only when you get to dispatching
pull requests, which is not in this runbook.

### 1.2 Write the credential file

Two lines, and the file must be readable only by the account the broker runs as:

```sh
printf 'origin=https://api.github.com\ntoken=github_pat_xxx\n' > /etc/orchd/github.credential
chmod 600 /etc/orchd/github.credential
```

On Linux the loader refuses a file with any group or other permission bits. On Windows there
are no mode bits to check and it will load regardless — so if you are configuring on Windows
and deploying to Linux, set the permissions at deployment.

Verify it loads without printing it:

```sh
python -c "from orch import dispatch; print(dispatch.read_credential('/etc/orchd/github.credential')['origin'])"
# https://api.github.com:443
```

### 1.3 Confirm the token, read-only

One GET. No effect. This is also the fastest way to find a mis-scoped token:

```sh
python - <<'EOF'
from orch import dispatch
cred = dispatch.read_credential('/etc/orchd/github.credential')
reads = {'repository': {'method': 'GET', 'url': 'https://api.github.com/repos/OWNER/NAME'}}
body = dispatch.fetch('a'*64, reads, cred)['responses']['repository']['body']
print({k: body.get(k) for k in ('id', 'full_name', 'archived', 'disabled', 'has_issues')})
EOF
```

**Write down the `id`.** It is the `repository_id` every profile needs, and it is the reason a
renamed or transferred repository cannot quietly become a different one.

What can go wrong:

| Symptom | Cause |
| --- | --- |
| `403` with an HTML body | Missing `User-Agent`. Fixed in `dispatch.TRANSPORT`; if you see it, you are on an older revision |
| `401` | Token wrong, expired, or revoked |
| `404` on a repository that exists | The token's repository access does not include it |
| `has_issues: false` | Issues are disabled; the preview will refuse and it is right to |

### 1.4 The profile

```json
{"schema_version": "1.0.0", "codec": "github-rest-2026-03-10",
 "repository": "OWNER/NAME", "repository_id": 1234567}
```

`github_issues.API` is hardcoded to `https://api.github.com` on purpose — no host override, no
inferred destination. **GitHub Enterprise Server is therefore not supported yet**; see the open
tasks below.

### 1.5 Ask whether the work is already tracked

```sh
python -m orch github-issue-duplicate-plan --github-profile profile.json \
    --terms "rate limiting in:title" > plan.json
python -m orch broker-fetch --plan plan.json \
    --broker-endpoint 127.0.0.1:PORT --broker-secret /etc/orchd/broker.secret > capture.json
python -m orch github-issue-duplicates --github-profile profile.json \
    --terms "rate limiting in:title" --transcript capture.json
```

A search whose `total_count` exceeds the results returned is **refused** — narrow the terms
rather than acting on a partial view of what exists.

### 1.6 Create one

```sh
# 1. The intent
cat > intent.json <<'EOF'
{"schema_version": "1.0.0", "task_id": "<32 hex>", "operation_id": "<32 hex>",
 "repository": "OWNER/NAME", "title": "...", "body": "...", "labels": []}
EOF

# 2. What must be observed first, and observing it
python -m orch github-issue-read-plan --github-profile profile.json --intent intent.json > plan.json
python -m orch broker-fetch --plan plan.json --broker-endpoint ... --broker-secret ... > capture.json

# 3. The exact request, checked against what was observed
python -m orch github-issue-preview --github-profile profile.json --intent intent.json \
    --transcript capture.json > preview.json

# 4. Hold it, so there is a record of what is being approved
python -m orch github-issue-stage --journal github.sqlite --github-profile profile.json \
    --intent intent.json --transcript capture.json --expected-sha256 <preview sha256>

# 5. Approve it — evidence for an issue is the confirmed spec and the duplicate search
python -m orch github-approval-preview --journal github.sqlite --operation-id <op> \
    --expected-sha256 <scope sha256> --records evidence.json
python -m orch github-check-approval ...

# 6. Send it. The hash is taken from the preview, not from you, so what goes out is
#    the request this preview describes and no other.
python -m orch broker-dispatch --preview preview.json     --broker-endpoint 127.0.0.1:PORT --broker-secret /etc/orchd/broker.secret
```

`broker-dispatch` exits non-zero on anything but success, so a script stops rather than
carrying on past a `failed` or — more importantly — an `uncertain` outcome.

Labels must already exist on the repository. A label the repository does not have is **named
rather than created**, because creating an issue with an unknown label creates the label too,
and that is an effect nobody reviewed.

Every issue created this way carries an HTML comment in its body naming the operation. It is
invisible when the issue is read and exact when it is searched for, and it is what makes
creation idempotent and an uncertain dispatch reconcilable.

### 1.7 When the answer never comes back

A dispatch returns `outcome: "uncertain"` when the bytes went and the answer did not. **Do not
retry it.** The effect may exist. Reconcile instead:

```sh
python -m orch github-issue-reconcile --github-profile profile.json --intent intent.json \
    --transcript <fresh capture>
```

`candidate_observed` with a number means it was created. `unresolved` with
`no_candidate_observed` means it was not. `duplicate_operation_marker` means something is
wrong that a human must look at, not a case to choose between.

## Part 2 — Jira

**Nothing in this part has been run against a real Jira.** The codec is built and tested
offline; the path below is what it is designed to do, and confirming it is the first task.

### 2.1 Use a service account, not your own token

This is the important difference from GitHub. A Jira PAT (Data Center) or API token (Cloud)
**inherits the permissions of whoever owns it** — there is no per-permission scoping. A token
minted on an admin account is an admin-capable credential no matter how narrowly you intend
to use it.

So the scoping mechanism is the account:

- Create a dedicated service account
- Grant it, on **one project** only: Browse Projects, Create Issues, Edit Issues, Link Issues,
  Transition Issues
- Nothing else, and no admin group

### 2.2 The credential file

```sh
printf 'origin=https://jira.example.com\ntoken=xxx\n' > /etc/orchd/jira.credential
chmod 600 /etc/orchd/jira.credential
```

A broker serves **one** origin, because a credential names one origin. Running GitHub and Jira
through one broker is not possible today; see the open tasks.

### 2.3 The profile

`jira_actions.validate_profile` requires every field explicitly — no inference, no defaults:

```json
{"schema_version": "1.0.0", "codec": "jira-rest-8.5",
 "base_url": "https://jira.example.com", "project_id": "10000", "project_key": "PROJ",
 "issue_types": {"epic": "10001", "story": "10002", "task": "10003"},
 "epic_name_field": "customfield_10005", "epic_link_field": "customfield_10006",
 "github_repository": "OWNER/NAME"}
```

Every one of those ids is deployment-specific and must come from your Jira, not from a guess.
The issue type ids and the two Epic custom fields are the ones that differ most between
instances, and the codec checks the Epic fields against the exact Jira field types it expects,
so a wrong mapping fails loudly rather than writing to the wrong field.

### 2.4 The actions available

`create_issue` (epic, story or task), `transition`, `issue_link`, `epic_link`, and
`github_link` — which names a GitHub issue or pull request by number, and is how a ticket ends
up pointing at the issue Part 1 created.

The shape is the same as GitHub's: `jira-action-read-plan` → fetch → `jira-action-preview` →
`jira-stage-action` → approve.

Epic reparenting is deliberately unsupported: `epic_link` attaches an issue that has no Epic,
and refuses one that already does, because implicit reparenting would silently move existing
work.

## Open tasks

These are genuinely open — they need decisions or evidence that cannot be produced on the
development workstation.

1. **Confirm a Jira read plan survives `broker-fetch`.** The plan's request shape passes the
   fetch checks offline; what is unconfirmed is whether a real Jira's responses satisfy
   `jira_actions.responses` — particularly the `createmeta` and `editmeta` bodies, which vary
   by version and configuration. Start with `jira-action-read-plan` for a `create_issue` and
   compare the capture against what `preview_action` expects.
2. **One broker per origin is a limitation, not a design.** A broker holds one credential, so
   GitHub and Jira need two brokers today. Decide whether a broker should hold several
   credentials keyed by origin, and if so, how a request is matched to one without letting a
   request choose its own credential.
3. **GitHub Enterprise Server is unsupported**, tracked as
   [issue #127](https://github.com/Stephnrl/orchd.ai/issues/127). `github_issues.API` is hardcoded. Supporting
   GHE means letting the profile name the API host, which re-opens the question the hardcoding
   closed: how to stop an operator-supplied profile pointing at a host the token was not
   issued for. The credential's origin binding is probably the answer, but it needs stating
   deliberately rather than assuming.
4. **A real receipt is still not admitted into the task workflow.** `engine.py` requires
   `ExternalActionReceipt.simulated` to be true for a task to reach `PR_CREATED`. Until that
   changes, dispatch is an operator path beside the workflow rather than part of it.
5. **`outbound_dispatch` passes only when the broker runs as a separate OS account.** Provision
   that account. A credential file on the orchestrator's own account is readable by everything
   the orchestrator runs.
6. **Nothing connects the draft specification stage to tracker actions.** A project manager
   producing an epic, its stories, and the issue tracking them as *one* reviewed bundle is not
   built. Creating an issue and linking a ticket to it are two actions with two approvals, and
   across two services nothing could make them one transaction — so decide what a partial
   bundle should leave behind.

## What proves it works

```sh
python -m orch deployment-check --provider github_copilot_cli \
    --broker-endpoint 127.0.0.1:PORT --broker-secret /etc/orchd/broker.secret
```

Read `blocking_gates`. `outbound_dispatch` passing means a credential is configured **and** the
broker is a separate account. Every other gate has its own `next_step`, and none of them is
satisfied by editing this file.
