"""Regression tests for #1188: install pipeline must accept YAML
list form under the singular 'target:' key.

Before the fix, the install path (``_read_yaml_targets`` ->
``parse_targets_field``) called ``str(raw)`` on a Python list, producing
the literal repr ``"['copilot', 'claude']"`` and then comma-splitting
it into garbled tokens (``"['copilot'"``, ``"'claude']"``). The dry-run
path used a different parser and silently passed, masking the bug.

These tests exercise the install-pipeline read path directly so that
any future divergence between the dry-run parser
(``target_detection.parse_target_field``) and the install parser
(``apm_yml.parse_targets_field``) is caught at unit-test level.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _ctx_with_apm_yml(tmp_path: Path, body: str) -> MagicMock:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "apm.yml").write_text(body, encoding="utf-8")
    ctx = MagicMock()
    ctx.apm_package = MagicMock()
    ctx.apm_package.package_path = project
    return ctx


def test_read_yaml_targets_accepts_flow_list_under_singular_key(tmp_path):
    """target: [copilot, claude] (flow style) must parse to a list."""
    from apm_cli.install.phases.targets import _read_yaml_targets

    ctx = _ctx_with_apm_yml(
        tmp_path,
        "name: demo\nversion: 0.1.0\ntarget: [copilot, claude]\n",
    )
    result = _read_yaml_targets(ctx)
    assert result is not None
    assert sorted(result) == ["claude", "copilot"]


def test_read_yaml_targets_accepts_block_list_under_singular_key(tmp_path):
    """target:\\n  - copilot\\n  - claude (block style) must parse to a list."""
    from apm_cli.install.phases.targets import _read_yaml_targets

    ctx = _ctx_with_apm_yml(
        tmp_path,
        "name: demo\nversion: 0.1.0\ntarget:\n  - copilot\n  - claude\n",
    )
    result = _read_yaml_targets(ctx)
    assert result is not None
    assert sorted(result) == ["claude", "copilot"]


def test_read_yaml_targets_csv_form_still_works(tmp_path):
    """Regression guard: CSV form 'target: copilot,claude' continues to work."""
    from apm_cli.install.phases.targets import _read_yaml_targets

    ctx = _ctx_with_apm_yml(
        tmp_path,
        'name: demo\nversion: 0.1.0\ntarget: "copilot,claude"\n',
    )
    result = _read_yaml_targets(ctx)
    assert result is not None
    assert sorted(result) == ["claude", "copilot"]


def test_read_yaml_targets_scalar_form_still_works(tmp_path):
    """Regression guard: scalar 'target: copilot' continues to work."""
    from apm_cli.install.phases.targets import _read_yaml_targets

    ctx = _ctx_with_apm_yml(
        tmp_path,
        "name: demo\nversion: 0.1.0\ntarget: copilot\n",
    )
    result = _read_yaml_targets(ctx)
    assert result == ["copilot"]


def test_read_yaml_targets_install_and_dry_run_parsers_agree(tmp_path):
    """Both the install parser (parse_targets_field) and the dry-run
    parser (parse_target_field) must ACCEPT the YAML list form without
    crashing and return 2 targets. The two parsers canonicalize aliases
    differently (e.g. copilot vs vscode), but that's tracked separately
    as part of consolidating onto a single parser. The point of this
    regression-trap is that both code paths agree on shape, not naming.
    """
    from apm_cli.core.apm_yml import parse_targets_field
    from apm_cli.core.target_detection import parse_target_field

    yaml_value = ["copilot", "claude"]
    install_result = parse_targets_field({"target": yaml_value})
    dry_run_result = parse_target_field(yaml_value)

    assert len(install_result) == 2
    assert len(dry_run_result) == 2
    # claude is canonical in both parsers; this is the stable cross-check.
    assert "claude" in install_result
    assert "claude" in dry_run_result


def test_read_yaml_targets_unknown_token_in_list_raises_clean_error(tmp_path):
    """Unknown token inside a YAML list must surface a readable headline,
    not a Python list-repr fragment like '['copilot''.
    """
    from apm_cli.core.errors import UnknownTargetError
    from apm_cli.install.phases.targets import _read_yaml_targets

    ctx = _ctx_with_apm_yml(
        tmp_path,
        "name: demo\nversion: 0.1.0\ntarget: [bogus]\n",
    )
    with pytest.raises(UnknownTargetError) as exc_info:
        _read_yaml_targets(ctx)
    headline = str(exc_info.value).splitlines()[0]
    assert headline == "[x] Unknown target 'bogus'"
