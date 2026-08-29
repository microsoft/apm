"""CLI output / error-ergonomics folds for PR #2705.

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
from apm_cli.copilot_plugins.constants import EXTRA_MARKETPLACES_KEY

from ._builders import write_agent_plugin

pytestmark = pytest.mark.component


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


def _settings_path(project: Path) -> Path:
    return project / ".github" / "copilot" / "settings.local.json"


# ---------------------------------------------------------------------------
# Item 1: the settings collision surfaces verbatim, not double-wrapped.
# ---------------------------------------------------------------------------


def test_settings_collision_renders_verbatim_without_double_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A foreign 'apm' marketplace collision is shown once, un-nested.

    Dependency resolution succeeded and the lockfile was written; only the
    Copilot settings merge collided. The message must NOT be prefixed with
    'Failed to install APM dependencies' or 'Failed to resolve APM
    dependencies', which mis-attribute the failure to dependency resolution.
    """
    project = tmp_path / "project"
    source = tmp_path / "source" / "sentinel"
    write_agent_plugin(source, name="sentinel")
    _write_project(project, [str(source)])
    settings_path = _settings_path(project)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps({EXTRA_MARKETPLACES_KEY: {"apm": {"source": {"source": "git", "url": "x"}}}}),
        encoding="ascii",
    )

    result = _install(monkeypatch, project)

    assert result.exit_code != 0, result.output
    flat = _flat(result.output)
    assert "does not own it" in flat or "already defines" in flat
    assert "Failed to install APM dependencies" not in flat
    assert "Failed to resolve APM dependencies" not in flat


# ---------------------------------------------------------------------------
# Item 3: --dry-run announces the settings write it would perform.
# ---------------------------------------------------------------------------


def test_dry_run_announces_the_settings_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``apm prune --dry-run`` names the settings file it would register in.

    Prune's native resync is the reachable dry-run surface for the registration
    write: it rebuilds the APM-owned rows from surviving locked state and, in
    dry-run, must announce the settings file it would touch instead of
    returning silently.
    """
    project = tmp_path / "project"
    source = tmp_path / "source" / "sentinel"
    write_agent_plugin(source, name="sentinel")
    _write_project(project, [str(source)])
    assert _install(monkeypatch, project).exit_code == 0
    assert _settings_path(project).exists()

    monkeypatch.chdir(project)
    result = CliRunner().invoke(cli, ["prune", "--dry-run"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert "Would register 1 Agent Plugin with GitHub Copilot in" in _flat(result.output)
    # CommandLogger word-wraps long paths, so compare with whitespace stripped.
    assert str(_settings_path(project)) in "".join(result.output.split())


# ---------------------------------------------------------------------------
# Item 7: a malformed settings file yields a human line, not a decoder dump.
# ---------------------------------------------------------------------------


def test_malformed_settings_json_reports_human_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hand-authored JSONC settings file gives an actionable message."""
    project = tmp_path / "project"
    source = tmp_path / "source" / "sentinel"
    write_agent_plugin(source, name="sentinel")
    _write_project(project, [str(source)])
    settings_path = _settings_path(project)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        '{\n  // a hand-authored comment\n  "enabledPlugins": {}\n}\n',
        encoding="ascii",
    )

    result = _install(monkeypatch, project)

    assert result.exit_code != 0, result.output
    flat = _flat(result.output)
    assert "is not valid JSON" in flat
    assert "no packages were installed" in flat
    # The reassurance must be truthful: nothing landed on disk.
    assert not (project / "apm_modules").exists()
    # The raw decoder message stays behind --verbose.
    assert "Expecting property name" not in flat


def test_malformed_settings_json_keeps_decoder_detail_under_verbose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--verbose`` still exposes the raw decoder detail for debugging."""
    project = tmp_path / "project"
    source = tmp_path / "source" / "sentinel"
    write_agent_plugin(source, name="sentinel")
    _write_project(project, [str(source)])
    settings_path = _settings_path(project)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        '{\n  // a hand-authored comment\n  "enabledPlugins": {}\n}\n',
        encoding="ascii",
    )

    result = _install(monkeypatch, project, "--verbose")

    assert result.exit_code != 0, result.output
    flat = _flat(result.output)
    assert "is not valid JSON" in flat
    assert "Expecting property name" in flat


# ---------------------------------------------------------------------------
# Item 6b: the install summary caps the inline plugin roster.
# ---------------------------------------------------------------------------


def test_install_summary_caps_the_inline_plugin_roster(
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

    result = _install(monkeypatch, project)

    assert result.exit_code == 0, result.output
    flat = _flat(result.output)
    assert "Registered 4 Agent Plugins with GitHub Copilot" in flat
    assert "and 1 more" in flat
    # The fourth name is pushed to verbose_detail, not the summary line.
    summary = flat.split("Registered 4 Agent Plugins with GitHub Copilot", 1)[1]
    assert "delta" not in summary.split("[")[0]
