"""Architecture regression for the canonical APM-owned metadata root."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.architecture_linter.runner import run_selected_rules

pytestmark = pytest.mark.component

_ROOT = Path(__file__).resolve().parents[2]
_RULE_ID = "registry_delegation.apm_home_resolution"
_SCOPE_PATH = "src/apm_cli/core/scope.py"
_LIFECYCLE_PATH = "src/apm_cli/core/lifecycle_scripts.py"


def test_apm_home_owner_boundary_is_clean() -> None:
    result = run_selected_rules(_ROOT, {_RULE_ID})

    assert result.failures == ()
    assert result.violations == ()


def test_apm_home_guard_rejects_parallel_environment_lookup() -> None:
    source = (_ROOT / _LIFECYCLE_PATH).read_text(encoding="utf-8")
    mutated = source.replace(
        'return get_apm_home() / "apm.yml"',
        'return Path(os.environ.get("APM_HOME", "~/.apm")) / "apm.yml"',
        1,
    )
    assert mutated != source

    result = run_selected_rules(
        _ROOT,
        {_RULE_ID},
        source_overrides={_LIFECYCLE_PATH: mutated},
    )

    assert any("APM_HOME must be read only" in item.message for item in result.violations)


def test_apm_home_guard_requires_the_canonical_owner() -> None:
    source = (_ROOT / _SCOPE_PATH).read_text(encoding="utf-8")
    mutated = source.replace("def get_apm_home()", "def get_user_metadata_root()", 1)
    assert mutated != source

    result = run_selected_rules(
        _ROOT,
        {_RULE_ID},
        source_overrides={_SCOPE_PATH: mutated},
    )

    assert any("get_apm_home must be defined only" in item.message for item in result.violations)
