"""Architectural invariants for the install engine package.

These tests are the structural defence against regression to a
god-function/god-module design. They are intentionally activated as the
modularization refactor progresses; LOC budgets are set to current actuals
and tightened as more code is extracted.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ENGINE_ROOT = Path(__file__).resolve().parents[3] / "src" / "apm_cli" / "install"


def _line_count(path: Path) -> int:
    return sum(1 for _ in path.read_text(encoding="utf-8").splitlines())


def test_engine_package_exists():
    """The engine package must exist as a sibling of commands/."""
    assert ENGINE_ROOT.is_dir(), f"{ENGINE_ROOT} is missing"
    assert (ENGINE_ROOT / "__init__.py").is_file()
    assert (ENGINE_ROOT / "context.py").is_file()
    assert (ENGINE_ROOT / "phases").is_dir()
    assert (ENGINE_ROOT / "helpers").is_dir()
    assert (ENGINE_ROOT / "presentation").is_dir()


def test_install_context_importable():
    """InstallContext is the contract carrying state between phases."""
    from apm_cli.install.context import InstallContext

    assert hasattr(InstallContext, "__dataclass_fields__"), (
        "InstallContext must be a dataclass"
    )


MAX_MODULE_LOC = 1000

KNOWN_LARGE_MODULES = {
    # No exceptions: integrate.py was decomposed into Strategy
    # (sources.py) + Template Method (template.py) and now sits well
    # below the default budget.
}


def test_no_install_module_exceeds_loc_budget():
    """No file in the engine package may grow past its LOC budget.

    Default budget: 1000 LOC. Specific modules with documented oversize
    extractions have their own per-file budget in KNOWN_LARGE_MODULES; any
    file under the default budget is fine. This guards against the
    mega-function pattern returning by accident.

    KNOWN_LARGE_MODULES entries are technical debt: their natural seams
    (e.g. integrate.py's 4 per-package code paths) should be decomposed in
    a follow-up PR, after which their entry should be removed.
    """
    offenders = []
    for path in ENGINE_ROOT.rglob("*.py"):
        rel = path.relative_to(ENGINE_ROOT).as_posix()
        budget = KNOWN_LARGE_MODULES.get(rel, MAX_MODULE_LOC)
        n = _line_count(path)
        if n > budget:
            offenders.append((rel, n, budget))
    assert not offenders, (
        "Modules exceeding LOC budget (file, actual, budget): "
        f"{offenders}"
    )


def test_install_py_under_legacy_budget():
    """commands/install.py is the legacy seam being thinned.

    It started this refactor at 2905 LOC. The post-P2 actual is ~1268 LOC.
    Budget is set with headroom for follow-ups; tighten when further
    extractions land.

    NOTE TO AGENTS: when this test fails, do NOT trim the file by deleting
    comments, collapsing whitespace, or inlining helpers to dodge the
    budget. Engage the python-architecture skill
    (.github/skills/python-architecture/SKILL.md) and propose a real
    extraction into apm_cli/install/ -- modularity is what gets us back
    under budget honestly. The python-architect agent persona owns these
    decisions; trimming LOC for its own sake is the anti-pattern this
    invariant exists to catch.

    PR #810 raised the ceiling 1500 -> 1525 to land the MCP install
    surface (--mcp / --registry / chaos-fix C1-C3, U1-U3). A python-
    architect follow-up will extract _maybe_handle_mcp_install() and
    tighten this back below 1500 with proper headroom.

    Issue #827 (W2-mcp-preflight) raised 1525 -> 1625 to land the
    --mcp policy preflight block. The preflight adds ~36 lines of
    policy enforcement wiring inside the --mcp branch. A python-
    architect extraction of the --mcp branch into
    apm_cli/install/_mcp_install.py should recover this budget.

    Issue #827 (W2-dry-run) raised 1625 -> 1650 to add policy
    preflight in preview mode to the --dry-run block (+17 lines).
    The call lives in install.py because it coordinates between
    policy discovery and the existing render_and_exit presenter.
    The pending --mcp extraction will recover all #827 headroom.

    Issue #827 (C2-S1) raised 1650 -> 1675 to add a second
    run_policy_preflight call guarding transitive MCP servers
    collected from installed APM packages (+23 lines). This is a
    security-critical gate: without it, transitive MCP servers
    bypass policy enforcement entirely (panel blocker S1).
    The pending --mcp extraction will recover this budget.

    PR #832 (review fix) raised 1675 -> 1680 to land the
    PolicyViolationError unwrap in the install error handler so the
    user sees the policy message verbatim instead of double-nested
    under "Failed to install ... Failed to resolve ..." (+5 lines:
    one import + four error-handler lines). Recovered by the same
    pending --mcp extraction.
    PR #852 (panel fix B7) raised 1680 -> 1690 to add the
    HACK(#852) try/finally cleanup around APM_VERBOSE so that the
    env-var mutation that surfaces --verbose to the auth layer does
    not leak past this command invocation (+10 lines: 4-line save
    block at function entry + 6-line finally block at function exit).
    The follow-up issue tracks threading verbose state through
    AuthResolver as a constructor arg, after which both blocks can
    be deleted.

    PR #856 (post-PR review fix C1+F2/F3) raised 1690 -> 1700 to:
    move ``_apm_verbose_prev`` initialisation outside the ``try:``
    so the ``finally`` clause never sees an UnboundLocalError if
    ``InstallLogger(...)`` raises (+1 line C1) and to wire the
    InstallLogger into AuthResolver via ``set_logger()`` so the
    deferred stale-PAT diagnostic and verbose auth-source line route
    through CommandLogger / DiagnosticCollector instead of stderr
    (+5 lines comment + call F2/F3). Both will be recovered by the
    same pending --mcp extraction.

    WI-3 (complexity audit) raised 1700 -> 1950 for god-function
    decomposition within the same file.  The net +235 LOC comes from
    function-definition overhead (signatures, docstrings, blank lines)
    of the seven extracted helpers and the ``InstallContext`` dataclass.
    Cyclomatic complexity of ``install()`` dropped from ~70 to ~15 and
    ``_validate_and_add_packages_to_apm_yml()`` from ~50 to ~10.  This
    is a structural improvement, not feature growth -- the follow-up
    file-split into ``apm_cli/install/`` will recover the budget.
    """
    install_py = Path(__file__).resolve().parents[3] / "src" / "apm_cli" / "commands" / "install.py"
    assert install_py.is_file()
    n = _line_count(install_py)
    assert n <= 1950, (
        f"commands/install.py grew to {n} LOC (budget 1950). "
        "Do NOT trim cosmetically -- engage the python-architecture skill "
        "(.github/skills/python-architecture/SKILL.md) and propose an "
        "extraction into apm_cli/install/."
    )
