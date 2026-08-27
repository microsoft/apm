"""Regression folds for PR #2705 (native Agent Plugin registration).

Each test here pins one security or correctness guard added while folding the
apm-review-panel advisory. They are hermetic: the Copilot client version is
pinned through ``VERSION_OVERRIDE_ENV`` and no real ``copilot`` binary runs.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.copilot_plugins.capability import VERSION_OVERRIDE_ENV
from apm_cli.copilot_plugins.constants import ENABLED_PLUGINS_KEY, EXTRA_MARKETPLACES_KEY
from apm_cli.copilot_plugins.registrar import catalog_path_for, ledger_path_for
from apm_cli.install.native_plugin_admission import finalize_native_plugin
from apm_cli.security.executables import (
    TRUST_DENIED,
    build_exec_trust_context,
)
from apm_cli.utils.diagnostics import DiagnosticCollector

from ._builders import QUALIFIED_VERSION, read_json, write_agent_plugin

pytestmark = pytest.mark.component


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


def _install(monkeypatch, project: Path, *args: str, version: str = QUALIFIED_VERSION):
    monkeypatch.setenv(VERSION_OVERRIDE_ENV, version)
    monkeypatch.chdir(project)
    return CliRunner().invoke(
        cli,
        ["install", "--no-policy", "--target", "copilot", *args],
        catch_exceptions=False,
    )


def _settings_path(project: Path) -> Path:
    return project / ".github" / "copilot" / "settings.local.json"


def _modules(project: Path) -> Path:
    return project / "apm_modules"


# ---------------------------------------------------------------------------
# Item 3: per-dependency target narrowing excludes native registration.
# ---------------------------------------------------------------------------


def test_dependency_target_restriction_excludes_native_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plugin pinned ``targets: [claude]`` is not registered with Copilot."""
    project = tmp_path / "project"
    source = tmp_path / "source" / "sentinel"
    write_agent_plugin(source, name="sentinel")
    _write_project(project, [{"path": str(source), "targets": ["claude"]}])

    monkeypatch.setenv(VERSION_OVERRIDE_ENV, QUALIFIED_VERSION)
    monkeypatch.chdir(project)
    result = CliRunner().invoke(
        cli,
        ["install", "--no-policy", "--target", "copilot,claude"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    # The consumer excluded copilot for this dependency, so the settings
    # document is never created and no catalog is written.
    assert not _settings_path(project).exists()
    assert not catalog_path_for(_modules(project)).exists()
    assert not ledger_path_for(_modules(project)).exists()


# ---------------------------------------------------------------------------
# Item 2: a denied executable policy refuses native registration but still
# records a non-None lockfile exec_status.
# ---------------------------------------------------------------------------


def _native_package_info():
    server = SimpleNamespace(executables=(), command="printf", server_type="stdio")
    components = SimpleNamespace(mcp_servers=(server,))
    package = SimpleNamespace(agent_plugin=SimpleNamespace(components=components))
    return SimpleNamespace(
        package=package,
        dependency_ref=SimpleNamespace(canonical_string=lambda: "owner/plugin"),
    )


def test_denied_exec_policy_refuses_native_registration_and_records_status() -> None:
    """A default-deny for MCP refuses native admission; exec_status is non-None."""
    trust_ctx = build_exec_trust_context(
        policy=None,
        project_data={"executables": {"deny": {"owner/plugin": {"mcp": True}}}},
    )
    ctx = SimpleNamespace(exec_trust_ctx=trust_ctx, package_exec_status={})
    result = {"native_plugin": False}

    finalized = finalize_native_plugin(
        result,
        _native_package_info(),
        "owner/plugin",
        [SimpleNamespace(name="copilot")],
        mcp_approved=False,
        bin_approved=True,
        ctx=ctx,
        diagnostics=DiagnosticCollector(),
        logger=None,
    )

    assert finalized["native_plugin"] is False
    assert ctx.package_exec_status["owner/plugin"] == TRUST_DENIED


def test_approved_exec_policy_admits_native_registration() -> None:
    """The same plugin with MCP approved is admitted natively."""
    trust_ctx = build_exec_trust_context(
        policy=None,
        project_data={"executables": {"allow": {"owner/plugin": {"mcp": True}}}},
    )
    ctx = SimpleNamespace(exec_trust_ctx=trust_ctx, package_exec_status={})
    result = {"native_plugin": False}

    finalized = finalize_native_plugin(
        result,
        _native_package_info(),
        "owner/plugin",
        [SimpleNamespace(name="copilot")],
        mcp_approved=True,
        bin_approved=True,
        ctx=ctx,
        diagnostics=DiagnosticCollector(),
        logger=None,
    )

    assert finalized["native_plugin"] is True


def test_finalize_drops_native_when_copilot_target_excluded() -> None:
    """finalize_native_plugin refuses admission when copilot is not a target."""
    ctx = SimpleNamespace(exec_trust_ctx=None, package_exec_status={})
    result = {"native_plugin": False}

    finalized = finalize_native_plugin(
        result,
        _native_package_info(),
        "owner/plugin",
        [SimpleNamespace(name="claude")],
        mcp_approved=True,
        bin_approved=True,
        ctx=ctx,
        diagnostics=DiagnosticCollector(),
        logger=None,
    )

    assert finalized["native_plugin"] is False


# ---------------------------------------------------------------------------
# Item 4: registration runs only after the integrity gate.
# ---------------------------------------------------------------------------


def test_integrity_gate_failure_leaves_no_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A require-hashes failure leaves no marketplace, ledger, or enabledPlugins."""
    from apm_cli.install.errors import PolicyViolationError

    project = tmp_path / "project"
    source = tmp_path / "source" / "sentinel"
    write_agent_plugin(source, name="sentinel")
    _write_project(project, [str(source)])

    def _boom(_ctx):
        raise PolicyViolationError("require_hashes: missing content hash")

    monkeypatch.setattr("apm_cli.install.pipeline._enforce_require_hashes", _boom)

    result = _install(monkeypatch, project)

    assert result.exit_code != 0
    assert not catalog_path_for(_modules(project)).exists()
    assert not ledger_path_for(_modules(project)).exists()
    assert not _settings_path(project).exists()


# ---------------------------------------------------------------------------
# Item 6: the ownership ledger self-heals after ``rm -rf apm_modules``.
# ---------------------------------------------------------------------------


def test_reinstall_after_ledger_deletion_self_heals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting the ledger and reinstalling re-adopts instead of colliding."""
    project = tmp_path / "project"
    source = tmp_path / "source" / "sentinel"
    write_agent_plugin(source, name="sentinel")
    _write_project(project, [str(source)])

    assert _install(monkeypatch, project).exit_code == 0
    settings = read_json(_settings_path(project))
    assert settings[ENABLED_PLUGINS_KEY] == {"sentinel@apm": True}

    # Simulate the common recovery ritual: the ledger lives under the disposable
    # materialization root, so it is gone while the settings it governs survive.
    ledger_path_for(_modules(project)).unlink()
    catalog_path_for(_modules(project)).unlink()

    result = _install(monkeypatch, project, "--force")

    assert result.exit_code == 0, result.output
    healed = read_json(_settings_path(project))
    assert healed[ENABLED_PLUGINS_KEY] == {"sentinel@apm": True}
    assert healed[EXTRA_MARKETPLACES_KEY]["apm"]["source"] == {
        "source": "directory",
        "path": "apm_modules",
    }
    assert ledger_path_for(_modules(project)).is_file()


def test_foreign_marketplace_value_still_collides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-APM 'apm' marketplace whose value DIFFERS still hard-collides."""
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


# ---------------------------------------------------------------------------
# Items 8/11: prune never scans a still-declared plugin's inner skills.
# ---------------------------------------------------------------------------


def test_prune_never_touches_the_inner_skills_of_a_still_declared_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A declared plugin's inner skill is not an orphan and survives prune."""
    project = tmp_path / "project"
    source = tmp_path / "source" / "sentinel"
    write_agent_plugin(source, name="sentinel")
    _write_project(project, [str(source)])
    assert _install(monkeypatch, project).exit_code == 0

    inner_skill = (
        _modules(project) / "_local" / "sentinel" / "skills" / "sentinel-skill" / "SKILL.md"
    )
    assert inner_skill.is_file()

    # An unrelated true orphan that prune should remove.
    orphan = _modules(project) / "owner" / "orphan"
    orphan.mkdir(parents=True)
    (orphan / "apm.yml").write_text("name: orphan\nversion: 1.0.0\n", encoding="ascii")

    result = CliRunner().invoke(cli, ["prune"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    # The plugin's inner skill is part of the plugin unit, never a standalone
    # orphan, so prune leaves it intact.
    assert inner_skill.is_file()
    assert "sentinel-skill" not in result.output


def test_inner_skill_of_an_agent_plugin_is_recognized_as_nested(tmp_path: Path) -> None:
    """A plugin's inner skill resolves as nested under its plugin.json root.

    This pins the ``_is_agent_plugin_root(parent)`` clause in
    ``_is_nested_under_package``: without it, an Agent Plugin's inner skill
    (which carries a SKILL.md but whose root carries only plugin.json) would
    be treated as a standalone package and enter the orphan-deletion scan.
    """
    from apm_cli.commands.deps._utils import _is_nested_under_package

    modules = tmp_path / "apm_modules"
    plugin_root = modules / "_local" / "sentinel"
    write_agent_plugin(plugin_root, name="sentinel")
    inner_skill_dir = plugin_root / "skills" / "sentinel-skill"

    assert _is_nested_under_package(inner_skill_dir, modules) is True


# ---------------------------------------------------------------------------
# Item: apm deps list reports the natively registered plugin and its version.
# ---------------------------------------------------------------------------


def test_deps_list_reports_the_natively_registered_plugin_with_its_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``apm deps list`` shows the plugin name, its real version, and the line."""
    project = tmp_path / "project"
    source = tmp_path / "source" / "sentinel"
    write_agent_plugin(source, name="sentinel", version="2.3.4")
    _write_project(project, [str(source)])
    assert _install(monkeypatch, project).exit_code == 0

    result = CliRunner().invoke(cli, ["deps", "list"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert "sentinel" in result.output
    assert "2.3.4" in result.output
    assert "unknown" not in result.output
    assert "registered natively with GitHub Copilot" in result.output
