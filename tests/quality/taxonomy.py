"""Messages, suggestions and the fast local check of the taxonomy gate (tm006).

When a module misses its behavioral marker or its promise, the gate prints
the exact module-level ``pytestmark`` line to add: the behavioral marker
inferred from where the module lives and what it drives, and the closest
promise ids of ``tests/quality/promise-catalog.json`` (req-* ids the module
mentions first, then ids that share tokens with the module name).

``python -m tests.quality.taxonomy --changed`` applies the same rules, by
AST only, to the test modules changed against the merge base with
``origin/main`` (committed, staged, unstaged and untracked) and prints the
same messages; ``tests/quality/test_test_taxonomy.py`` stays the gate.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from scripts.test_static_facts import drives_installed_cli, module_markers
from tests.quality.taxonomy_inventory_plugin import BEHAVIORAL_MARKERS, spawns_processes

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMISE_CATALOG = REPO_ROOT / "tests" / "quality" / "promise-catalog.json"
TAXONOMY_BASELINE = REPO_ROOT / "tests" / "quality" / "test_taxonomy_baseline.json"
CATALOG_NAME = "tests/quality/promise-catalog.json"
BASELINE_LISTS = ("misplaced_modules", "promise_exempt_modules", "unclassified_modules")
CANDIDATES = 3
_TOKEN = re.compile(r"[a-z0-9]+")
_REQUIREMENT = re.compile(r"\breq-[a-z]{2,3}-[0-9]{3}\b")
_STOP_WORDS = frozenset({"a", "and", "apm", "for", "in", "of", "on", "py", "test", "tests", "the"})


def load_promises(path: Path = PROMISE_CATALOG) -> dict[str, str]:
    """Promise id -> description, from the promise catalog."""
    return dict(json.loads(path.read_text(encoding="utf-8"))["promises"])


def infer_behavioral(module: str, tree: ast.Module) -> str:
    """The behavioral marker a module most likely needs, from its path and what it drives."""
    if module.startswith("tests/lifecycle/"):
        return "e2e"
    if module.startswith("tests/integration/"):
        return "e2e" if drives_installed_cli(tree) else "component"
    if module.startswith("tests/unit/"):
        return "unit"
    return "component"


def _tokens(text: str) -> set[str]:
    return set(_TOKEN.findall(text.lower())) - _STOP_WORDS


def candidate_promises(
    module: str, source: str, promises: Mapping[str, str], limit: int = CANDIDATES
) -> list[str]:
    """Up to *limit* catalog ids for *module*: mentioned req-* ids, then name-token matches."""
    mentioned = [rid for rid in dict.fromkeys(_REQUIREMENT.findall(source)) if rid in promises]
    name = _tokens(Path(module).stem)

    def score(pid: str) -> int:
        return 2 * len(name & _tokens(pid)) + len(name & _tokens(promises[pid]))

    ranked = sorted(
        (pid for pid in promises if pid not in mentioned and score(pid) > 0),
        key=lambda pid: (-score(pid), pid),
    )
    return (mentioned + ranked)[:limit]


def suggested_pytestmark(
    module: str, source: str, promises: Mapping[str, str]
) -> tuple[str, list[str]]:
    """(the ``pytestmark`` line to add, its candidate promise ids)."""
    behavioral = infer_behavioral(module, ast.parse(source))
    ids = candidate_promises(module, source, promises)
    promise = ids[0] if ids else "<catalog id>"
    return f'pytestmark = [pytest.mark.{behavioral}, pytest.mark.promise("{promise}")]', ids


def fix_hint(module: str, root: Path, promises: Mapping[str, str]) -> str:
    """The suggestion appended to a tm006 failure for *module* (relative to *root*)."""
    path = root / module
    if not path.is_file():
        return ""
    source = path.read_text(encoding="utf-8")
    line, ids = suggested_pytestmark(module, source, promises)
    hint = f" Suggested: {line}"
    if len(ids) > 1:
        hint += f" (other candidates: {', '.join(ids[1:])})"
    return hint


# -- failure messages (shared by the gate and --changed) ------------------------


def unclassified_message(module: str, root: Path, promises: Mapping[str, str]) -> str:
    return (
        f"{module}: unclassified test module. Fix: add exactly one module-level "
        "pytestmark behavioral marker (unit, component or e2e)." + fix_hint(module, root, promises)
    )


def unit_under_integration_message(module: str) -> str:
    return (
        f"{module}: unit-marked module under tests/integration. Fix: move it to tests/unit, "
        "or mark it component or e2e if it crosses a boundary."
    )


def spawning_unit_message(module: str) -> str:
    return (
        f"{module}: tests/unit module spawns processes. Fix: move it to tests/integration "
        "(marked component or e2e), or mock the process boundary instead of spawning."
    )


def missing_promise_message(module: str, root: Path, promises: Mapping[str, str]) -> str:
    return (
        f"{module}: new test module carries no pytest.mark.promise. Fix: add "
        'pytest.mark.promise("<id>", ...) to its module-level pytestmark, naming ids from '
        f"{CATALOG_NAME}." + fix_hint(module, root, promises)
    )


def unknown_promise_message(module: str, unknown: Sequence[str]) -> str:
    return (
        f"{module}: unknown promise id(s) {sorted(unknown)}. Fix: use ids listed in "
        f"{CATALOG_NAME}, or add the promise to the catalog."
    )


# -- python -m tests.quality.taxonomy --changed ---------------------------------


def module_promises(tree: ast.Module) -> tuple[str, ...]:
    """Promise ids named by ``pytest.mark.promise(...)`` in the module-level pytestmark."""
    found: list[str] = []
    for statement in tree.body:
        targets = statement.targets if isinstance(statement, ast.Assign) else []
        if not any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in targets):
            continue
        for node in ast.walk(statement.value):
            func = node.func if isinstance(node, ast.Call) else None
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "promise"
                and isinstance(func.value, ast.Attribute)
                and func.value.attr == "mark"
            ):
                found += [a.value for a in node.args if isinstance(a, ast.Constant)]
    return tuple(sorted(set(found)))


def _git(root: Path, *args: str) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    )
    return [line for line in result.stdout.splitlines() if line]


def changed_modules(root: Path, base_ref: str) -> list[str]:
    """Test modules changed against the merge base with *base_ref*, plus untracked ones."""
    base = _git(root, "merge-base", "HEAD", base_ref)[0]
    paths = _git(root, "diff", "--name-only", "--diff-filter=ACMR", base)
    paths += _git(root, "ls-files", "--others", "--exclude-standard", "tests")
    return sorted(
        {
            path
            for path in paths
            if path.startswith("tests/")
            and Path(path).name.startswith("test_")
            and path.endswith(".py")
            and (root / path).is_file()
        }
    )


def module_findings(
    module: str,
    root: Path,
    baseline: Mapping[str, frozenset[str]],
    promises: Mapping[str, str],
) -> list[str]:
    """Every tm006 violation of one module, read from its source (no collection)."""
    source = (root / module).read_text(encoding="utf-8")
    tree = ast.parse(source)
    behavioral = sorted(module_markers(tree) & BEHAVIORAL_MARKERS)
    found: list[str] = []
    if len(behavioral) != 1 and module not in baseline["unclassified_modules"]:
        found.append(unclassified_message(module, root, promises))
    misplaced = baseline["misplaced_modules"]
    if module.startswith("tests/integration/") and behavioral == ["unit"]:
        if module not in misplaced:
            found.append(unit_under_integration_message(module))
    if module.startswith("tests/unit/") and spawns_processes(source) and module not in misplaced:
        found.append(spawning_unit_message(module))
    declared = module_promises(tree)
    if not declared and module not in baseline["promise_exempt_modules"]:
        found.append(missing_promise_message(module, root, promises))
    unknown = [pid for pid in declared if pid not in promises]
    if unknown:
        found.append(unknown_promise_message(module, unknown))
    return found


def _baseline(path: Path) -> dict[str, frozenset[str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {key: frozenset(data[key]) for key in BASELINE_LISTS}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.quality.taxonomy")
    parser.add_argument(
        "--changed",
        action="store_true",
        required=True,
        help="check only test modules changed against the merge base",
    )
    parser.add_argument("--base-ref", default="origin/main")
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    quality = root / "tests" / "quality"
    promises = load_promises(quality / "promise-catalog.json")
    baseline = _baseline(quality / "test_taxonomy_baseline.json")
    modules = changed_modules(root, args.base_ref)
    findings = [f for module in modules for f in module_findings(module, root, baseline, promises)]
    for finding in findings:
        print(f"[x] {finding}", file=sys.stderr)
    if findings:
        return 1
    print(f"[+] tm006: {len(modules)} changed test module(s) carry their marker and promise")
    return 0


if __name__ == "__main__":
    sys.exit(main())
