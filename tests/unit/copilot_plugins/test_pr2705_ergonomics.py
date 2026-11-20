"""CLI output / error-ergonomics folds for PR #2705.

Each test pins one user-facing string guard added while folding the
cli-logging-expert + devx-ux-expert advisory, re-expressed for the shared
native-plugin route (#3921). They are hermetic: admission is
a pure function of the ``--target copilot`` selection and no real ``copilot``
binary ever runs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.copilot_plugins.constants import ENABLED_PLUGINS_KEY, EXTRA_MARKETPLACES_KEY
from apm_cli.copilot_plugins.registrar import catalog_path_for, ledger_path_for
from apm_cli.native_plugins.binding import project_marketplace_name

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
    return project / ".github" / "copilot" / "settings.json"


# ---------------------------------------------------------------------------
# Item 1: the settings collision names the file, the key and the fix.
# ---------------------------------------------------------------------------


def test_settings_collision_names_the_file_and_the_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A same-name marketplace pointing elsewhere fails with an actionable line."""
    project = tmp_path / "project"
    source = tmp_path / "source" / "sentinel"
    write_agent_plugin(source, name="sentinel")
    _write_project(project, [str(source)])
    marketplace = project_marketplace_name("consumer")
    settings_path = _settings_path(project)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps(
            {EXTRA_MARKETPLACES_KEY: {marketplace: {"source": {"source": "git", "url": "x"}}}}
        ),
        encoding="ascii",
    )

    result = _install(monkeypatch, project)

    assert result.exit_code != 0, result.output
    flat = _flat(result.output)
    assert f"already declares {EXTRA_MARKETPLACES_KEY}.{marketplace} with another source" in flat
    assert "APM did not change it" in flat
    assert "Remove or rename that entry, then retry." in flat
    # CommandLogger word-wraps long paths, so compare with whitespace stripped.
    assert str(settings_path) in "".join(result.output.split())


# ---------------------------------------------------------------------------
# Item 3: --dry-run announces the settings change it would perform.
# ---------------------------------------------------------------------------


def test_dry_run_announces_the_legacy_registration_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``apm prune --dry-run`` names the legacy settings file it would clean."""
    project = tmp_path / "project"
    source = tmp_path / "source" / "sentinel"
    write_agent_plugin(source, name="sentinel")
    _write_project(project, [str(source)])
    assert _install(monkeypatch, project).exit_code == 0
    modules = project / "apm_modules"
    legacy_settings = project / ".github" / "copilot" / "settings.local.json"
    legacy = {
        catalog_path_for(modules): {"name": "apm", "plugins": [{"name": "sentinel"}]},
        ledger_path_for(modules): {"marketplaceOwned": True, "enabledPlugins": ["sentinel@apm"]},
        legacy_settings: {ENABLED_PLUGINS_KEY: {"sentinel@apm": True}},
    }
    for path, document in legacy.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document), encoding="ascii")
    before = {path: path.read_bytes() for path in legacy}

    result = CliRunner().invoke(cli, ["prune", "--dry-run"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert "Would move the APM Copilot plugin registration out of" in _flat(result.output)
    assert str(legacy_settings) in "".join(result.output.split())
    assert {path: path.read_bytes() for path in legacy} == before


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
    before = settings_path.read_bytes()

    result = _install(monkeypatch, project)

    assert result.exit_code != 0, result.output
    flat = _flat(result.output)
    assert "as JSON; APM did not change it. Repair the file and retry." in flat
    # The reassurance must be truthful: nothing landed on disk.
    assert settings_path.read_bytes() == before
    assert not (project / "apm_modules").exists()
    assert not (project / ".agents" / "plugins").exists()
    assert not (project / "apm.lock.yaml").exists()
    # The raw decoder message stays out of the default output.
    assert "Expecting property name" not in flat


# ---------------------------------------------------------------------------
# Item 6b: the install summary reports a count, not an inline plugin roster.
# ---------------------------------------------------------------------------


def test_install_summary_counts_the_declared_plugins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Four plugins collapse to one count line instead of listing every name."""
    project = tmp_path / "project"
    deps = []
    for name in ("alpha", "bravo", "charlie", "delta"):
        source = tmp_path / "source" / name
        write_agent_plugin(source, name=name)
        deps.append(str(source))
    _write_project(project, deps)

    result = _install(monkeypatch, project)

    assert result.exit_code == 0, result.output
    summary = next(line for line in _flat(result.output).split("[") if "native plugin(s)" in line)
    assert "4 native plugin(s) declared and enabled in Copilot settings" in summary
    assert "delta" not in summary
