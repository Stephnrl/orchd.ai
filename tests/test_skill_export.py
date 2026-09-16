"""The published protocol says what the code does, and keeps saying it.

The text in `skills/orchd-protocol/` is the only thing an agent container is given about how
to work here. Prose drifts from code silently: a state gains a role, a lease bound moves, a
route is renamed, and the agents keep reading a document that was true once. These tests read
the published text and compare it to the constants it describes, so drift fails here rather
than in a container halfway through a working day.

They also fix the property that made plain Markdown the right choice: the same bytes are
published to a Claude-style skill directory and to a Copilot instructions layout, because the
placement differs between tools and the protocol does not.
"""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

from orch.__main__ import main
from orch import agent_sessions as sessions
from orch.agent_service import MAX_AGENTS, MAX_ROLES, ROUTES, WIRE_SCHEMA
from orch.contracts import Rejected
from orch.maintenance import real_path
from orch.skill import FILES, LAYOUTS, SOURCE, contents, export, sha256

ENVELOPE = {"schema_version", "kind", "nonce"}
COMMON = {"retry_allowed", "live_authorized"}
HEADINGS = ("State", "Route", "What it means", "")


def rows(text, heading):
    """The rows of the Markdown table under a heading, as lists of stripped cells."""
    body = text.split("## " + heading, 1)[1].split("\n## ", 1)[0]
    result = []
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("|") or set(line) <= set("|- "):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if cells[0] not in HEADINGS:
            result.append(cells)
    return result


def unticked(cell):
    return cell.replace("`", "").strip()


def named(text):
    """Every `identifier` mentioned in a fragment of the text."""
    return set(re.findall(r"`([a-z0-9_]+)`", text))


class SkillTextTests(unittest.TestCase):
    """What the published text claims, measured against the code it describes."""

    def setUp(self):
        self.skill = (SOURCE / "SKILL.md").read_text(encoding="utf-8")
        self.reference = (SOURCE / "reference.md").read_text(encoding="utf-8")

    def test_the_state_table_names_every_state_and_the_role_it_belongs_to(self):
        published = {unticked(state): unticked(role)
                     for state, role in rows(self.reference, "Which role a state belongs to")}
        self.assertEqual(published, dict(sessions.ROLE_FOR_STATE))

    def test_the_skill_names_exactly_the_roles_the_kernel_knows(self):
        listed = named(self.skill.split("The roles orchd knows are", 1)[1])
        self.assertEqual(listed, set(sessions.ROLES))

    def test_every_role_without_a_state_is_explained_rather_than_left_out(self):
        stateless = set(sessions.ROLES) - set(sessions.ROLE_FOR_STATE.values())
        explanation = self.reference.split("roles appear in no state", 1)[1].split("\n\n", 1)[0]
        self.assertEqual(stateless, named(explanation))

    def test_the_lease_table_matches_the_bounds_the_kernel_enforces(self):
        seconds = [int(value) for _, value in rows(self.reference, "Leases")]
        self.assertEqual(seconds, [sessions.MIN_LEASE, sessions.DEFAULT_LEASE, sessions.MAX_LEASE])

    def test_the_route_table_names_exactly_the_routes_that_exist(self):
        published = [unticked(route) for route, _, _ in rows(self.reference, "The five routes")]
        self.assertEqual(sorted(published), sorted(ROUTES))
        self.assertEqual(len(ROUTES), 5, "The reference calls them the five routes")

    def test_no_route_is_documented_with_a_field_its_contract_does_not_have(self):
        for route, sent, received in rows(self.reference, "The five routes"):
            request, reply = ROUTES[unticked(route)]
            for kind, cell in ((request, sent), (reply, received)):
                allowed = set(WIRE_SCHEMA["$defs"][kind].get("properties", {}))
                self.assertEqual(named(cell) - allowed, set(), kind + " has no such field")

    def test_every_required_reply_field_is_either_documented_or_common(self):
        for route, _, received in rows(self.reference, "The five routes"):
            reply = ROUTES[unticked(route)][1]
            required = set(WIRE_SCHEMA["$defs"][reply].get("required", ()))
            self.assertEqual(required - named(received) - ENVELOPE - COMMON, set(),
                             reply + " has an undocumented required field")

    def test_the_session_shape_is_documented_in_full(self):
        described = self.reference.split("A `session` describes", 1)[1].split("\n\n", 1)[0]
        self.assertEqual(set(WIRE_SCHEMA["$defs"]["Session"]["required"]) - named(described), set())

    def test_the_limits_paragraph_matches_the_service(self):
        limits = self.reference.split("## Limits worth knowing", 1)[1]
        self.assertIn("at most " + str(MAX_AGENTS) + " agents", limits)
        self.assertIn("at most " + str(MAX_ROLES) + " roles", limits)

    def test_the_skill_carries_frontmatter_naming_itself(self):
        self.assertTrue(self.skill.startswith("---\n"), "A skill file leads with its frontmatter")
        front = self.skill.split("---\n", 2)[1]
        self.assertIn("name: " + SOURCE.name, front)
        description = re.search(r"^description: (.+)$", front, re.M)
        self.assertIsNotNone(description, "A tool matches on the description, so it must exist")
        self.assertGreater(len(description.group(1)), 40)

    def test_the_published_bytes_do_not_depend_on_how_the_repository_was_checked_out(self):
        # The report names a SHA-256 and --check compares against it, so a checkout that
        # converts newlines must not make the same file hash differently per platform.
        for name, data in contents().items():
            self.assertNotIn(b"\r", data, name + " would hash differently on another host")

    def test_the_core_stays_short_enough_to_inject_every_turn(self):
        # Copilot CLI injects its instructions file on every turn, which is what bounds this.
        # The reference is read on demand and carries no such budget.
        self.assertLess(len(self.skill), 5000, "The always-loaded core has grown past its budget")


