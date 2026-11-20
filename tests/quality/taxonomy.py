"""Self-fixing suggestions for the test-module taxonomy gate (tm006).

When a module misses its behavioral marker or its promise, the gate prints
the exact module-level ``pytestmark`` line to add: the behavioral marker
inferred from where the module lives and what it drives, and the closest
promise ids of ``tests/quality/promise-catalog.json`` (req-* ids the module
mentions first, then ids that share tokens with the module name).
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Mapping
from pathlib import Path

from scripts.test_static_facts import drives_installed_cli

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMISE_CATALOG = REPO_ROOT / "tests" / "quality" / "promise-catalog.json"
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
