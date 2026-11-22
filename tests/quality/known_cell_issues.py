"""Tracking issues of every strict known cell and their recorded GitHub state (#4565).

A strict known cell is a lifecycle known cell (generated obligations, journey,
machine and scenario known cells, table-level machine and scenario rows, rule
cells) or any strict xfail in ``tests/``, whether a ``KnownFailure`` (kind
``bug`` or ``deferred-design``) or a raw mark.

A strict known cell stays a known failure only while its tracking issue is
open. Reading issue state needs the network, so the gate
(``test_known_cells_cite_open_issues.py``) reads an offline snapshot,
``known_cell_issue_states.json``. Refresh it with gh after a cell or an issue
changes:

    python -m tests.quality.known_cell_issues --write

The snapshot only moves when someone runs ``--write``, so an issue closed on
GitHub would not trip it. ``--check-live`` reads the live state of every cited
issue in one batched GraphQL request (``gh api graphql``: ``GH_TOKEN`` or
``GITHUB_TOKEN`` in CI, the gh login locally)
and fails naming each known cell whose issue is closed:

    python -m tests.quality.known_cell_issues --check-live
"""

from __future__ import annotations

import ast
import importlib
import json
import pkgutil
import re
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

ROOT = Path(__file__).parents[2]
SNAPSHOT_PATH = Path(__file__).with_name("known_cell_issue_states.json")
GENERATED_PATH = Path(__file__).with_name("generated_obligations.json")
REPO = "microsoft/apm"
# GraphQL query -> the decoded response; tests pass a stub.
Transport = Callable[[str], Mapping]
# KnownFailure itself and the gate that exercises it declare no known cell.
_NOT_CELLS = frozenset(
    {
        ROOT / "tests" / "lifecycle" / "known.py",
        Path(__file__).with_name("test_known_cells_cite_open_issues.py"),
    }
)


def _machine_cells() -> Iterable[tuple[str, str]]:
    from tests.lifecycle import machines

    for info in pkgutil.iter_modules(machines.__path__):
        if not info.name.startswith(("test_", "_")):
            continue
        module = importlib.import_module(f"{machines.__name__}.{info.name}")
        for cell in getattr(module, "KNOWN_CELLS", ()):
            yield f"machine:{cell.cell_id}", cell.tracking


def _literal(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _keyword(call: ast.Call, name: str) -> ast.AST | None:
    return next((kw.value for kw in call.keywords if kw.arg == name), None)


def _is_xfail(func: ast.AST) -> bool:
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "xfail"
        and isinstance(func.value, ast.Attribute)
        and func.value.attr == "mark"
    )


# Constructors of a strict known cell -> the positional index of its tracking issue.
# KnownFailure: tables and unit tests; Known / known_defect: scenarios (a scenario
# credit takes its tracking from a Known); KnownCell: machines.
_TRACKING_ARG = {"KnownFailure": 0, "Known": 0, "known_defect": 0, "KnownCell": 2}


def _constants(tree: ast.Module) -> dict[str, str]:
    """Module-level ``NAME = "text"`` constants, so ``Known(_ISSUE, ...)`` resolves."""
    found: dict[str, str] = {}
    for statement in tree.body:
        value = getattr(statement, "value", None)
        targets = statement.targets if isinstance(statement, ast.Assign) else []
        if isinstance(statement, ast.AnnAssign):
            targets = [statement.target]
        for target in targets:
            if isinstance(target, ast.Name) and _literal(value) is not None:
                found[target.id] = _literal(value)
    return found


def _tracking(call: ast.Call, index: int, constants: dict[str, str]) -> str | None:
    node = call.args[index] if len(call.args) > index else _keyword(call, "tracking")
    if isinstance(node, ast.Name):
        return constants.get(node.id, "")
    if node is None or (isinstance(node, ast.Constant) and node.value is None):
        return None
    return _literal(node) if _literal(node) is not None else ""


def static_cells(root: Path = ROOT / "tests") -> dict[str, str]:
    """Strict known cells written in test source, by ``path:line``.

    Every constructor in ``_TRACKING_ARG`` and every raw strict xfail. A raw
    xfail's tracking is the ``#<n>`` its reason starts with; a tracking the scan
    cannot resolve to a literal is ``""``, which the gate rejects.
    """
    cells: dict[str, str] = {}
    for path in sorted(root.rglob("*.py")):
        if path in _NOT_CELLS:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        constants = _constants(tree)
        where = path.relative_to(ROOT).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name in _TRACKING_ARG:
                tracking = _tracking(node, _TRACKING_ARG[name], constants)
                if tracking is not None:
                    cells[f"source:{where}:{node.lineno}"] = tracking
            elif _is_xfail(func):
                strict = _keyword(node, "strict")
                if isinstance(strict, ast.Constant) and strict.value is True:
                    reason = _literal(_keyword(node, "reason")) or ""
                    found = re.match(r"#\d+", reason)
                    cells[f"source:{where}:{node.lineno}"] = found.group(0) if found else ""
    return cells


