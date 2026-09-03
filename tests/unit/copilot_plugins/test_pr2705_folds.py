"""Regression folds for PR #2705 (native Agent Plugin registration).

Each test here pins one security or correctness guard added while folding the
apm-review-panel advisory. They are hermetic: admission is a pure function of
the ``--target copilot`` selection and no real ``copilot`` binary ever runs.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.copilot_plugins.constants import ENABLED_PLUGINS_KEY, EXTRA_MARKETPLACES_KEY
from apm_cli.copilot_plugins.registrar import catalog_path_for, ledger_path_for
from apm_cli.install.native_plugin_admission import finalize_native_plugin
from apm_cli.security.executables import (
    TRUST_DENIED,
    TRUST_GATED,
    build_exec_trust_context,
)
from apm_cli.utils.diagnostics import DiagnosticCollector

from ._builders import read_json, write_agent_plugin, write_legacy_package

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


def test_project_target_excluding_copilot_skips_plugin_and_installs_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PROJECT-level ``targets: [claude]`` skips the plugin, not the batch.

    Regression for Item 4: a project that excludes copilot must treat an Agent
    Plugin as non-applicable (skip + one warning), exactly like the
    per-dependency subset already does -- NOT abort the whole install and strip
    the unrelated ordinary package with it.
    """
    project = tmp_path / "project"
    plain_src = tmp_path / "source" / "plainpkg"
    plugin_src = tmp_path / "source" / "alpha"
    write_legacy_package(plain_src, name="plainpkg")
    write_agent_plugin(plugin_src, name="alpha")
    project.mkdir(parents=True, exist_ok=True)
    (project / "apm.yml").write_text(
        json.dumps(
            {
                "name": "consumer",
                "version": "1.0.0",
                "description": "consumer",
                "targets": ["claude"],
                "dependencies": {"apm": [str(plain_src), str(plugin_src)]},
            }
        ),
        encoding="ascii",
    )

    monkeypatch.chdir(project)
    result = CliRunner().invoke(cli, ["install", "--no-policy"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    # The ordinary package installs; the plugin is skipped, not fatal.
    assert (_modules(project) / "_local" / "plainpkg").exists()
    assert not _settings_path(project).exists()
    assert not catalog_path_for(_modules(project)).exists()


# ---------------------------------------------------------------------------
# Item 1: the executable trust gate lives in candidates_from_lockfile, the ONE
# candidate source shared by install and every lifecycle resync. A locked entry
# the scanner denied or left pending approval must never reach a resync, or a
# later `apm prune` / `apm uninstall` would silently enable the very MCP server
# the install refused.
# ---------------------------------------------------------------------------


def test_resync_gate_drops_exec_blocked_locked_dependencies(tmp_path: Path) -> None:
    """A gated/denied locked dependency is excluded from resync candidates.

    Panelist reproduction: a ``LockedDependency`` with
    ``exec_status=gated_pending_approval`` (the default-deny outcome for an
    Agent Plugin MCP server) must not resurface through the lifecycle path.
    """
    from apm_cli.copilot_plugins.registrar import candidates_from_lockfile
    from apm_cli.deps.lockfile import LockedDependency

    modules = tmp_path / "apm_modules"
    gated = LockedDependency(
        repo_url="https://github.com/testowner/gatedplugin",
        resolved_commit="a" * 40,
        exec_status=TRUST_GATED,
    )
    denied = LockedDependency(
        repo_url="https://github.com/testowner/deniedplugin",
        resolved_commit="b" * 40,
        exec_status=TRUST_DENIED,
    )
    cleared = LockedDependency(
        repo_url="https://github.com/testowner/clearedplugin",
        resolved_commit="c" * 40,
        exec_status=None,
    )
    lockfile = SimpleNamespace(
        dependencies={
            "github.com/testowner/gatedplugin": gated,
            "github.com/testowner/deniedplugin": denied,
            "github.com/testowner/clearedplugin": cleared,
        }
    )

    keys = {c.dependency_key for c in candidates_from_lockfile(lockfile, modules)}

    assert "github.com/testowner/gatedplugin" not in keys
    assert "github.com/testowner/deniedplugin" not in keys
    assert "github.com/testowner/clearedplugin" in keys


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
    collector = DiagnosticCollector()

    finalized = finalize_native_plugin(
        result,
        _native_package_info(),
        "owner/plugin",
        [SimpleNamespace(name="copilot")],
        hooks_approved=True,
        mcp_approved=False,
        bin_approved=True,
        canvas_approved=True,
        lsp_approved=True,
        ctx=ctx,
        diagnostics=collector,
        logger=None,
    )

    assert finalized["native_plugin"] is False
    assert ctx.package_exec_status["owner/plugin"] == TRUST_DENIED
    # Item 6b/7c: exactly ONE user-facing refusal line, owned by the collector,
    # naming the plugin and carrying the actionable fix in the message itself.
    warnings = [d.message for d in collector._diagnostics]
    assert len(warnings) == 1
    refusal = _flat(warnings[0])
    assert "not registered with GitHub Copilot" in refusal
    assert "apm approve owner/plugin" in refusal


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
        hooks_approved=True,
        mcp_approved=True,
        bin_approved=True,
        canvas_approved=True,
        lsp_approved=True,
        ctx=ctx,
        diagnostics=DiagnosticCollector(),
        logger=None,
    )

    assert finalized["native_plugin"] is True


def test_record_native_exec_status_folds_and_never_downgrades() -> None:
    """A partial MCP grant must not downgrade a worse status the gate recorded.

    Item 2: ``check_executable_approval`` records the worst-case status over
    ALL exec types on disk (e.g. an unbounded ``hooks`` leaves
    ``gated_pending_approval``). The IR-derived status here sees only MCP/BIN,
    so an MCP-only allow yields ``deployed``. Folding must keep the more severe
    ``gated_pending_approval`` -- an assign would write a false ``deployed``
    into the audited lockfile provenance field.
    """
    from apm_cli.install.native_plugin_admission import record_native_exec_status

    trust_ctx = build_exec_trust_context(
        policy=None,
        project_data={"executables": {"allow": {"owner/plugin": {"mcp": True}}}},
    )
    # The exec gate already recorded a more severe status from the on-disk scan.
    ctx = SimpleNamespace(
        exec_trust_ctx=trust_ctx,
        package_exec_status={"owner/plugin": TRUST_GATED},
    )

    record_native_exec_status(ctx, "owner/plugin", _native_package_info(), ("mcp",))

    assert ctx.package_exec_status["owner/plugin"] == TRUST_GATED


def test_on_disk_hooks_absent_from_ir_still_refuse_native_registration(
    tmp_path: Path,
) -> None:
    """A live-loaded hooks component the IR never models must gate registration.

    Item 3: for a natively registered plugin PRESENCE IS DEPLOYMENT -- Copilot
    loads the whole apm_modules directory live. The IR only models MCP/BIN, so
    an unapproved ``hooks/hooks.json`` on disk would execute with no gate unless
    finalize unions the on-disk exec types with the IR-derived ones.
    """
    install_path = tmp_path / "plugin"
    hooks_dir = install_path / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "hooks.json").write_text("{}", encoding="ascii")

    # IR declares NO executables (empty mcp_servers), so ir_exec_types is empty
    # and only the on-disk scan can see the hooks component.
    components = SimpleNamespace(mcp_servers=())
    package = SimpleNamespace(agent_plugin=SimpleNamespace(components=components), version="1.0.0")
    package_info = SimpleNamespace(
        package=package,
        dependency_ref=SimpleNamespace(canonical_string=lambda: "owner/plugin"),
        install_path=install_path,
    )
    ctx = SimpleNamespace(exec_trust_ctx=None, package_exec_status={})
    result = {"native_plugin": False}

    finalized = finalize_native_plugin(
        result,
        package_info,
        "owner/plugin",
        [SimpleNamespace(name="copilot")],
        hooks_approved=False,
        mcp_approved=True,
        bin_approved=True,
        canvas_approved=True,
        lsp_approved=True,
        ctx=ctx,
        diagnostics=DiagnosticCollector(),
        logger=None,
    )

    assert finalized["native_plugin"] is False


