"""Static regression traps for marketplace locked-evidence consumers."""

import ast
from pathlib import Path

import pytest

from scripts.architecture_linter.checks.marketplace_locked_evidence import ORIGIN_RULE_ID, RULE_ID
from scripts.architecture_linter.runner import run_selected_rules

pytestmark = pytest.mark.component

_ROOT = Path(__file__).parents[2]


def test_marketplace_locked_evidence_routes_through_owners() -> None:
    """The real source tree must satisfy every scoped canonical-owner route."""
    report = run_selected_rules(_ROOT, (RULE_ID,))
    assert not report.violations
    assert not report.failures


@pytest.mark.parametrize(
    ("path", "before", "after"),
    [
        ("policy/ci_checks.py", "marketplace_ref_mismatch(dep_ref, locked_dep)", "None"),
        ("policy/ci_checks.py", "lock.get_dependency(", "lock.dependencies.get("),
        ("install/plan.py", "lockfile.get_dependency(", "lockfile.dependencies.get("),
        ("drift.py", "marketplace_ref_mismatch(dep_ref, locked_dep)", "None"),
        ("drift.py", "satisfies_range(version, spec)", "True"),
        ("marketplace/resolver.py", "build_tag_regex(", "re.compile("),
    ],
)
def test_marketplace_locked_evidence_guard_rejects_bypasses(
    path: str, before: str, after: str
) -> None:
    """In-memory mutations avoid whole-repository copies or source edits."""
    source_path = f"src/apm_cli/{path}"
    source = (_ROOT / source_path).read_text(encoding="utf-8")
    assert before in source
    report = run_selected_rules(
        _ROOT, (RULE_ID,), source_overrides={source_path: source.replace(before, after)}
    )
    assert any(item.rule_id == RULE_ID for item in report.violations)


def test_marketplace_catalog_origin_routes_through_owners() -> None:
    """The real source tree satisfies the catalog-origin owner routes (#3912)."""
    report = run_selected_rules(_ROOT, (ORIGIN_RULE_ID,))
    assert not report.violations
    assert not report.failures


@pytest.mark.parametrize(
    ("path", "before", "after"),
    [
        (
            "marketplace/resolver.py",
            "source_url = marketplace_origin(source)",
            "source_url = manifest.source_url",
        ),
        (
            "marketplace/client.py",
            "        source_digest = catalog_digest(data)\n",
            "        pass\n",
        ),
        (
            "marketplace/catalog_origin.py",
            "for dep in lockfile.marketplace_rows(marketplace_name, plugin_name)",
            "for dep in lockfile.dependencies.values()",
        ),
        (
            "marketplace/catalog_origin.py",
            "if not same_marketplace_origin(locked_origin, current):",
            "if locked_origin != current:",
        ),
        (
            "commands/marketplace/__init__.py",
            "        _warn_if_duplicate_origin(logger, final_source)\n",
            "",
        ),
    ],
)
def test_marketplace_catalog_origin_guard_rejects_bypasses(
    path: str, before: str, after: str
) -> None:
    """Recording or comparing the alias instead of the catalog origin is a violation."""
    source_path = f"src/apm_cli/{path}"
    source = (_ROOT / source_path).read_text(encoding="utf-8")
    assert source.count(before) == 1
    mutated = source.replace(before, after)
    ast.parse(mutated)  # a syntax error would trip the rule for the wrong reason
    report = run_selected_rules(_ROOT, (ORIGIN_RULE_ID,), source_overrides={source_path: mutated})
    assert any(item.rule_id == ORIGIN_RULE_ID for item in report.violations)


def test_marketplace_catalog_origin_guard_requires_check_before_catalog_io() -> None:
    """The alias origin check must run before the catalog is fetched."""
    source_path = "src/apm_cli/deps/apm_resolver.py"
    source = (_ROOT / source_path).read_text(encoding="utf-8")
    guard = (
        "        require_locked_catalog_origin(\n"
        "            dep_ref.marketplace_name, dep_ref.marketplace_plugin_name, "
        "self._existing_lockfile\n"
        "        )\n"
    )
    assert source.count(guard) == 1
    moved = source.replace(guard, "").replace(
        "        return resolved\n\n    def _resolve_marketplace_or_record_error",
        guard + "        return resolved\n\n    def _resolve_marketplace_or_record_error",
    )
    assert moved != source.replace(guard, "")
    ast.parse(moved)
    report = run_selected_rules(_ROOT, (ORIGIN_RULE_ID,), source_overrides={source_path: moved})
    assert any(
        item.rule_id == ORIGIN_RULE_ID and "before" in item.message for item in report.violations
    )
