"""Whole-plugin Claude admission preserves executable and target boundaries."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from apm_cli.core.scope import InstallScope
from apm_cli.models.apm_package import APMPackage, PackageInfo, PackageType

pytestmark = pytest.mark.component


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def _plugin(tmp_path: Path) -> PackageInfo:
    root = tmp_path / "plugin"
    (root / ".claude-plugin").mkdir(parents=True)
    write_json(root / ".claude-plugin/plugin.json", {"name": "owned", "commands": []})
    return PackageInfo(
        package=APMPackage(name="owned", version="1.0.0"),
        install_path=root,
        package_type=PackageType.MARKETPLACE_PLUGIN,
    )


def _plan(info: PackageInfo, **overrides):
    from apm_cli.claude_plugins.admission import plan_native_claude_plugin

    args = {
        "package_name": "owner/plugin",
        "targets": [SimpleNamespace(name="claude")],
        "scope": InstallScope.USER,
        "skill_subset": None,
        "admit": lambda: None,
        "skip_bin": True,
    }
    args.update(overrides)
    return plan_native_claude_plugin(info, **args)


def test_explicit_empty_commands_keep_native_manifest(tmp_path: Path) -> None:
    info = _plugin(tmp_path)
    plan = _plan(info)
    assert plan.name == "owned"
    assert plan.source_root == info.install_path
    assert plan.dependency_key == "owner/plugin"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"scope": InstallScope.PROJECT},
        {"targets": [SimpleNamespace(name="copilot")]},
    ],
)
def test_non_native_scope_keeps_existing_route(tmp_path: Path, kwargs: dict) -> None:
    assert _plan(_plugin(tmp_path), **kwargs) is None


def test_incidental_manifest_does_not_override_apm_intent(tmp_path: Path) -> None:
    info = _plugin(tmp_path)
    info.package_type = PackageType.APM_PACKAGE
    assert _plan(info) is None


def test_partial_selection_refused(tmp_path: Path) -> None:
    from apm_cli.claude_plugins.admission import ClaudeNativePluginError

    with pytest.raises(ClaudeNativePluginError, match="subset"):
        _plan(_plugin(tmp_path), skill_subset=("only-one",))


def test_reset_to_complete_skill_selection_is_not_a_subset(tmp_path: Path) -> None:
    assert _plan(_plugin(tmp_path), skill_subset=()).name == "owned"


def _trust(allow: dict, deny: dict | None = None):
    from apm_cli.security.executables import build_exec_trust_context

    return build_exec_trust_context(
        policy=None,
        project_data={"executables": {"allow": allow, "deny": deny or {}}},
        include_user=False,
    )


def test_whole_plugin_pin_admits_and_any_change_parks(tmp_path: Path) -> None:
    """req-na-005: one pin over the plugin root; unapproved or changed is parked."""
    from apm_cli.security.trust import admit_native_plugin
    from apm_cli.utils.content_hash import compute_native_plugin_hash

    info = _plugin(tmp_path)
    (info.install_path / "hooks").mkdir()
    write_json(info.install_path / "hooks/hooks.json", {"hooks": {}})
    keys = ("owner/plugin",)

    def plan_with(allow: dict):
        return _plan(
            info, admit=lambda: admit_native_plugin(_trust(allow), keys, info.install_path)
        )

    unapproved = plan_with({})
    assert unapproved.parked is not None and "not approved" in unapproved.parked
    assert "apm approve" in unapproved.parked
    pin = compute_native_plugin_hash(info.install_path)
    assert plan_with({"owner/plugin": {"plugin": pin}}).parked is None
    (info.install_path / "hooks/hooks.json").write_text('{"hooks": {"changed": []}}')
    changed = plan_with({"owner/plugin": {"plugin": pin}})
    assert changed.parked is not None and "content changed since approval" in changed.parked


def test_declared_plugin_dependencies_park_with_one_diagnostic(tmp_path: Path) -> None:
    info = _plugin(tmp_path)
    write_json(
        info.install_path / ".claude-plugin/plugin.json",
        {"name": "owned", "dependencies": ["helper"]},
    )
    plan = _plan(info)
    assert plan.parked is not None
    assert "plugin dependencies" in plan.parked


def test_unqualified_capabilities_fail_closed(tmp_path: Path) -> None:
    from apm_cli.claude_plugins.admission import ClaudeNativePluginError

    info = _plugin(tmp_path)
    write_json(
        info.install_path / ".claude-plugin/plugin.json",
        {"name": "owned", "commands": [], "experimental": {"unsafe": True}},
    )
    with pytest.raises(ClaudeNativePluginError, match="capabilit"):
        _plan(info)


_STDIO = {"mcpServers": {"tracker": {"command": "${CLAUDE_PLUGIN_ROOT}/bin/tracker"}}}
_LSP = {"pyls": {"command": "pyls", "extensionToLanguage": {".py": "python"}}}


def _with_mcp(tmp_path: Path, inline: bool = False) -> PackageInfo:
    info = _plugin(tmp_path)
    if inline:
        write_json(info.install_path / ".claude-plugin/plugin.json", {"name": "owned", **_STDIO})
    else:
        write_json(info.install_path / ".mcp.json", _STDIO)
    return info


def _admit(info: PackageInfo, allow: dict, deny: dict | None = None):
    from apm_cli.security.trust import admit_native_plugin

    return lambda: admit_native_plugin(_trust(allow, deny), ("owner/plugin",), info.install_path)


def test_mcp_file_is_admitted_under_the_whole_plugin_pin(tmp_path: Path) -> None:
    from apm_cli.utils.content_hash import compute_native_plugin_hash

    info = _with_mcp(tmp_path)
    assert "not approved" in _plan(info, admit=_admit(info, {})).parked
    pin = {"owner/plugin": {"plugin": compute_native_plugin_hash(info.install_path)}}
    assert _plan(info, admit=_admit(info, pin)).parked is None


def test_inline_mcp_servers_are_gated_as_mcp(tmp_path: Path) -> None:
    from apm_cli.security.trust import native_plugin_exec_types

    info = _with_mcp(tmp_path, inline=True)
    assert "mcp" in native_plugin_exec_types(info.install_path)
    plan = _plan(info, admit=_admit(info, {}, deny={"owner/plugin": {"mcp": True}}))
    assert plan.parked is not None and "denied" in plan.parked


def test_lsp_file_is_admitted_and_an_lsp_deny_parks(tmp_path: Path) -> None:
    from apm_cli.utils.content_hash import compute_native_plugin_hash

    info = _plugin(tmp_path)
    write_json(info.install_path / ".lsp.json", _LSP)
    pin = {"owner/plugin": {"plugin": compute_native_plugin_hash(info.install_path)}}
    assert _plan(info, admit=_admit(info, pin)).parked is None
    denied = _plan(info, admit=_admit(info, pin, deny={"owner/plugin": {"lsp": True}}))
    assert denied.parked is not None and "denied" in denied.parked


def _policy(enforcement: str = "block", **mcp):
    from apm_cli.policy.schema import ApmPolicy, McpPolicy

    return ApmPolicy(enforcement=enforcement, mcp=McpPolicy(**mcp))


def test_org_mcp_deny_parks_the_whole_plugin(tmp_path: Path) -> None:
    plan = _plan(_with_mcp(tmp_path, inline=True), policy=_policy(deny=("track*",)))
    assert plan.parked is not None
    assert "owner/plugin" in plan.parked and "'tracker'" in plan.parked
    assert "mcp-denylist" in plan.parked


def test_disallowed_mcp_transport_parks_the_whole_plugin(tmp_path: Path) -> None:
    from apm_cli.policy.schema import McpTransportPolicy

    policy = _policy(transport=McpTransportPolicy(allow=("http",)))
    plan = _plan(_with_mcp(tmp_path), policy=policy)
    assert plan.parked is not None and "mcp-transport" in plan.parked
    assert "'stdio'" in plan.parked


def test_warn_enforcement_reports_without_parking(tmp_path: Path) -> None:
    plan = _plan(_with_mcp(tmp_path), policy=_policy("warn", deny=("tracker",)))
    assert plan.parked is None
    assert plan.warning is not None and "'tracker'" in plan.warning


@pytest.mark.parametrize("field", ["mcpServers", "lspServers"])
def test_server_file_outside_the_plugin_is_refused(tmp_path: Path, field: str) -> None:
    from apm_cli.claude_plugins.admission import ClaudeNativePluginError

    info = _plugin(tmp_path)
    write_json(
        info.install_path / ".claude-plugin/plugin.json", {"name": "owned", field: "../x.json"}
    )
    with pytest.raises(ClaudeNativePluginError, match=field):
        _plan(info)


def test_native_claude_servers_are_not_expressed_for_claude() -> None:
    from apm_cli.integration.lsp_integrator import LSPIntegrator
    from apm_cli.models.dependency.lsp import LSPDependency

    native = LSPDependency(
        name="pyls",
        command="pyls",
        extension_to_language={".py": "python"},
        native_runtimes=frozenset({"claude"}),
    )
    assert LSPIntegrator.expressible_server_names([native], "claude") == set()
    assert LSPIntegrator.expressible_server_names([native], "copilot") == {"pyls"}


def test_source_symlink_is_not_native_loadable(tmp_path: Path) -> None:
    from apm_cli.claude_plugins.admission import ClaudeNativePluginError
    from apm_cli.install.deployable_source_plan import DeployableSourcePlan
    from apm_cli.integration.targets import KNOWN_TARGETS

    info = _plugin(tmp_path)
    (info.install_path / "linked").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ClaudeNativePluginError, match="symlink"):
        _plan(info)
    with pytest.raises(ValueError, match="symlink"):
        DeployableSourcePlan.create(
            info,
            [KNOWN_TARGETS["claude"]],
            skill_subset=None,
            hooks_approved=True,
            canvas_approved=False,
            skip_bin=True,
            native_plugin_root=info.install_path.resolve(),
        )


def test_native_plan_scans_nontextual_hook_resource_dependencies(tmp_path: Path) -> None:
    from apm_cli.install.deployable_source_plan import DeployableSourcePlan
    from apm_cli.integration.targets import KNOWN_TARGETS

    info = _plugin(tmp_path)
    (info.install_path / "resources").mkdir()
    (info.install_path / "resources/runtime-owned.json").write_text('{"owner":"alpha"}')
    (info.install_path / "hooks").mkdir()
    write_json(
        info.install_path / "hooks/hooks.json",
        {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "node -e \"require(process.env.CLAUDE_PLUGIN_ROOT + '/hooks/probe.cjs')\"",
                            }
                        ]
                    }
                ]
            },
        },
    )
    (info.install_path / "hooks/probe.cjs").write_text("process.exit(0);\n")
    plan = _plan(info)
    source_plan = DeployableSourcePlan.create(
        info,
        [KNOWN_TARGETS["claude"]],
        skill_subset=None,
        hooks_approved=True,
        canvas_approved=False,
        skip_bin=True,
        native_plugin_root=plan.source_root,
    )
    assert source_plan.includes("hooks/probe.cjs")
    assert source_plan.includes("resources/runtime-owned.json")
    assert source_plan.includes(".claude-plugin/plugin.json")


def test_native_source_selection_omits_only_transport_metadata(tmp_path: Path) -> None:
    from apm_cli.install.cache_pin import MARKER_FILENAME
    from apm_cli.install.deployable_source_plan import DeployableSourcePlan
    from apm_cli.integration.targets import KNOWN_TARGETS

    info = _plugin(tmp_path)
    root = info.install_path
    (root / ".git").mkdir()
    (root / ".git/config").write_text("transport metadata\n")
    (root / MARKER_FILENAME).write_text("generated pin\n")
    (root / "resources").mkdir()
    (root / "resources" / MARKER_FILENAME).write_text("publisher resource\n")
    (root / "SKILL.md").write_text("# Publisher root skill\n")
    source_plan = DeployableSourcePlan.create(
        info,
        [KNOWN_TARGETS["claude"]],
        skill_subset=None,
        hooks_approved=True,
        canvas_approved=False,
        skip_bin=True,
        native_plugin_root=root.resolve(),
    )
    assert not source_plan.includes(".git/config")
    assert not source_plan.includes(MARKER_FILENAME)
    assert source_plan.includes(f"resources/{MARKER_FILENAME}")
