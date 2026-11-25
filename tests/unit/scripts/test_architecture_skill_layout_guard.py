"""Contract for the install-deployment-skill-layout guard (#3773)."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.architecture_linter.checks.install_skill_layout import ALLOWLIST
from scripts.architecture_linter.runner import run_selected_rules

pytestmark = pytest.mark.component

ROOT = Path(__file__).resolve().parents[3]
RULE_ID = "install-deployment-skill-layout"
ENGINE = "src/apm_cli/commands/uninstall/engine.py"
DRIFT = "src/apm_cli/install/drift.py"


def _run(overrides: dict[str, str] | None = None):
    report = run_selected_rules(ROOT, (RULE_ID,), source_overrides=overrides)
    assert report.failures == ()
    return [v for v in report.violations if v.rule_id == RULE_ID]


def _with_extra(path: str, extra: str) -> dict[str, str]:
    return {path: (ROOT / path).read_text(encoding="utf-8") + "\n" + extra}


def test_real_tree_is_clean() -> None:
    assert _run() == []


@pytest.mark.parametrize("path", [ENGINE, DRIFT])
def test_layout_blind_rebuild_resolution_is_flagged(path: str) -> None:
    snippet = "def _x(root):\n    return resolve_targets(root, user_scope=False)\n"
    assert [v.path for v in _run(_with_extra(path, snippet))] == [path]


def test_direct_legacy_primitive_outside_owner_is_flagged() -> None:
    path = "src/apm_cli/install/phases/targets.py"
    snippet = (
        "def _x(targets):\n"
        "    from apm_cli.integration.targets import apply_legacy_skill_paths\n"
        "    return apply_legacy_skill_paths(targets)\n"
    )
    assert [v.path for v in _run(_with_extra(path, snippet))] == [path, path]


def test_audit_targets_must_recover_the_recorded_layout() -> None:
    path = "src/apm_cli/integration/targets.py"
    source = (ROOT / path).read_text(encoding="utf-8")
    mutated = source.replace("recorded_paths=recorded_deployed_paths(lock),", "")
    assert mutated != source
    assert [v.path for v in _run({path: mutated})] == [path]


def test_allowlist_is_not_stale() -> None:
    """Removing any allowlist entry must surface its use, proving it is needed."""
    for key in list(ALLOWLIST):
        saved = ALLOWLIST.pop(key)
        try:
            assert any(v.path == key[0] for v in _run()), key
        finally:
            ALLOWLIST[key] = saved
