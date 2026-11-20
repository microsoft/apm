"""Remediation advice routes through core/remediation.py (#4090).

``contracts-tooling-remediation-advice``: a string literal under
``src/apm_cli`` that tells the user to run an ``apm`` command to clear a
reported state is free-text remediation. It must come from the
``core/remediation.py`` catalog instead, which renders it in one place and
is proven to clear that state. The matcher walks AST string literals (an
implicit concatenation is one literal; an f-string is flattened with ``{}``
for each placeholder) and skips docstrings, bare-expression strings,
``help=``/``short_help=``/``epilog=`` values and pure navigation hints
("run 'apm deps list' to see ..."). A shrink-only, per-file baseline in
``.apm/architecture/remediation-advice-baseline.json`` holds the sites not yet
migrated: a file over its count fails, and a file under its count fails until
the baseline is lowered.

``contracts-tooling-remediation-drift-kinds``: every ``DriftFinding(kind=...)``
literal emitted under ``src/apm_cli`` has exactly one ``DRIFT_KIND_REMEDIATION``
row, and every row names an emitted kind.

Regenerate the baseline with
``python -m scripts.architecture_linter.checks.contracts_remediation_advice --write-baseline``.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from collections.abc import Callable, Iterable
from pathlib import Path

from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import checked_facts, violation
from scripts.architecture_linter.models import Rule, Violation

ADVICE_RULE_ID = "contracts-tooling-remediation-advice"
DRIFT_RULE_ID = "contracts-tooling-remediation-drift-kinds"
_SRC = "src/apm_cli/"
_OWNER = "src/apm_cli/core/remediation.py"
BASELINE_PATH = ".apm/architecture/remediation-advice-baseline.json"

_ADVICE = re.compile(
    r"(?<![\w-])(?:run|rerun|re-run|try|use)\b[^.;\n]{0,40}?['\"`]?apm [a-z]", re.IGNORECASE
)
_NAVIGATION = re.compile(
    r"^[^.;\n]{0,40}?\b(?:to (?:see|list|browse|view|inspect|preview|diagnose|show|explore|learn)"
    r"|for (?:more|details))\b",
    re.IGNORECASE,
)
_EXEMPT_KEYWORDS = frozenset({"help", "short_help", "epilog"})
# Advice to run a read-only command is navigation, not remediation: it reports
# state without clearing it. A state-changing command is never in this list,
# so it cannot be laundered as navigation.
READ_ONLY_COMMANDS = frozenset(
    {
        "apm audit",
        "apm config get",
        "apm deps list",
        "apm deps tree",
        "apm deps why",
        "apm doctor",
        "apm experimental list",
        "apm list",
        "apm marketplace audit",
        "apm marketplace browse",
        "apm marketplace doctor",
        "apm marketplace list",
        "apm marketplace validate",
        "apm mcp list",
        "apm mcp search",
        "apm mcp show",
        "apm outdated",
        "apm policy explain",
        "apm policy status",
        "apm targets",
        "apm view",
    }
)
_MUTATING_FLAGS = ("--strip", "--fix", "--force", "--write")
_COMMAND = re.compile(r"apm [a-z][^'\"`\[\n;,)]*")


def literal_text(node: ast.AST) -> str | None:
    """Return the text of a str literal or a flattened f-string, else ``None``."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value if isinstance(part, ast.Constant) and isinstance(part.value, str) else "{}"
            for part in node.values
        )
    return None


def _read_only(command: str) -> bool:
    command = command.split(" to ", 1)[0].strip()
    if any(flag in command for flag in _MUTATING_FLAGS):
        return False
    return any(command == ro or command.startswith(ro + " ") for ro in READ_ONLY_COMMANDS)


def is_remediation_text(text: str) -> bool:
    """Whether *text* advises running a state-changing apm command."""
    for match in _ADVICE.finditer(text):
        if _NAVIGATION.match(text[match.end() :]):
            continue
        command = _COMMAND.match(text, match.end() - len("apm a"))
        if command is not None and _read_only(command.group(0)):
            continue
        return True
    return False


def advice_lines(
    nodes: Iterable[ast.AST], parent_of: Callable[[ast.AST], ast.AST | None]
) -> list[int]:
    """Return the line of every free-text remediation literal among *nodes*."""
    lines: list[int] = []
    for node in nodes:
        text = literal_text(node)
        if text is None:
            continue
        parent = parent_of(node)
        if isinstance(parent, ast.JoinedStr | ast.Expr):
            continue  # an f-string part, or a docstring / bare string
        if isinstance(parent, ast.keyword) and parent.arg in _EXEMPT_KEYWORDS:
            continue
        if is_remediation_text(text):
            lines.append(node.lineno)
    return lines


def _scanned(path: str) -> bool:
    return path.startswith(_SRC) and path.endswith(".py") and path != _OWNER


def _baseline(provider: FactsProvider) -> tuple[dict[str, int] | None, list[Violation]]:
    facts, failures = checked_facts(provider, BASELINE_PATH, ADVICE_RULE_ID)
    if failures:
        return None, list(failures)
    try:
        payload = json.loads("\n".join(facts.lines))
        files = payload["files"]
        if payload.get("version") != 1 or not isinstance(files, dict):
            raise ValueError("expected {'version': 1, 'files': {path: count}}")
        return {str(path): int(count) for path, count in files.items()}, []
    except (ValueError, KeyError, TypeError) as exc:
        return None, [violation(ADVICE_RULE_ID, BASELINE_PATH, f"unreadable baseline: {exc}")]


