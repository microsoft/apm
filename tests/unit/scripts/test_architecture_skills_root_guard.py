"""Contract for the install-deployment-skills-root-derivation guard."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.architecture_linter.checks.install_skills_root_derivation import ALLOWLIST
from scripts.architecture_linter.runner import run_selected_rules

pytestmark = pytest.mark.component

ROOT = Path(__file__).resolve().parents[3]
RULE_ID = "install-deployment-skills-root-derivation"
PATH = "src/apm_cli/install/deployed_paths.py"


def _run(overrides: dict[str, str] | None = None):
    report = run_selected_rules(ROOT, (RULE_ID,), source_overrides=overrides)
    assert report.failures == ()
    return [v for v in report.violations if v.rule_id == RULE_ID]


def _with_extra(extra: str) -> dict[str, str]:
    return {PATH: (ROOT / PATH).read_text(encoding="utf-8") + "\n" + extra}


@pytest.mark.parametrize(
    "snippet",
    [
        'def _x(t, p):\n    return p / t.root_dir / "skills"\n',
        'def _x(p):\n    return p / ".github" / "skills"\n',
        'def _x(t, p):\n    return Path(t.deploy_root, "skills")\n',
        'def _x(t):\n    return os.path.join(t.root_dir, "skills")\n',
        'def _x(root):\n    return f"{root}/skills"\n',
        'def _x(root, n):\n    return f"{root}/skills/{n}"\n',
    ],
)
def test_open_coded_skills_root_is_flagged(snippet: str) -> None:
    violations = _run(_with_extra(snippet))
    assert [v.path for v in violations] == [PATH]


def test_package_source_layout_is_not_flagged() -> None:
    assert _run(_with_extra('def _x(p):\n    return p / ".apm" / "skills"\n')) == []


def test_allowlisted_symbol_is_not_flagged() -> None:
    path, symbol = "src/apm_cli/install/template.py", "_agent_plugin_target_skip_message"
    assert (path, symbol) in ALLOWLIST
    assert _run() == []


def test_allowlist_is_not_stale() -> None:
    """Removing any allowlist entry must surface its use, proving it is needed."""
    for key in list(ALLOWLIST):
        saved = ALLOWLIST.pop(key)
        try:
            assert any(v.path == key[0] for v in _run()), key
        finally:
            ALLOWLIST[key] = saved


def test_real_tree_is_clean() -> None:
    assert _run() == []
