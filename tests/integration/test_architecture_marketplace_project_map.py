"""The root marketplaces: map guard rejects bypasses of its owner (#2903)."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.architecture_linter.checks.marketplace_project_map import RULE_ID
from scripts.architecture_linter.runner import run_selected_rules

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-marketplace-project-map")]

_ROOT = Path(__file__).resolve().parents[2]


def test_project_map_routes_through_owner() -> None:
    owner = (_ROOT / "src/apm_cli/marketplace/project_marketplaces.py").read_text(encoding="utf-8")
    assert "def bind_project_marketplaces(" in owner
    report = run_selected_rules(_ROOT, (RULE_ID,))
    assert not report.violations
    assert not report.failures


@pytest.mark.parametrize(
    ("path", "before", "after"),
    [
        (
            "deps/apm_resolver.py",
            "            require_root_declared_marketplace(\n",
            "            print(\n",
        ),
        (
            "deps/apm_resolver.py",
            "            raise DownloadedPackageError(dependency_marketplaces_error(package.name))\n",
            "            pass\n",
        ),
        (
            "install/service.py",
            "        marketplace_scope = bind_project_marketplaces(\n",
            "        marketplace_scope = nullcontext(\n",
        ),
        (
            "drift.py",
            "    return declared_origin_conflict(dep_ref.marketplace_name, locked_dep.source_url)\n",
            "    return None\n",
        ),
        (
            "marketplace/catalog_origin.py",
            "        if not project_wins_locked_origin(marketplace_name, dep.source_url):\n",
            "        if True:\n",
        ),
        (
            "marketplace/catalog_origin.py",
            "        if rebound_by_project_map(alias, dep.source_url):\n",
            "        if False:\n",
        ),
        (
            "commands/update.py",
            "            bind_project_marketplaces(marketplaces),\n",
            "            nullcontext(marketplaces),\n",
        ),
        (
            "policy/policy_checks.py",
            "marketplace_resolution_origins(lock)",
            "()",
        ),
        (
            "install/phases/policy_gate.py",
            "            marketplace_resolution_origins(\n",
            "            tuple(\n",
        ),
        (
            "commands/view.py",
            "def _display_marketplace_plugin(",
            "ORIGIN_PROJECT = 'project'\n\n\ndef _display_marketplace_plugin(",
        ),
        (
            "commands/view.py",
            "def _display_marketplace_plugin(",
            "project_marketplace_scope(())\n\n\ndef _display_marketplace_plugin(",
        ),
    ],
)
def test_project_map_guard_rejects_bypasses(path: str, before: str, after: str) -> None:
    """Dropping an enforcement, binding ad hoc or minting project sources fails."""
    source_path = f"src/apm_cli/{path}"
    source = (_ROOT / source_path).read_text(encoding="utf-8")
    assert source.count(before) == 1
    report = run_selected_rules(
        _ROOT, (RULE_ID,), source_overrides={source_path: source.replace(before, after)}
    )
    # The mutation is surgical: the linter still reads and parses every file.
    assert report.failures == ()
    assert any(item.rule_id == RULE_ID for item in report.violations)