def test_finalize_drops_native_when_copilot_target_excluded() -> None:
    """finalize_native_plugin refuses admission when copilot is not a target."""
    ctx = SimpleNamespace(exec_trust_ctx=None, package_exec_status={})
    result = {"native_plugin": False}

    finalized = finalize_native_plugin(
        result,
        _native_package_info(),
        "owner/plugin",
        [SimpleNamespace(name="claude")],
        hooks_approved=True,
        mcp_approved=True,
        bin_approved=True,
        canvas_approved=True,
        lsp_approved=True,
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

    # Make the inner skill orphan-SHAPED: give it its own apm.yml so that, absent
    # the ``_is_agent_plugin_root`` nesting guard, prune's package scan would see a
    # standalone, undeclared package here and delete it. With the guard intact it
    # stays recognized as part of the plugin unit.
    (inner_skill.parent / "apm.yml").write_text(
        "name: sentinel-skill\nversion: 1.0.0\n", encoding="ascii"
    )

    # An unrelated true orphan that prune should remove.
    orphan = _modules(project) / "owner" / "orphan"
    orphan.mkdir(parents=True)
    (orphan / "apm.yml").write_text("name: orphan\nversion: 1.0.0\n", encoding="ascii")

    result = CliRunner().invoke(cli, ["prune"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    # The plugin's inner skill is part of the plugin unit, never a standalone
    # orphan, so prune leaves it intact even though it now looks package-shaped.
    assert inner_skill.is_file()
    assert (inner_skill.parent / "apm.yml").is_file()
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
    assert "registered natively with GitHub Copilot" in _flat(result.output)


# ---------------------------------------------------------------------------
# Item 5: admission is a pure function of resolved target names. No lifecycle
# command may ever shell out or discover a Copilot binary to decide it -- there
# is no version or binary-presence gate on this path at all.
# ---------------------------------------------------------------------------


def _forbid_copilot_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly if the lifecycle path ever probes for a Copilot binary."""
    from apm_cli.runtime import utils as runtime_utils

    def _boom(name: str) -> str | None:
        raise AssertionError(f"unexpected runtime binary discovery for {name!r}")

    monkeypatch.setattr(runtime_utils, "find_runtime_binary", _boom)
    import subprocess as _subprocess

    def _subprocess_boom(*args, **kwargs):
        raise AssertionError(f"unexpected subprocess invocation: {args!r} {kwargs!r}")

    for attr in ("run", "Popen", "check_output", "check_call"):
        monkeypatch.setattr(_subprocess, attr, _subprocess_boom)


def test_zero_plugin_install_never_discovers_a_copilot_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``--target copilot`` install with no Agent Plugin never shells out."""
    project = tmp_path / "project"
    ordinary = tmp_path / "source" / "plainpkg"
    write_legacy_package(ordinary, name="plainpkg")
    _write_project(project, [str(ordinary)])

    _forbid_copilot_discovery(monkeypatch)
    result = _install(monkeypatch, project)

    assert result.exit_code == 0, result.output


def test_full_lifecycle_never_discovers_a_copilot_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """install -> uninstall -> prune never probe for a Copilot binary/version.

    Admission is a pure function of resolved target names: there is no binary
    probe, version check, or ``PATH`` lookup anywhere on this lifecycle path.
    """
    project = tmp_path / "project"
    source = tmp_path / "source" / "sentinel"
    write_agent_plugin(source, name="sentinel")
    _write_project(project, [str(source)])
    assert _install(monkeypatch, project).exit_code == 0

    _forbid_copilot_discovery(monkeypatch)

    uninstall = CliRunner().invoke(cli, ["uninstall", str(source)], catch_exceptions=False)
    assert uninstall.exit_code == 0, uninstall.output

    prune = CliRunner().invoke(cli, ["prune"], catch_exceptions=False)
    assert prune.exit_code == 0, prune.output


def test_update_and_frozen_restore_never_discover_a_copilot_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Update and restore converge entirely from APM-owned state."""
    project = tmp_path / "project"
    source = tmp_path / "source" / "sentinel"
    write_agent_plugin(source, name="sentinel")
    _write_project(project, [str(source)])
    assert _install(monkeypatch, project).exit_code == 0

    _forbid_copilot_discovery(monkeypatch)

    updated = CliRunner().invoke(
        cli,
        ["update", "--yes", "--target", "copilot"],
        catch_exceptions=False,
    )
    assert updated.exit_code == 0, updated.output

    catalog = catalog_path_for(project / "apm_modules")
    settings = project / ".github" / "copilot" / "settings.local.json"
    catalog.unlink()
    settings.unlink()

    restored = CliRunner().invoke(
        cli,
        ["install", "--frozen", "--no-policy", "--target", "copilot"],
        catch_exceptions=False,
    )
    assert restored.exit_code == 0, restored.output
    assert catalog.is_file()
    assert settings.is_file()


def test_transitive_dependency_is_not_marked_direct_on_the_install_path() -> None:
    """The in-flight overlay must not re-derive directness and mark everything direct.

    ``ctx.deps_to_install`` is the full transitive closure, and
    ``declaring_parent`` is only stamped on local path sub-dependencies, so
    deriving ``direct`` from it marks every transitive git dependency direct.
    That defeats both precedence gates in ``discover_native_plugins``: the
    direct-beats-transitive branch never fires, and the ledger owner-flip
    refusal (guarded on ``not entry.direct``) lets a transitive package
    silently re-point an existing registration.
    """
    from apm_cli.install.phases.copilot_plugins import resolved_candidates

    def _dep(key: str) -> SimpleNamespace:
        return SimpleNamespace(
            get_unique_key=lambda key=key: key,
            get_install_path=lambda modules, key=key: modules / key,
            declaring_parent=None,
            target_subset=None,
        )

    direct = _dep("owner/declared")
    transitive = _dep("other/pulled-in")
    ctx = SimpleNamespace(
        apm_modules_dir=Path("apm_modules"),
        lockfile=None,
        existing_lockfile=None,
        deps_to_install=[direct, transitive],
        all_apm_deps=[direct],
        package_exec_status={},
    )

    by_key = {candidate.dependency_key: candidate for candidate in resolved_candidates(ctx)}

    assert by_key["owner/declared"].direct is True
    assert by_key["other/pulled-in"].direct is False


def test_unknown_exec_status_is_not_registrable(tmp_path: Path) -> None:
    """An unrecognised exec_status fails CLOSED at the registration gate.

    The severity ladder ranks an unknown status ABOVE ``denied``, so a
    denylist gate would disagree with it and admit, on forward version skew, a
    status the ladder calls worse-than-denied. Both gates read the
    ``REGISTRABLE_EXEC_STATUSES`` allowlist instead.
    """
    from apm_cli.copilot_plugins.registrar import candidates_from_lockfile
    from apm_cli.deps.lockfile import LockedDependency

    modules = tmp_path / "apm_modules"
    lockfile = SimpleNamespace(
        dependencies={
            "github.com/testowner/futureplugin": LockedDependency(
                repo_url="https://github.com/testowner/futureplugin",
                resolved_commit="d" * 40,
                exec_status="quarantined",
            ),
        }
    )

    keys = {c.dependency_key for c in candidates_from_lockfile(lockfile, modules)}

    assert "github.com/testowner/futureplugin" not in keys


def test_in_flight_gated_dependency_is_dropped_from_install_candidates(
    tmp_path: Path,
) -> None:
    """The in-flight overlay re-adds every deps_to_install key AFTER the
    lockfile gate ran, so its sibling guard is the only thing stopping a
    package this very install gated from being registered natively.
    """
    from apm_cli.install.phases.copilot_plugins import resolved_candidates

    key = "github.com/testowner/gatedplugin"

    def _dep() -> SimpleNamespace:
        return SimpleNamespace(
            get_unique_key=lambda: key,
            get_install_path=lambda modules: modules / "gatedplugin",
            declaring_parent=None,
            target_subset=None,
        )

    ctx = SimpleNamespace(
        apm_modules_dir=tmp_path / "apm_modules",
        lockfile=None,
        existing_lockfile=None,
        deps_to_install=[_dep()],
        all_apm_deps=[_dep()],
        package_exec_status={key: TRUST_GATED},
    )

    assert [c.dependency_key for c in resolved_candidates(ctx)] == []


def test_root_layout_components_absent_from_ir_refuse_native_registration(
    tmp_path: Path,
) -> None:
    """NATIVE root-layout components are exec-gated too.

    ``scan_package_executables`` only understands APM's ``.apm/`` layout, so a
    skills-only Agent Plugin carrying root ``extensions/``, ``lsp.json`` or
    ``hooks/`` used to reach native registration with zero exec approvals --
    even though Copilot loads the whole directory live.
    """
    from apm_cli.install.native_plugin_admission import plugin_root_exec_types

    root = tmp_path / "plugin"
    (root / "extensions" / "x").mkdir(parents=True)
    (root / "extensions" / "x" / "extension.mjs").write_text("//\n", encoding="ascii")
    (root / "lsp.json").write_text("{}\n", encoding="ascii")
    (root / "hooks").mkdir()
    (root / "hooks" / "pre.sh").write_text("#!/bin/sh\n", encoding="ascii")

    types = set(plugin_root_exec_types(root))

    assert "canvas" in types
    assert "lsp" in types
    assert "hooks" in types
    assert plugin_root_exec_types(tmp_path / "empty") == ()