def _check_advice(provider: FactsProvider) -> Iterable[Violation]:
    baseline, findings = _baseline(provider)
    if baseline is None:
        return findings
    seen: set[str] = set()
    for path in sorted(p for p in provider.inventory if _scanned(p)):
        facts, failures = checked_facts(provider, path, ADVICE_RULE_ID, require_python=True)
        if failures:
            findings.extend(failures)
            continue
        index = facts.tree_index
        lines = [] if index is None else advice_lines(index.nodes, index.parent)
        allowed = baseline.get(path, 0)
        seen.add(path)
        if len(lines) > allowed:
            findings.extend(
                violation(
                    ADVICE_RULE_ID,
                    path,
                    "free-text remediation: render it from a core/remediation.py catalog "
                    f"entry ({len(lines)} in this file, baseline {allowed})",
                    line=line,
                )
                for line in lines
            )
        elif len(lines) < allowed:
            findings.append(
                violation(
                    ADVICE_RULE_ID,
                    BASELINE_PATH,
                    f"{path} has {len(lines)} free-text remediation site(s) but the "
                    f"baseline allows {allowed}: lower the baseline (shrink-only)",
                )
            )
    findings.extend(
        violation(ADVICE_RULE_ID, BASELINE_PATH, f"baseline names a file with no sites: {path}")
        for path, count in sorted(baseline.items())
        if path not in seen and count
    )
    return findings


# -- Drift kinds ---------------------------------------------------------------

_DRIFT_EMITTERS = (_SRC,)


def _string_values(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.IfExp):
        return _string_values(node.body) | _string_values(node.orelse)
    return set()


def _check_drift_kinds(provider: FactsProvider) -> Iterable[Violation]:
    emitted: set[str] = set()
    findings: list[Violation] = []
    for path in sorted(p for p in provider.inventory if p.startswith(_DRIFT_EMITTERS)):
        if not path.endswith(".py"):
            continue
        facts, failures = checked_facts(provider, path, DRIFT_RULE_ID, require_python=True)
        if failures:
            findings.extend(failures)
            continue
        for node in () if facts.tree_index is None else facts.tree_index.nodes:
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "DriftFinding"
            ):
                for keyword in node.keywords:
                    if keyword.arg == "kind":
                        emitted |= _string_values(keyword.value)
    facts, failures = checked_facts(provider, _OWNER, DRIFT_RULE_ID, require_python=True)
    if failures:
        return [*findings, *failures]
    rows: dict[str, int] = {}
    index = facts.tree_index
    for node in () if index is None else index.nodes:
        if (
            isinstance(node, ast.AnnAssign | ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "DRIFT_KIND_REMEDIATION"
                for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
            )
            and node.value is not None
        ):
            for inner in index.walk(node.value):
                if isinstance(inner, ast.Dict):
                    rows.update(
                        {
                            key.value: key.lineno
                            for key in inner.keys
                            if isinstance(key, ast.Constant)
                        }
                    )
    if not emitted:
        findings.append(
            violation(DRIFT_RULE_ID, _OWNER, "no DriftFinding(kind=...) emitters found")
        )
    findings.extend(
        violation(
            DRIFT_RULE_ID,
            _OWNER,
            f"drift kind {kind!r} is emitted but DRIFT_KIND_REMEDIATION has no row for it",
        )
        for kind in sorted(emitted - set(rows))
    )
    findings.extend(
        violation(
            DRIFT_RULE_ID,
            _OWNER,
            f"DRIFT_KIND_REMEDIATION row {kind!r} names no emitted drift kind",
            line=line,
        )
        for kind, line in sorted(rows.items())
        if kind not in emitted
    )
    return findings


RULES = (
    Rule(
        ADVICE_RULE_ID,
        "contracts_tests",
        (ADVICE_RULE_ID,),
        "Remediation advice is rendered from the core/remediation.py catalog only.",
        _check_advice,
    ),
    Rule(
        DRIFT_RULE_ID,
        "contracts_tests",
        (DRIFT_RULE_ID,),
        "Every emitted drift kind maps to exactly one DRIFT_KIND_REMEDIATION row.",
        _check_drift_kinds,
    ),
)


# -- Baseline writer (maintenance CLI, not a rule) -----------------------------


def count_sites(root: Path) -> dict[str, int]:
    """Count free-text remediation sites per file under *root* (same matcher)."""
    counts: dict[str, int] = {}
    for file in sorted((root / _SRC).rglob("*.py")):
        path = file.relative_to(root).as_posix()
        if not _scanned(path):
            continue
        tree = ast.parse(file.read_text(encoding="utf-8"), filename=path)
        parents: dict[ast.AST, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node
        lines = advice_lines(ast.walk(tree), parents.get)
        if lines:
            counts[path] = len(lines)
    return counts


def write_baseline(root: Path) -> dict[str, int]:
    counts = count_sites(root)
    (root / BASELINE_PATH).write_text(
        json.dumps({"version": 1, "files": counts}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return counts


if __name__ == "__main__":  # pragma: no cover - maintenance entry point
    root = Path(__file__).resolve().parents[3]
    if sys.argv[1:] == ["--write-baseline"]:
        counts = write_baseline(root)
        print(f"{sum(counts.values())} site(s) in {len(counts)} file(s) -> {BASELINE_PATH}")
    else:
        for path, count in sorted(count_sites(root).items()):
            print(f"{count:4d} {path}")
