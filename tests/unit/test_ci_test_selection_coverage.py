"""Every test module under tests/ is selected by some CI job (#4736).

Sixteen top-level ``tests/test_*.py`` modules sat outside every job selection,
so a stale failure in one of them went unnoticed. This guard reads the test
roots each workflow's pytest, sharding and selection commands name, plus the
canonical selections (``scripts/ci_selections.py``), and fails on any test
module none of them covers. The integration, lifecycle, benchmark and perf
trees have their own jobs and planners, so they are out of scope here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from scripts.ci_selections import SELECTIONS, selection_roots
from scripts.test_file_inventory import is_test_module_path

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-OWN-ci-validation-topology")]

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
# Trees whose own jobs select them (ci-integration, lifecycle shards, perf lanes).
OWN_JOBS = ("tests/integration", "tests/lifecycle", "tests/benchmarks", "tests/perf")
_RUNNER = re.compile(r"pytest|scripts\.ci_sharding|scripts\.ci_selections")
_TEST_PATH = re.compile(r"(?<![\w/.-])tests/[^\s'\";]+")


def workflow_test_roots(workflows: Path) -> set[str]:
    """Test paths named on a workflow line that runs or shards pytest."""
    roots: set[str] = set()
    for path in sorted(workflows.glob("*.y*ml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for job in (document.get("jobs") or {}).values():
            for step in job.get("steps") or []:
                run = str(step.get("run", "")).replace("\\\n", " ")
                for line in run.splitlines():
                    if _RUNNER.search(line.split("#", 1)[0]):
                        roots.update(match.split("::", 1)[0] for match in _TEST_PATH.findall(line))
    return roots


def selected_roots() -> set[str]:
    roots = workflow_test_roots(WORKFLOWS)
    for name in SELECTIONS:
        roots.update(selection_roots(name))
    return {root.rstrip("/") for root in roots}


def unselected_modules(tests: Path, roots: set[str]) -> list[str]:
    """Test modules under *tests* that no root covers (paths relative to its parent)."""
    base = tests.parent
    found = []
    for path in sorted(tests.rglob("*.py")):
        relative = path.relative_to(base).as_posix()
        if not is_test_module_path(path) or relative.startswith(OWN_JOBS):
            continue
        if not any(relative == root or relative.startswith(root + "/") for root in roots):
            found.append(relative)
    return found


def test_every_test_module_is_selected_by_a_ci_job() -> None:
    orphans = unselected_modules(ROOT / "tests", selected_roots())
    assert not orphans, (
        "test modules no CI job selects (move them under tests/unit/<area>/ or add "
        f"them to a job selection): {orphans}"
    )


def test_the_guard_reports_a_module_outside_every_root(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    for relative in ("unit/test_a.py", "test_top.py", "integration/test_i.py", "unit/helper.py"):
        (tests / relative).parent.mkdir(parents=True, exist_ok=True)
        (tests / relative).write_text("", encoding="utf-8")
    assert unselected_modules(tests, {"tests/unit"}) == ["tests/test_top.py"]
    assert unselected_modules(tests, {"tests/unit", "tests/test_top.py"}) == []


def test_only_runner_lines_select_test_paths(tmp_path: Path) -> None:
    workflow = {
        "on": {"push": {"paths": ["tests/ignored/**"]}},
        "jobs": {
            "unit": {
                "steps": [
                    {"run": "echo tests/echoed\nuv run pytest tests/unit \\\n  tests/extra.py"},
                    {"run": "python -m scripts.ci_sharding files --out f.txt tests/sharded"},
                    {"run": "uv run pytest tests/node.py::test_one -n 2"},
                ]
            }
        },
    }
    (tmp_path / "ci.yml").write_text(yaml.safe_dump(workflow), encoding="utf-8")
    assert workflow_test_roots(tmp_path) == {
        "tests/unit",
        "tests/extra.py",
        "tests/sharded",
        "tests/node.py",
    }
