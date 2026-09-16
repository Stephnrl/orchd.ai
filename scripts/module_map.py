"""Write docs/modules.md from each module's own opening sentence.

A hand-written map of sixty modules is a map that goes wrong quietly: a module gets renamed,
another gets a new job, and the document keeps describing what used to be true. Taking each
line from the module's own docstring means the map cannot say something the code does not,
and `tests/test_module_map.py` fails when the two stop matching.
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREAMBLE = """# What each module is for

Sixty modules in one flat package is a lot to scan, and subpackages would be churn with real
risk: `broker.source_digest` names module files as an integrity input, so moving them changes
what a broker reports itself to be. The cheaper answer is a map.

Every line below is the module's own opening sentence, so this cannot drift into describing
something the code stopped doing. `tests/test_module_map.py` fails when a module is added,
removed or reworded without regenerating:

```sh
python scripts/module_map.py
```

| Module | What it is for |
| --- | --- |
"""


def summary(path):
    """The module's first docstring line, which is the one sentence it leads with."""
    doc = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8"))) or ""
    first = doc.strip().splitlines()[0].strip() if doc.strip() else ""
    if not first:
        raise SystemExit(str(path) + " has no docstring; the map is built from them")
    return first.replace("|", "\\|")


def modules():
    return sorted(path for path in (ROOT / "orch").glob("*.py") if path.name != "__init__.py")


def rendered():
    rows = ["| `orch/" + path.name + "` | " + summary(path) + " |" for path in modules()]
    return PREAMBLE + "\n".join(rows) + "\n"


def main():
    destination = ROOT / "docs" / "modules.md"
    destination.write_text(rendered(), encoding="utf-8")
    print("wrote " + str(destination) + " for " + str(len(modules())) + " modules")


if __name__ == "__main__":
    main()
