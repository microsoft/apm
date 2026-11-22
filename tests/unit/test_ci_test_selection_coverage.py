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
import shlex
from itertools import pairwise
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
# Programs that start a test run; ``echo pytest tests/x`` names a path but runs nothing.
_LAUNCHERS = frozenset({"uv", "uvx", "python", "python3", "pytest"})
# Options whose next token is a value, never a selected test root.
_VALUE_FLAGS = frozenset(
    {
        "-m",
        "-k",
        "-n",
        "-p",
        "-c",
        "-o",
        "-r",
        "-W",
        "--dist",
        "--ignore",
        "--ignore-glob",
        "--deselect",
        "--shard-durations",
        "--shard-count",
        "--shard-index",
        "--basetemp",
        "--rootdir",
        "--timeout",
        "--junitxml",
        "--junit-xml",
        "--cov",
        "--cov-report",
        "--cov-fail-under",
        "--durations",
        "--maxfail",
        "--tb",
        "--out",
        "--import-mode",
        "--confcutdir",
        "--extra",
        "--group",
        "--with",
    }
)
_NO_RUN = frozenset({"--collect-only", "--co"})


def _commands(run: str) -> list[list[str]]:
    """Each shell command of a step's ``run`` text, tokenized with comments dropped."""
    commands = []
    for part in re.split(r"&&|\|\||;|\|", run.replace("\\\n", " ")):
        for line in part.splitlines():
            try:
                tokens = shlex.split(line, comments=True)
            except ValueError:
                continue
            if tokens:
                commands.append(tokens)
    return commands


def _positionals(tokens: list[str]) -> list[str]:
    found, skip = [], False
    for token in tokens:
        if skip:
            skip = False
        elif token in _VALUE_FLAGS:
            skip = True
        elif not token.startswith("-"):
            found.append(token)
    return found


def command_roots(tokens: list[str]) -> set[str]:
    """Test roots one command runs: pytest, ``ci_sharding files`` or a named selection."""
    while tokens and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[0]):
        tokens = tokens[1:]  # leading environment assignments
    if not tokens or Path(tokens[0]).name not in _LAUNCHERS or _NO_RUN & set(tokens):
        return set()
    names = [Path(token).name for token in tokens]
    module = next((b for a, b in pairwise(tokens) if a == "-m"), None)
    if module == "scripts.ci_selections":
        name = tokens[tokens.index(module) + 1]
        return set(selection_roots(name)) if name in SELECTIONS else set()
    if module == "scripts.ci_sharding":
        rest = tokens[tokens.index(module) + 1 :]
        args = rest[1:] if rest[:1] == ["files"] else []
    elif "pytest" in names:
        args = tokens[names.index("pytest") + 1 :]
    else:
        return set()
    return {
        arg.split("::", 1)[0].rstrip("/") for arg in _positionals(args) if arg.startswith("tests/")
    }


def workflow_test_roots(workflows: Path) -> set[str]:
    """Test roots every workflow step's commands run."""
    roots: set[str] = set()
    for path in sorted(workflows.glob("*.y*ml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for job in (document.get("jobs") or {}).values():
            for step in job.get("steps") or []:
                for tokens in _commands(str(step.get("run", ""))):
                    roots |= command_roots(tokens)
    return roots


def _under(relative: str, root: str) -> bool:
    return relative == root or relative.startswith(root + "/")


def unselected_modules(tests: Path, roots: set[str]) -> list[str]:
    """Test modules under *tests* that no root covers (paths relative to its parent)."""
    base = tests.parent
    found = []
    for path in sorted(tests.rglob("*.py")):
        relative = path.relative_to(base).as_posix()
        if not is_test_module_path(path) or any(_under(relative, own) for own in OWN_JOBS):
            continue
        if not any(_under(relative, root) for root in roots):
            found.append(relative)
    return found


def test_every_test_module_is_selected_by_a_ci_job() -> None:
    orphans = unselected_modules(ROOT / "tests", workflow_test_roots(WORKFLOWS))
    assert not orphans, (
        "test modules no CI job selects (move them under tests/unit/<area>/ or add "
        f"them to a job selection): {orphans}"
    )


def test_the_guard_reports_a_module_outside_every_root(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    for relative in (
        "unit/test_a.py",
        "test_top.py",
        "integration/test_i.py",
        "integration_extra/test_x.py",
        "unit/helper.py",
    ):
        (tests / relative).parent.mkdir(parents=True, exist_ok=True)
        (tests / relative).write_text("", encoding="utf-8")
    # A sibling of an own-job tree is not exempt (directory boundary, not prefix).
    assert unselected_modules(tests, {"tests/unit"}) == [
        "tests/integration_extra/test_x.py",
        "tests/test_top.py",
    ]
    roots = {"tests/unit", "tests/test_top.py", "tests/integration_extra"}
    assert unselected_modules(tests, roots) == []


@pytest.mark.parametrize(
    ("line", "roots"),
    [
        ("uv run pytest tests/unit tests/extra.py -n 2", {"tests/unit", "tests/extra.py"}),
        ("uv run pytest tests/node.py::test_one", {"tests/node.py"}),
        (
            "uv run python -m pytest tests/spec -p scripts.ci_sharding --shard-count 3",
            {"tests/spec"},
        ),
        ("python -m scripts.ci_sharding files --out f.txt tests/sharded", {"tests/sharded"}),
        ("uv run pytest tests/unit --ignore tests/test_orphan.py", {"tests/unit"}),
        ("uv run pytest tests/unit  # tests/test_orphan.py", {"tests/unit"}),
        ("echo 'pytest tests/test_orphan.py'", set()),
        ("echo pytest tests/test_orphan.py", set()),
        ("uv run pytest tests/test_orphan.py --collect-only -q", set()),
        ("python -m scripts.ci_selections no-such-selection", set()),
    ],
)
def test_only_commands_that_run_tests_select_roots(line: str, roots: set[str]) -> None:
    assert set().union(*(command_roots(tokens) for tokens in _commands(line))) == roots


def test_a_selection_counts_only_where_a_workflow_invokes_it(tmp_path: Path) -> None:
    name = next(iter(SELECTIONS))
    steps = [
        {"run": "echo tests/echoed"},
        {"run": f"uv run --frozen python -m scripts.ci_selections {name} --shard-count 3"},
    ]
    workflow = {"on": {"push": {"paths": ["tests/ignored/**"]}}, "jobs": {"u": {"steps": steps}}}
    (tmp_path / "ci.yml").write_text(yaml.safe_dump(workflow), encoding="utf-8")
    assert workflow_test_roots(tmp_path) == {r.rstrip("/") for r in selection_roots(name)}
    (tmp_path / "ci.yml").write_text(yaml.safe_dump({"jobs": {"u": {"steps": steps[:1]}}}))
    assert workflow_test_roots(tmp_path) == set()
