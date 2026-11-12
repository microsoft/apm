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
        "hooks_approved": True,
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


def test_root_hooks_cannot_bypass_approval(tmp_path: Path) -> None:
    from apm_cli.claude_plugins.admission import ClaudeNativePluginError

    info = _plugin(tmp_path)
    (info.install_path / "hooks").mkdir()
    write_json(info.install_path / "hooks/hooks.json", {"hooks": {}})
    with pytest.raises(ClaudeNativePluginError, match="approve"):
        _plan(info, hooks_approved=False)


@pytest.mark.parametrize("field", ["mcpServers", "lspServers", "dependencies", "experimental"])
def test_unqualified_capabilities_fail_closed(tmp_path: Path, field: str) -> None:
    from apm_cli.claude_plugins.admission import ClaudeNativePluginError

    info = _plugin(tmp_path)
    write_json(
        info.install_path / ".claude-plugin/plugin.json",
        {"name": "owned", "commands": [], field: {"unsafe": True}},
    )
    with pytest.raises(ClaudeNativePluginError, match="capabilit"):
        _plan(info)


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
