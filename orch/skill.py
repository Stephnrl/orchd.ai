"""Publish the agent protocol as plain context, in whatever shape an agent tool reads.

An agent container has to be told how to work through this control plane, and the tools
people actually run disagree only about *where* that text lives. A Claude-style tool
discovers `SKILL.md` by its frontmatter and loads it when the description matches; Copilot
CLI reads an instructions file every turn; an image may simply vendor a copy. None of them
disagree about the text itself, so there is one text and three placements, not three
protocols.

The text lives in `skills/orchd-protocol/` and belongs to this repository on purpose. It
states the role of every task state, the five routes, the lease bounds and the refusals —
all facts that come from the code beside it, and all facts that quietly go stale when the
protocol is documented somewhere else. `tests/test_skill_export.py` fails when they drift.

Exporting copies files. It does not install anything, run an agent, or grant one permission
it did not already have: an agent that reads this text is still refused by the same gates.
"""
import hashlib
from pathlib import Path

from .contracts import Rejected
from .maintenance import real_path

SOURCE = Path(__file__).resolve().parents[1] / "skills" / "orchd-protocol"
FILES = ("SKILL.md", "reference.md")
LAYOUTS = ("plain", "claude", "copilot")

# Injected on every turn by Copilot CLI, so it says the least it can while still sending an
# agent to the real text. The protocol itself stays in the file it points at.
POINTER = """# Working through orchd

Work in this container is held through the orchd control plane, not taken directly. Before
claiming, renewing or releasing a task, and before any action with an effect outside this
container, read `orchd-protocol/SKILL.md`. It will send you to
`orchd-protocol/reference.md` when you need the wire detail.
"""


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def contents():
    """The published text, read fresh so an edit is never masked by an earlier export.

    Line endings are normalised because the report names a SHA-256 and `--check` compares
    against it. A checkout that converts newlines would otherwise publish a different digest
    on Windows than on Linux for a file nobody edited, and a vendored copy would look stale
    to whichever host did not write it.
    """
    result = {}
    for name in FILES:
        path = SOURCE / name
        if not path.is_file():
            raise Rejected("The protocol skill is missing " + name)
        result[name] = path.read_bytes().replace(b"\r\n", b"\n")
    return result


def placement(layout):
    """Where each file goes, and which of them this command may overwrite.

    The two protocol files are ours: an export replaces them, because replacing an older
    copy is the whole point. A pointer file is the operator's — it may hold instructions we
    know nothing about — so it is written only when absent or already identical.
    """
    if layout not in LAYOUTS:
        raise Rejected("Unknown skill layout; choose one of " + ", ".join(LAYOUTS))
    published = contents()
    if layout == "plain":
        return {name: (data, True) for name, data in published.items()}
    if layout == "claude":
        return {".claude/skills/orchd-protocol/" + name: (data, True) for name, data in published.items()}
    files = {"orchd-protocol/" + name: (data, True) for name, data in published.items()}
    files[".github/copilot-instructions.md"] = (POINTER.encode("utf-8"), False)
    return files


def export(destination, layout="plain", check=False):
    """Write the protocol into `destination`, or report whether it is already current.

    `check` is for an image that vendored a copy: it says whether that copy still matches
    this control plane without changing anything, so drift is a failing check rather than a
    surprise at the first refusal.
    """
    root = real_path(destination)
    if not root.is_dir():
        raise Rejected("The skill export destination must be an existing directory")
    report, wrote, stale = [], False, False
    for relative, (data, ours) in sorted(placement(layout).items()):
        target = root / relative
        current = target.read_bytes() if target.is_file() else None
        if current == data:
            status = "current"
        elif check:
            status, stale = ("stale" if current is not None else "absent"), True
        elif current is not None and not ours:
            # Someone else's instructions file. Saying so is more useful than merging it.
            raise Rejected("Refusing to overwrite " + relative + "; add the pointer to it yourself")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            status, wrote = "written", True
        report.append({"path": relative, "sha256": sha256(data), "status": status})
    return {"kind": "SkillExport", "layout": layout, "destination": str(root), "checked": bool(check),
            "files": report, "status": "stale" if stale else ("exported" if wrote else "current"),
            "live_authorized": False}