class SkillExportTests(unittest.TestCase):
    """Placing that text where each tool looks, without touching anyone else's file."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = real_path(self.temp.name)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, 'argv', ['orch', *argv]), redirect_stdout(out), redirect_stderr(err):
            try:
                main()
                code = 0
            except SystemExit as exit:
                code = exit.code
        return code, out.getvalue(), err.getvalue()

    def test_every_layout_publishes_the_same_bytes_in_its_own_place(self):
        published, written = contents(), {}
        for layout in LAYOUTS:
            destination = self.root / layout
            destination.mkdir()
            report = export(destination, layout)
            self.assertEqual(report["status"], "exported")
            for entry in report["files"]:
                data = (destination / entry["path"]).read_bytes()
                self.assertEqual(sha256(data), entry["sha256"])
                written.setdefault(Path(entry["path"]).name, set()).add(data)
        for name in FILES:
            self.assertEqual(written[name], {published[name]},
                             name + " differs between layouts; the protocol must not")

    def test_the_claude_layout_lands_where_a_skill_is_discovered(self):
        export(self.root, "claude")
        self.assertTrue((self.root / ".claude/skills/orchd-protocol/SKILL.md").is_file())

    def test_the_copilot_pointer_names_a_file_that_was_actually_written(self):
        export(self.root, "copilot")
        pointer = (self.root / ".github/copilot-instructions.md").read_text(encoding="utf-8")
        referenced = re.findall(r"`([A-Za-z0-9_./-]+\.md)`", pointer)
        self.assertTrue(referenced, "A pointer that names no file points nowhere")
        for relative in referenced:
            self.assertTrue((self.root / relative).is_file(), relative + " was never written")

    def test_an_existing_instructions_file_is_never_overwritten(self):
        (self.root / ".github").mkdir()
        mine = self.root / ".github/copilot-instructions.md"
        mine.write_text("Our own house rules.\n", encoding="utf-8")
        with self.assertRaisesRegex(Rejected, "copilot-instructions"):
            export(self.root, "copilot")
        self.assertEqual(mine.read_text(encoding="utf-8"), "Our own house rules.\n")

    def test_a_stale_protocol_file_is_replaced_because_it_is_ours(self):
        export(self.root, "plain")
        (self.root / "SKILL.md").write_text("an older protocol\n", encoding="utf-8")
        report = export(self.root, "plain")
        self.assertEqual(report["status"], "exported")
        self.assertEqual((self.root / "SKILL.md").read_bytes(), contents()["SKILL.md"])

    def test_exporting_twice_changes_nothing_the_second_time(self):
        export(self.root, "plain")
        report = export(self.root, "plain")
        self.assertEqual(report["status"], "current")
        self.assertEqual({entry["status"] for entry in report["files"]}, {"current"})

    def test_checking_reports_drift_without_writing_anything(self):
        export(self.root, "plain")
        stale = self.root / "reference.md"
        stale.write_text("an older protocol\n", encoding="utf-8")
        report = export(self.root, "plain", check=True)
        self.assertEqual(report["status"], "stale")
        self.assertEqual(stale.read_text(encoding="utf-8"), "an older protocol\n")
        self.assertEqual({entry["path"]: entry["status"] for entry in report["files"]},
                         {"SKILL.md": "current", "reference.md": "stale"})

    def test_checking_an_empty_destination_reports_absent_rather_than_writing(self):
        report = export(self.root, "plain", check=True)
        self.assertEqual(report["status"], "stale")
        self.assertEqual({entry["status"] for entry in report["files"]}, {"absent"})
        self.assertEqual(list(self.root.iterdir()), [])

    def test_an_unknown_layout_and_a_missing_destination_are_both_refused(self):
        with self.assertRaisesRegex(Rejected, "layout"):
            export(self.root, "vscode")
        with self.assertRaisesRegex(Rejected, "existing directory"):
            export(self.root / "absent", "plain")

    def test_the_cli_exports_reports_and_fails_a_stale_check(self):
        code, out, _ = self.run_cli("skill-export", "--to", str(self.root), "--layout", "claude")
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report["kind"], "SkillExport")
        self.assertIs(report["live_authorized"], False)
        code, _, _ = self.run_cli("skill-export", "--to", str(self.root), "--layout", "claude", "--check")
        self.assertEqual(code, 0)
        (self.root / ".claude/skills/orchd-protocol/SKILL.md").write_text("old\n", encoding="utf-8")
        code, out, _ = self.run_cli("skill-export", "--to", str(self.root), "--layout", "claude", "--check")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)["status"], "stale")

    def test_the_cli_insists_on_a_destination(self):
        code, _, err = self.run_cli("skill-export")
        self.assertEqual(code, 2)
        self.assertIn("--to", err)


if __name__ == "__main__":
    unittest.main()
