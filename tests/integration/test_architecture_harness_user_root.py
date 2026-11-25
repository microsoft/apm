"""Architecture regression for the canonical harness user-root owner (#3745)."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.architecture_linter.runner import run_selected_rules

pytestmark = pytest.mark.component

_ROOT = Path(__file__).resolve().parents[2]
_RULE_ID = "registry_delegation.harness_user_root_resolution"
_OWNER_PATH = "src/apm_cli/integration/targets.py"
_SETTINGS_PATH = "src/apm_cli/copilot_plugins/settings.py"


def _violations(path: str, old: str, new: str) -> list[str]:
    source = (_ROOT / path).read_text(encoding="utf-8")
    mutated = source.replace(old, new, 1)
    assert mutated != source
    result = run_selected_rules(_ROOT, {_RULE_ID}, source_overrides={path: mutated})
    assert result.failures == ()
    return [item.message for item in result.violations]


def test_harness_user_root_owner_boundary_is_clean() -> None:
    result = run_selected_rules(_ROOT, {_RULE_ID})

    assert result.failures == ()
    assert result.violations == ()


@pytest.mark.parametrize(
    "variable", ["COPILOT_HOME", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "HERMES_HOME"]
)
def test_guard_rejects_a_parallel_environment_lookup(variable: str) -> None:
    messages = _violations(
        _SETTINGS_PATH,
        'return resolve_user_root("copilot")',
        f'return Path(os.environ.get("{variable}", "~/.copilot"))',
    )

    assert any(f"{variable} must be named only" in message for message in messages)
    assert any("must route through integration/targets.py" in message for message in messages)


def test_guard_requires_the_canonical_owner() -> None:
    messages = _violations(_OWNER_PATH, "def resolve_user_root(", "def resolve_harness_home(")

    assert any("resolve_user_root must be defined only" in message for message in messages)


def test_guard_requires_every_variable_in_the_owner_table() -> None:
    messages = _violations(_OWNER_PATH, 'env_var="HERMES_HOME"', 'env_var="HERMES_DIR"')

    assert any("must declare every harness user-root variable" in m for m in messages)
