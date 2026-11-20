"""CLI output / error-ergonomics folds for PR #2705, on the #3921 route.

Each test pins one user-facing string guard added while folding the
cli-logging-expert + devx-ux-expert advisory. They are hermetic: admission is
a pure function of the ``--target copilot`` selection and no real ``copilot``
binary ever runs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.native_plugins.scopes import project_marketplace_name

from ._builders import (
    copilot_project_settings,
    native_plugins_root,
    write_agent_plugin,
)

pytestmark = pytest.mark.component

_JSONC = '{\n  // a hand-authored comment\n  "enabledPlugins": {}\n}\n'


def _flat(text: str) -> str:
    """Collapse whitespace so assertions survive terminal-width word wrapping.

    CommandLogger renders through rich, which re-wraps at the detected
    terminal width. A CI runner and a developer terminal disagree on that
    width, so a multi-word phrase can land with an embedded newline. Compare
    against the whitespace-normalized output instead of the raw buffer.
    """
    return " ".join(text.split())


def _write_project(project: Path, dependencies: list) -> None:
    project.mkdir(parents=True, exist_ok=True)
    (project / "apm.yml").write_text(
        json.dumps(
            {
                "name": "consumer",
                "version": "1.0.0",
                "description": "consumer",
                "dependencies": {"apm": dependencies},
            }
        ),
        encoding="ascii",
    )


def _install(monkeypatch, project: Path, *args: str):
    monkeypatch.chdir(project)
    return CliRunner().invoke(
        cli,
        ["install", "--no-policy", "--target", "copilot", *args],
        catch_exceptions=False,
    )


def _sentinel_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    source = tmp_path / "source" / "sentinel"
    write_agent_plugin(source, name="sentinel")
    _write_project(project, [str(source)])
    return project


# ---------------------------------------------------------------------------
# Item 1: the settings collision surfaces verbatim, not double-wrapped.
# ---------------------------------------------------------------------------


def _foreign_marketplace_project(tmp_path: Path) -> tuple[Path, str, str]:
    """A project whose Copilot settings hold APM's marketplace name with another source."""
    project = _sentinel_project(tmp_path)
    marketplace = project_marketplace_name(project, None, None)
    settings_path = copilot_project_settings(project)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    foreign = json.dumps(
        {"extraKnownMarketplaces": {marketplace: {"source": {"source": "git", "url": "x"}}}}
    )
    settings_path.write_text(foreign, encoding="ascii")
    return project, marketplace, foreign


def test_settings_collision_fails_closed_with_an_actionable_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A foreign marketplace under APM's name is refused, named once, and kept."""
    project, marketplace, foreign = _foreign_marketplace_project(tmp_path)

    result = _install(monkeypatch, project)

    assert result.exit_code != 0, result.output
    flat = _flat(result.output)
    assert flat.count("Native plugin declaration failed") == 1
    assert f"already declares extraKnownMarketplaces.{marketplace}" in flat
    assert "Failed to resolve APM dependencies" not in flat
    assert copilot_project_settings(project).read_text(encoding="ascii") == foreign
    assert not native_plugins_root(project).exists()


def test_settings_collision_renders_verbatim_without_double_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dependency resolution succeeded; only the settings declaration collided.

    The message must NOT be prefixed with 'Failed to install APM dependencies',
    which mis-attributes the failure to dependency installation.
    """
    project, _marketplace, _foreign = _foreign_marketplace_project(tmp_path)

    result = _install(monkeypatch, project)

    assert result.exit_code != 0, result.output
    assert "Failed to install APM dependencies" not in _flat(result.output)


# ---------------------------------------------------------------------------
# Item 3: --dry-run writes no activation state.
# ---------------------------------------------------------------------------


def test_dry_run_writes_no_activation_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``apm install --dry-run`` deploys nothing and touches no settings file."""
    project = _sentinel_project(tmp_path)

    result = _install(monkeypatch, project, "--dry-run")

    assert result.exit_code == 0, result.output
    assert not native_plugins_root(project).exists()
    assert not copilot_project_settings(project).exists()
    assert not (project / ".github" / "copilot" / "settings.local.json").exists()
    assert not (project / "apm.lock.yaml").exists()


# ---------------------------------------------------------------------------
# Item 7: a malformed settings file yields a human line and is not changed.
# ---------------------------------------------------------------------------


def test_malformed_settings_json_reports_human_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hand-authored JSONC settings file gives an actionable message."""
    project = _sentinel_project(tmp_path)
    settings_path = copilot_project_settings(project)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(_JSONC, encoding="ascii")

    result = _install(monkeypatch, project)

    assert result.exit_code != 0, result.output
    flat = _flat(result.output)
    assert "Native plugin declaration failed" in flat
    assert "as JSON; APM did not change it" in flat
    # The raw decoder message never reaches the user.
    assert "Expecting property name" not in flat
    assert settings_path.read_text(encoding="ascii") == _JSONC
    assert not native_plugins_root(project).exists()


# ---------------------------------------------------------------------------
# Item 6b: the native plugin roster is capped inline.
# ---------------------------------------------------------------------------


def test_deps_list_caps_the_inline_plugin_roster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Four plugins collapse to three names plus 'and 1 more' on one line."""
    project = tmp_path / "project"
    deps = []
    for name in ("alpha", "bravo", "charlie", "delta"):
        source = tmp_path / "source" / name
        write_agent_plugin(source, name=name)
        deps.append(str(source))
    _write_project(project, deps)
    assert _install(monkeypatch, project).exit_code == 0

    result = CliRunner().invoke(cli, ["deps", "list"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    flat = _flat(result.output)
    # The fourth name is pushed to verbose_detail, not the summary line.
    assert (
        "4 native plugin(s) activated from .agents/plugins: alpha, bravo, charlie, and 1 more"
        in flat
    )
    assert "plugins: alpha, bravo, charlie, delta" not in flat
