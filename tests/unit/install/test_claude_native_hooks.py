"""Real hook migration and routing; registration is not runtime execution."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from apm_cli.claude_plugins.admission import ClaudeNativePluginError
from apm_cli.core.scope import InstallScope
from apm_cli.install.context import InstallContext
from apm_cli.install.deployment_plan import prepare_package_deployment
from apm_cli.install.services import IntegratorBundle, integrate_package_primitives
from apm_cli.integration.agent_integrator import AgentIntegrator
from apm_cli.integration.command_integrator import CommandIntegrator
from apm_cli.integration.hook_integrator import HookIntegrator
from apm_cli.integration.instruction_integrator import InstructionIntegrator
from apm_cli.integration.prompt_integrator import PromptIntegrator
from apm_cli.integration.skill_integrator import SkillIntegrator
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.models.apm_package import APMPackage, PackageInfo, PackageType
from apm_cli.models.validation import validate_apm_package
from apm_cli.utils.diagnostics import DiagnosticCollector

pytestmark = pytest.mark.component

EVENTS = ("SessionStart", "SubagentStart", "UserPromptSubmit")
COMMAND = "node -e \"require(process.env.CLAUDE_PLUGIN_ROOT + '/hooks/probe.cjs')\""


def _fixture(root: Path, *, native_target: str = "claude") -> PackageInfo:
    manifest = root / f".{native_target}-plugin/plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        '{"name":"owned-hooks","version":"1.0.0"}',
        encoding="ascii",
    )
    (root / "hooks").mkdir()
    (root / "hooks/hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    event: [{"hooks": [{"type": "command", "command": COMMAND}]}]
                    for event in EVENTS
                },
            }
        ),
        encoding="ascii",
    )
    (root / "hooks/probe.cjs").write_text(
        "require(process.env.CLAUDE_PLUGIN_ROOT + '/resources/identity.json');\n",
        encoding="ascii",
    )
    (root / "resources").mkdir()
    (root / "resources/identity.json").write_text('{"name":"owned-hooks"}', encoding="ascii")
    validated = validate_apm_package(root)
    assert validated.is_valid, validated.errors
    return PackageInfo(
        package=validated.package,
        install_path=root,
        package_type=validated.package_type,
    )


def _integrate(home: Path, info: PackageInfo, **kwargs):
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    ctx = kwargs.pop("ctx", None) or InstallContext(
        project_root=home, apm_dir=home / ".apm", scope=InstallScope.USER
    )
    result = integrate_package_primitives(
        info,
        home,
        targets=kwargs.pop("targets", [KNOWN_TARGETS["claude"]]),
        integrators=IntegratorBundle(
            prompt=PromptIntegrator(),
            agent=AgentIntegrator(),
            skill=SkillIntegrator(),
            instruction=InstructionIntegrator(),
            command=CommandIntegrator(),
            hook=HookIntegrator(),
        ),
        force=False,
        managed_files=set(),
        diagnostics=DiagnosticCollector(),
        package_name="owner/hooks",
        scope=kwargs.pop("scope", InstallScope.USER),
        ctx=ctx,
        **kwargs,
    )
    return result, ctx


def test_native_hooks_remain_intact_without_loose_settings(tmp_path: Path) -> None:
    info = _fixture(tmp_path / ".apm/apm_modules/owned")
    before = (info.install_path / "hooks/hooks.json").read_bytes()
    with patch("apm_cli.claude_plugins.client.ClaudePluginCLI.run") as native:
        result, ctx = _integrate(tmp_path, info)
    assert result["native_plugin"] is True
    assert result["hooks"] == 0
    assert not (tmp_path / ".claude/settings.json").exists()
    assert not (tmp_path / ".claude/hooks").exists()
    assert (info.install_path / "hooks/hooks.json").read_bytes() == before
    assert ctx.claude_native_plugins["owner/hooks"].source_root == info.install_path.resolve()
    native.assert_not_called()


def test_prepared_native_plan_preserves_unmanaged_loose_skill(tmp_path: Path) -> None:
    info = _fixture(tmp_path / ".apm/apm_modules/owned")
    skill = info.install_path / "skills/review/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: review\ndescription: Owned review\n---\nNative review\n")
    personal = tmp_path / ".claude/skills/review/SKILL.md"
    personal.parent.mkdir(parents=True)
    personal.write_bytes(b"Personal loose review\n")
    plan = prepare_package_deployment(
        info,
        tmp_path,
        targets=[KNOWN_TARGETS["claude"]],
        diagnostics=DiagnosticCollector(),
        package_name="owner/hooks",
        scope=InstallScope.USER,
    )
    assert plan.claude_native is not None
    assert plan.codex_native is None
    assert plan.projection_targets == []
    assert plan.source_plan.includes("skills/review/SKILL.md")
    assert (
        SkillIntegrator().find_unmanaged_conflicts(
            info,
            tmp_path,
            targets=plan.projection_targets,
            source_plan=plan.source_plan,
            managed_files=set(),
            force=False,
            skip_bin=plan.skip_bin,
        )
        == ()
    )
    with patch(
        "apm_cli.install.services.prepare_package_deployment",
        side_effect=AssertionError("Prepared admission must not be recalculated"),
    ):
        result, ctx = _integrate(tmp_path, info, deployment_plan=plan)
    assert result["native_claude_plugin"] is True
    assert result["deployed_files"] == []
    assert ctx.claude_native_plugins["owner/hooks"] == plan.claude_native
    assert personal.read_bytes() == b"Personal loose review\n"
    assert not (tmp_path / ".claude/settings.json").exists()


@pytest.mark.parametrize("native_target", ["claude", "codex"])
def test_native_admission_keeps_selected_format_owner_with_both_targets(
    tmp_path: Path, native_target: str
) -> None:
    """Both enabled targets must not reinterpret the other native package format."""
    info = _fixture(tmp_path / ".apm/apm_modules/owned", native_target=native_target)
    before = {path: path.read_bytes() for path in info.install_path.rglob("*") if path.is_file()}
    with (
        patch("apm_cli.claude_plugins.client.ClaudePluginCLI.run") as claude,
        patch("apm_cli.codex_plugins.client.CodexNativeClient.connect") as codex,
    ):
        result, ctx = _integrate(
            tmp_path, info, targets=[KNOWN_TARGETS["claude"], KNOWN_TARGETS["codex"]]
        )
    assert result["native_plugin"] is True
    assert result["hooks"] == 0
    assert result["deployed_files"] == []
    assert not (tmp_path / ".claude/settings.json").exists()
    assert not (tmp_path / ".codex/hooks.json").exists()
    assert {path: path.read_bytes() for path in before} == before
    queued = ctx.claude_native_plugins if native_target == "claude" else ctx.codex_native_plugins
    other = ctx.codex_native_plugins if native_target == "claude" else ctx.claude_native_plugins
    assert set(queued) == {"owner/hooks"}
    assert other == {}
    claude.assert_not_called()
    codex.assert_not_called()


def test_native_admission_preserves_unowned_loose_hooks(tmp_path: Path) -> None:
    info = _fixture(tmp_path / ".apm/apm_modules/owned")
    config = tmp_path / ".claude/settings.json"
    config.parent.mkdir()
    user_entry = {"hooks": [{"type": "command", "command": "echo USER_OWNED"}]}
    config.write_text(
        json.dumps(
            {
                "hooks": {"SessionStart": [user_entry]},
                "alwaysThinkingEnabled": False,
            }
        )
    )
    result, ctx = _integrate(tmp_path, info)

    assert result["native_plugin"] is True
    assert len(ctx.claude_native_plugins) == 1
    assert json.loads(config.read_text()) == {
        "hooks": {"SessionStart": [user_entry]},
        "alwaysThinkingEnabled": False,
    }


def test_unsafe_migration_never_queues_native_registration(tmp_path: Path) -> None:
    info = _fixture(tmp_path / ".apm/apm_modules/owned")
    config = tmp_path / ".claude/settings.json"
    config.parent.mkdir()
    config.write_text("not-json", encoding="ascii")
    with pytest.raises(ClaudeNativePluginError, match="Hook settings were preserved"):
        _integrate(tmp_path, info)
    assert config.read_text() == "not-json"


def test_hook_denial_does_not_register_a_commands_only_subset(tmp_path: Path) -> None:
    info = _fixture(tmp_path / ".apm/apm_modules/owned")
    with pytest.raises(ClaudeNativePluginError, match="not approved"):
        _integrate(tmp_path, info, allow_executables={})
    assert not (tmp_path / ".claude/settings.json").exists()


def test_standalone_inline_hook_keeps_loose_behavior(tmp_path: Path) -> None:
    root = tmp_path / "standalone"
    (root / ".apm/hooks").mkdir(parents=True)
    (root / ".apm/hooks/inline.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [{"hooks": [{"type": "command", "command": "echo INLINE"}]}]
                },
            }
        )
    )
    info = PackageInfo(
        package=APMPackage(name="standalone", version="1.0.0"),
        install_path=root,
        package_type=PackageType.APM_PACKAGE,
    )
    result, ctx = _integrate(tmp_path, info)
    assert result["native_plugin"] is False
    assert not ctx.claude_native_plugins
    hooks = json.loads((tmp_path / ".claude/settings.json").read_text())["hooks"]
    assert hooks["SessionStart"][0]["hooks"][0]["command"] == "echo INLINE"


def test_native_replay_stays_nonmutating(tmp_path: Path) -> None:
    live = tmp_path / "live"
    info = _fixture(live / ".apm/apm_modules/owned")
    (live / ".claude").mkdir()
    config = live / ".claude/settings.json"
    config.write_text('{"alwaysThinkingEnabled":false}', encoding="ascii")
    scratch = tmp_path / "scratch"
    with patch("apm_cli.claude_plugins.client.ClaudePluginCLI.run") as native:
        result, _ = _integrate(
            scratch,
            info,
            scratch_root=scratch,
            native_claude_user_scope=True,
            scope=None,
        )
    assert result["native_plugin"] is True
    assert config.read_text() == '{"alwaysThinkingEnabled":false}'
    assert not (scratch / ".claude/settings.json").exists()
    native.assert_not_called()


@pytest.mark.parametrize(
    ("retained", "refused"),
    [
        ({".claude/commands/review.md": "h1"}, True),
        ({".claude/skills/review/SKILL.md": None}, True),
        ({".codex/skills/review/SKILL.md": "h1", ".agents/skills/review/SKILL.md": "h2"}, False),
        ({}, False),
    ],
)
def test_retained_loose_claude_files_block_native_registration(
    retained: dict[str, str | None], refused: bool
) -> None:
    """Edited package-owned loose Claude files must not load beside the intact plugin."""
    from types import SimpleNamespace

    from apm_cli.claude_plugins.admission import ClaudeNativePluginError
    from apm_cli.install.phases.claude_plugins import _refuse_retained_loose_claude_files
    from apm_cli.integration.targets import KNOWN_TARGETS

    claude = KNOWN_TARGETS["claude"].for_scope(user_scope=True)
    ctx = SimpleNamespace(
        targets=[claude, KNOWN_TARGETS["codex"].for_scope(user_scope=True)],
        claude_native_plugins={"owner/hooks": object()},
        package_cleanup_retained={"owner/hooks": retained, "owner/other": {".claude/x.md": None}},
    )
    if refused:
        with pytest.raises(ClaudeNativePluginError, match="loose Claude files") as error:
            _refuse_retained_loose_claude_files(ctx)
        assert next(iter(retained)) in str(error.value)
    else:
        _refuse_retained_loose_claude_files(ctx)
