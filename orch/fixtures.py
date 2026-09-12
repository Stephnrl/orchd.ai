"""Trusted constant fixture inputs, never provider-selected executables."""
import hashlib

TEST_CODE = "from pathlib import Path\nimport sys\nvalue = Path(sys.argv[1]).read_text()\nprint('greeting:', value.strip())\nsys.exit(0 if value == 'hello world\\n' else 1)\n"
EDIT_CODE = "from pathlib import Path\nimport sys\nPath(sys.argv[1]).write_text(sys.argv[2], newline='')\n"
TEST_HASH = hashlib.sha256(TEST_CODE.encode()).hexdigest()
BASE = hashlib.sha1(b"orchd-fixture-v1").hexdigest()


def recipe():
    return {"recipe_id": "check-greeting-v1", "recipe_sha256": TEST_HASH,
            "argv": ["python", "/trusted-tests/check_greeting.py", "/workspace/greeting.txt"],
            "cwd": "workspace", "timeout_seconds": 10}


def repository(task_id):
    return {"repository_id": "fixture", "base_commit": BASE, "branch": "codex/fixture-" + task_id}