def known_cells() -> dict[str, str]:
    """``{cell label: "#<issue>"}`` for every strict known cell of the test suite."""
    from tests.lifecycle.generator.rules import RULE_CELL_KNOWN
    from tests.lifecycle.journeys.test_journeys import journey_files, load_journey

    cells: dict[str, str] = {}
    generated = json.loads(GENERATED_PATH.read_text(encoding="ascii"))["known_failing"]
    cells.update({f"generated:{key}": tracking for key, tracking in generated.items()})
    for path in journey_files():
        journey = load_journey(path)
        for cell in journey.get("known_cells") or ():
            cells[f"journey:{journey['id']}:{cell['id']}"] = str(cell["tracking"])
    cells.update(_machine_cells())
    cells.update({f"rule:{key}": tracking for key, (tracking, _) in RULE_CELL_KNOWN.items()})
    cells.update(static_cells())
    return dict(sorted(cells.items()))


def cited_issues(cells: dict[str, str] | None = None) -> list[int]:
    cells = known_cells() if cells is None else cells
    return sorted({int(t.lstrip("#")) for t in cells.values() if re.fullmatch(r"#\d+", t)})


def load_snapshot() -> dict[str, str]:
    return json.loads(SNAPSHOT_PATH.read_text(encoding="ascii"))["issues"]


def _fetch_state(number: int) -> str:
    out = subprocess.run(
        ["gh", "issue", "view", str(number), "--repo", REPO, "--json", "state", "-q", ".state"],
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


def live_query(numbers: Iterable[int], repo: str = REPO) -> str:
    """One GraphQL query that reads the state of every issue (or PR) in *numbers*."""
    owner, name = repo.split("/", 1)
    fields = " ".join(
        f"i{n}: issueOrPullRequest(number: {n}) {{ ... on Issue {{ state }} "
        f"... on PullRequest {{ state }} }}"
        for n in sorted(set(numbers))
    )
    return f'{{ repository(owner: "{owner}", name: "{name}") {{ {fields} }} }}'


def _gh_graphql(query: str) -> Mapping:
    out = subprocess.run(
        ["gh", "api", "graphql", "-f", f"query={query}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        raise RuntimeError(f"gh api graphql failed: {out.stderr.strip() or out.stdout.strip()}")
    return json.loads(out.stdout)


def live_states(numbers: Iterable[int], transport: Transport = _gh_graphql) -> dict[str, str]:
    """``{"<n>": "OPEN" | "CLOSED" | "MERGED" | "MISSING"}`` read live from GitHub."""
    numbers = sorted(set(numbers))
    if not numbers:
        return {}
    payload = transport(live_query(numbers))
    if payload.get("errors") and not payload.get("data"):
        raise RuntimeError(f"GitHub GraphQL error: {payload['errors']}")
    found = (payload.get("data") or {}).get("repository") or {}
    return {str(n): ((found.get(f"i{n}") or {}).get("state") or "MISSING") for n in numbers}


def closed_cells(cells: Mapping[str, str], states: Mapping[str, str]) -> dict[str, str]:
    """``{cell label: "#<n> (<state>)"}`` for every cell whose issue is not open."""
    return {
        label: f"{tracking} ({states.get(tracking.lstrip('#'), 'MISSING')})"
        for label, tracking in sorted(cells.items())
        if re.fullmatch(r"#\d+", tracking) and states.get(tracking.lstrip("#")) != "OPEN"
    }


def check_live(cells: Mapping[str, str] | None = None, transport: Transport = _gh_graphql) -> int:
    """Fail (1) when a known cell cites an issue that is no longer open on GitHub."""
    cells = known_cells() if cells is None else cells
    try:
        states = live_states(cited_issues(cells), transport)
    except RuntimeError as error:
        print(f"[x] could not read issue state: {error}")
        return 2
    closed = closed_cells(cells, states)
    for label, issue in closed.items():
        print(
            f"[x] {label} cites {issue}: rerun the cell; flip it to required if it now "
            "passes (or re-key it to an open issue), then run "
            "'python -m tests.quality.known_cell_issues --write'"
        )
    if not closed:
        print(f"[+] {len(cells)} known cells cite {len(states)} open issues")
    return int(bool(closed))


def main(argv: list[str]) -> int:
    if "--check-live" in argv:
        return check_live()
    issues = {str(n): _fetch_state(n) for n in cited_issues()}
    if "--write" not in argv:
        stale = issues != load_snapshot()
        print("[x] snapshot is stale; run with --write" if stale else "[+] snapshot is current")
        return int(stale)
    data = {
        "description": (
            "GitHub state of every issue a strict known cell cites; regenerate with "
            "'python -m tests.quality.known_cell_issues --write'."
        ),
        "issues": issues,
    }
    SNAPSHOT_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="ascii")
    print(f"[+] {len(issues)} issues recorded")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
