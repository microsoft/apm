"""Static boundary for explicit, legacy-accurate exemption semantics."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LINTER_ROOT = ROOT / "scripts/architecture_linter"

POLICY_KEYWORDS = {
    "line_pattern_violations": "exempt_marker",
    "_forbid_scan": "exempt",
    "_duplicate_scan": "exempt",
    "_line_findings": "respect_exempt",
    "_duplicate_definition_lines": "respect_exempt",
    "_banned": "respect_exempt",
    "_forbid_pattern": "exempt",
}


def _python_trees() -> tuple[tuple[Path, ast.Module], ...]:
    return tuple(
        (path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        for path in sorted(LINTER_ROOT.rglob("*.py"))
    )


def test_every_exemption_aware_check_declares_its_policy_explicitly() -> None:
    """No migrated check may inherit an exemption default accidentally."""
    missing: list[str] = []
    for path, tree in _python_trees():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            keyword = POLICY_KEYWORDS.get(node.func.id)
            if node.func.id == "TreeScan":
                keyword = "respect_exempt"
            if keyword is None:
                continue
            if any(item.arg == keyword for item in node.keywords):
                continue
            missing.append(f"{path.relative_to(ROOT)}:{node.lineno}:{node.func.id}")

    assert missing == []


def test_exemption_helpers_offer_no_implicit_policy_default() -> None:
    """A new caller must choose exemptible or non-exemptible at review time."""
    defaults: list[str] = []
    for path, tree in _python_trees():
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for argument, default in zip(
                node.args.kwonlyargs,
                node.args.kw_defaults,
                strict=True,
            ):
                if argument.arg in {"exempt", "respect_exempt", "exempt_marker"} and default:
                    defaults.append(
                        f"{path.relative_to(ROOT)}:{node.lineno}:{node.name}.{argument.arg}"
                    )

    assert defaults == []


def test_legacy_non_exempt_transport_scans_are_pinned_false() -> None:
    """The old bare-grep checks cannot be waived by an inline marker.

    The four tokens below moved out of the former monolithic
    ``transport_platform_analyzers.py`` into the cohesive
    ``transport_auth_platform.py`` / ``transport_network_and_runtime.py``
    check-family modules; this scans both split-out files (plus any other
    module in ``checks/``) rather than pinning a single obsolete path, so a
    future re-split cannot silently drop the assertion.
    """
    candidate_paths = tuple(sorted((LINTER_ROOT / "checks").glob("transport_*.py")))
    assert candidate_paths, "expected at least one transport check module"
    trees = tuple(
        (path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        for path in candidate_paths
    )
    expected_tokens = (
        "_SYMLINK_DEF",
        "_TRANSPORT_SELECTION",
        "_SEMVER_REF_KIND",
        "is_generic or is_azure_devops_hostname",
    )

    for token in expected_tokens:
        candidates = [
            call
            for _, tree in trees
            for call in ast.walk(tree)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "_forbid_scan"
            and token in ast.unparse(call)
        ]
        assert len(candidates) == 1, token
        exemption = next(
            keyword.value for keyword in candidates[0].keywords if keyword.arg == "exempt"
        )
        assert isinstance(exemption, ast.Constant)
        assert exemption.value is False
