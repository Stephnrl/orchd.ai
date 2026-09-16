# The agent protocol as plain context

[The loopback agent API](agent-api.md) gave an agent container a socket and a secret, and
nothing that tells it how to use them. Everything an agent needs to know — that a refusal is
final, that a lease expires, that an approval pause belongs to a human — lived in this
repository's documentation, where an agent running in a container never reads it.

That text now ships as something a container can be given:

```text
skills/orchd-protocol/SKILL.md        the whole protocol, short enough to inject every turn
skills/orchd-protocol/reference.md    states, routes, leases, refusals, read on demand
```

## Why plain Markdown

The agent tools people actually run disagree about where this text lives, not about what it
says. A Claude-style tool discovers `SKILL.md` and loads it when its frontmatter description
matches the work at hand. Copilot CLI reads an instructions file on every turn. An image may
simply vendor a copy and tell its agent to read it. Three placements, one protocol.

So the text is plain Markdown with three lines of YAML frontmatter at the top. The
frontmatter is the only tool-specific thing in it, it is what a Claude-style loader matches
on, and a tool that does not understand it reads it as a heading it can ignore. Nothing in
the body is written for one tool.

The split between the two files is progressive disclosure done with ordinary files rather
than a loading mechanism. The core states what an agent must never do and the loop it runs;
the reference holds the tables it needs only sometimes. Both tools can read a file when
asked, so both get the same behaviour: the short file is always present and the long one is
read when it is relevant. A test bounds the core's size, because a file injected on every
turn is paid for on every turn.

## Exporting it

```sh
python -m orch skill-export --to AGENT_REPO --layout copilot
python -m orch skill-export --to AGENT_REPO --layout claude
python -m orch skill-export --to IMAGE_STAGE --layout plain
python -m orch skill-export --to IMAGE_STAGE --layout plain --check
```

| Layout | Where the files land |
| --- | --- |
| `plain` | `SKILL.md` and `reference.md` at the destination |
| `claude` | `.claude/skills/orchd-protocol/`, where a skill is discovered |
| `copilot` | `orchd-protocol/`, plus a short `.github/copilot-instructions.md` pointing at it |

The two protocol files belong to this repository, so an export replaces them — replacing an
older copy is the point. The Copilot instructions file belongs to whoever owns that
repository and may hold rules this command knows nothing about, so it is written only when
absent or already identical; otherwise the export refuses and names it, and the pointer is
added by hand.

`--check` writes nothing and reports each file as `current`, `stale` or `absent`, exiting 2
when any is not current. That is for an image that vendored a copy: drift between the image
and the control plane becomes a failing check rather than a surprise at the first refusal.

Every report names the SHA-256 of what should be there, over text whose line endings are
normalised on the way out. A checkout that converts newlines would otherwise publish a
different digest on Windows than on Linux for a file nobody edited, and a vendored copy
would look stale to whichever host did not write it.

## Keeping it true

Prose about a protocol goes stale silently. A state gains a role, a lease bound moves, a
route is renamed, and the containers keep reading a document that was true once —
and unlike code, nothing fails.

`tests/test_skill_export.py` reads the published text and compares it to the code beside it:
every state and its role against `ROLE_FOR_STATE`, the role list against `ROLES`, the route
table against `ROUTES`, every documented field against the wire contract in both directions,
the lease table against the kernel's bounds and the limits paragraph against the service's.
A required reply field that no one documented fails; a documented field that no contract has
fails too. The text is the only place these facts are stated for an agent, which is why they
are checked rather than trusted.

One more property is fixed there: the bytes published to the Claude layout and to the Copilot
layout are identical. If the two ever diverge, the protocol has become tool-specific, which
is the thing this milestone exists to avoid.

## What this is not

Exporting copies files. It installs nothing, runs no agent and grants none any permission it
did not have: an agent that has read every word of this text is refused by exactly the same
gates as one that has read none of it. The text describes the boundary; it is not part of it.

The protocol is also not a role's instructions. It says how to hold work through this control
plane, not how to plan, review or implement anything — a project manager's or a guardian's
own skill is separate, sits on top of this, and belongs with whoever defines that role.
