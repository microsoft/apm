"""The blast-radius PR slice fails closed to the full integration suite (#4117).

``scripts/blast_radius.py`` (``decide``) is the one authority for what a pull
request runs. Each branch that cannot trust a slice -- an untrusted map, a
stale or future-dated map, an unowned ``src/`` change, an empty slice and a
harness change -- must return ``_full(...)``. Each branch is its own guard, so
turning any one of them into a slice is caught on its own.
"""

from __future__ import annotations

import ast

from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import checked_facts, source_text, violation
from scripts.architecture_linter.models import Rule, Violation

RULE_ID = "contracts-tooling-blast-radius-fail-closed"
OWNER = "scripts/blast_radius.py"
WORKFLOW = ".github/workflows/ci-blast-radius.yml"
WORKFLOW_CALL = "scripts/blast_radius.py plan"
_PREFIX = "contracts-tooling-blast-radius-"
GUARD_EMPTY = f"{_PREFIX}empty-slice"
GUARD_HARNESS = f"{_PREFIX}harness-change"
GUARD_MAP_INVALID = f"{_PREFIX}map-invalid"
GUARD_MAP_STALE = f"{_PREFIX}map-stale"
GUARD_UNOWNED = f"{_PREFIX}unowned-src"

# guard -> (exact `if` test in decide, reason the branch must pass to _full)
BRANCHES: dict[str, tuple[str, str]] = {
    GUARD_HARNESS: ("harness", "REASON_HARNESS"),
    GUARD_MAP_STALE: ("map_failure == REASON_MAP_STALE", "REASON_MAP_STALE"),
    GUARD_MAP_INVALID: ("map_failure is not None or coverage_map is None", "REASON_MAP_INVALID"),
    GUARD_UNOWNED: ("unowned", "REASON_UNOWNED"),
    GUARD_EMPTY: ("not targets or (src_paths and (not covered))", "REASON_EMPTY"),
}
STALE_TEST = "generated_at > now + MAX_CLOCK_SKEW or now - generated_at > MAX_MAP_AGE"
REQUIRED_HARNESS = (
    "tests/conftest.py",
    "tests/integration/conftest.py",
    "tests/utils/*",
    "pyproject.toml",
    "uv.lock",
    "scripts/blast_radius.py",
    "scripts/ci_sharding.py",
    "scripts/ci_*.py",
    ".github/workflows/*",
    ".github/ci-durations/*",
)
NOOP_TEST = "nothing_testable_changed(changes)"


def _functions(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    return {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}


def _assigned(tree: ast.Module, name: str) -> ast.expr | None:
    for node in tree.body:
        target = node.target if isinstance(node, ast.AnnAssign) else None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        if isinstance(target, ast.Name) and target.id == name:
            return node.value
    return None


def _returns_full(branch: ast.If, reason: str) -> bool:
    statement = branch.body[0] if len(branch.body) == 1 else None
    call = statement.value if isinstance(statement, ast.Return) else None
    return (
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "_full"
        and bool(call.args)
        and isinstance(call.args[0], ast.Name)
        and call.args[0].id == reason
    )


def _branch_findings(decide: ast.FunctionDef) -> list[tuple[str, str]]:
    branches = [node for node in decide.body if isinstance(node, ast.If)]
    findings: list[tuple[str, str]] = []
    for guard, (test, reason) in BRANCHES.items():
        if not any(ast.unparse(b.test) == test and _returns_full(b, reason) for b in branches):
            findings.append(
                (guard, f"decide must run the full suite: `if {test}: return _full({reason}, ...)`")
            )
    noop_returns = [
        node
        for node in ast.walk(decide)
        if isinstance(node, ast.Return) and "MODE_NOOP" in ast.unparse(node)
    ]
    noop_branches = [b for b in branches if ast.unparse(b.test) == NOOP_TEST]
    if (
        len(noop_returns) != 1
        or not noop_branches
        or noop_branches[0].body[0] is not noop_returns[0]
    ):
        findings.append((GUARD_EMPTY, f"decide may skip the run only under `if {NOOP_TEST}:`"))
    return findings


def _module_findings(tree: ast.Module) -> list[tuple[str, str]]:
    findings: list[tuple[str, str]] = []
    functions = _functions(tree)
    full = functions.get("_full")
    if full is None or "Decision(MODE_FULL" not in ast.unparse(full):
        findings.append((GUARD_MAP_INVALID, "_full must return Decision(MODE_FULL, ...)"))
    age = _assigned(tree, "MAX_MAP_AGE")
    load_map = functions.get("load_map")
    stale_tests = (
        [ast.unparse(n.test) for n in ast.walk(load_map) if isinstance(n, ast.If)]
        if load_map
        else []
    )
    if age is None or ast.unparse(age) != "timedelta(hours=48)" or STALE_TEST not in stale_tests:
        findings.append((GUARD_MAP_STALE, f"load_map must reject maps when `{STALE_TEST}` (48 h)"))
    harness = _assigned(tree, "HARNESS_PATTERNS")
    declared = (
        {e.value for e in harness.elts if isinstance(e, ast.Constant)}
        if isinstance(harness, ast.Tuple)
        else set()
    )
    missing = [pattern for pattern in REQUIRED_HARNESS if pattern not in declared]
    if missing:
        findings.append((GUARD_HARNESS, f"HARNESS_PATTERNS must list {', '.join(missing)}"))
    decide = functions.get("decide")
    if decide is None:
        return [*findings, *((guard, "decide must exist") for guard in BRANCHES)]
    return [*findings, *_branch_findings(decide)]


def check_blast_radius_fail_closed(provider: FactsProvider) -> tuple[Violation, ...]:
    """Every untrusted blast-radius input runs the full integration suite."""
    findings: list[Violation] = []
    facts, failures = checked_facts(provider, OWNER, RULE_ID, require_python=True)
    findings.extend(failures)
    if facts is not None and facts.read_error is None and facts.parse_error is None:
        tree = ast.parse(source_text(facts), filename=OWNER)
        findings.extend(
            violation(RULE_ID, OWNER, f"[{guard}] {message}")
            for guard, message in _module_findings(tree)
        )
    facts, failures = checked_facts(provider, WORKFLOW, RULE_ID)
    findings.extend(failures)
    if facts is not None and facts.read_error is None and WORKFLOW_CALL not in source_text(facts):
        findings.append(
            violation(RULE_ID, WORKFLOW, f"the PR slice must be planned by `{WORKFLOW_CALL}`")
        )
    return tuple(findings)


RULES = (
    Rule(
        RULE_ID,
        "contracts_tests",
        tuple(
            sorted((GUARD_EMPTY, GUARD_HARNESS, GUARD_MAP_INVALID, GUARD_MAP_STALE, GUARD_UNOWNED))
        ),
        "The blast-radius PR slice fails closed to the full integration suite.",
        check_blast_radius_fail_closed,
    ),
)
