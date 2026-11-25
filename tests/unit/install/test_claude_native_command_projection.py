"""Real integration contracts for native commands; no Claude runtime is simulated."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from apm_cli.claude_plugins.admission import ClaudeNativePluginError
from apm_cli.core.scope import InstallScope
from apm_cli.install.context import InstallContext
from apm_cli.install.services import IntegratorBundle, integrate_package_primitives
from apm_cli.integration.agent_integrator import AgentIntegrator
from apm_cli.integration.command_integrator import CommandIntegrator
from apm_cli.integration.hook_integrator import HookIntegrator
from apm_cli.integration.instruction_integrator import InstructionIntegrator
from apm_cli.integration.prompt_integrator import PromptIntegrator
from apm_cli.integration.skill_integrator import SkillIntegrator
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.models.apm_package import APMPackage, PackageInfo, PackageType
from apm_cli.models.dependency.reference import DependencyReference
from apm_cli.models.validation import validate_apm_package
from apm_cli.utils.diagnostics import DiagnosticCollector

pytestmark = pytest.mark.component

_FIXTURE = Path(__file__).parents[2] / "fixtures" / "native_claude_commands"


def _plugin(
    root: Path,
    *,
    commands: list[str] | str | None = None,
    name: str = "apm-command-context",
    virtual: bool = False,
) -> PackageInfo:
    """Normalize an owned native plugin through the production validator."""
    shutil.copytree(_FIXTURE, root)
    manifest_path = root / ".claude-plugin" / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["name"] = name
    if commands is not None:
        manifest["commands"] = commands
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    result = validate_apm_package(root)
    assert result.is_valid, result.errors
    assert result.package is not None
    assert result.package_type is PackageType.MARKETPLACE_PLUGIN
    return PackageInfo(
        package=result.package,
        install_path=root,
        package_type=PackageType.SKILL_BUNDLE if virtual else result.package_type,
        dependency_ref=DependencyReference(
            repo_url="fixture/native-commands",
            virtual_path="plugins/context",
            is_virtual=True,
        )
        if virtual
        else None,
    )


def _integrate(
    home: Path,
    package: PackageInfo,
    *,
    scope: InstallScope = InstallScope.USER,
    targets: tuple[str, ...] = ("claude",),
    subset: list[str] | None = None,
    context: InstallContext | None = None,
) -> tuple[dict, InstallContext]:
    """Use the real admission, security scan, dispatch and file integrators."""
    home.mkdir(parents=True, exist_ok=True)
    for target in targets:
        (home / KNOWN_TARGETS[target].root_dir).mkdir(parents=True, exist_ok=True)
    if context is None:
        context = InstallContext(
            project_root=home,
            apm_dir=home / ".apm",
            apm_modules_dir=home / ".apm" / "apm_modules",
            scope=scope,
        )
    result = integrate_package_primitives(
        package,
        home,
        targets=[KNOWN_TARGETS[target] for target in targets],
        integrators=IntegratorBundle(
            prompt=PromptIntegrator(),
            agent=AgentIntegrator(),
            skill=SkillIntegrator(),
            instruction=InstructionIntegrator(),
            command=CommandIntegrator(),
            hook=HookIntegrator(),
        ),
        package_name=package.package.name,
        force=False,
        managed_files=set(),
        scope=scope,
        ctx=context,
        diagnostics=DiagnosticCollector(),
        dep_target_subset=subset,
    )
    return result, context


@pytest.mark.parametrize("virtual", [False, True], ids=["plugin-root", "git-path-plugin"])
def test_selected_native_commands_do_not_become_loose_commands(
    tmp_path: Path, virtual: bool
) -> None:
    """Both root and Git-path plugins keep their original command/resource unit."""
    home = tmp_path / "home"
    source = home / ".apm" / "apm_modules" / "fixture" / "commands"
    package = _plugin(source, virtual=virtual)
    originals = {
        path.relative_to(source): path.read_bytes()
        for path in source.rglob("*")
        if path.is_file() and ".apm" not in path.relative_to(source).parts
    }

    result, context = _integrate(home, package)

    assert not (home / ".claude" / "commands" / "status.md").exists()
    assert not (home / ".claude" / "commands" / "unselected.md").exists()
    assert result["commands"] == 0
    assert result["native_plugin"] is True
    assert len(context.claude_native_plugins) == 1
    native = next(iter(context.claude_native_plugins.values()))
    assert native.name == "apm-command-context"
    assert native.source_root == source.resolve()
    for relative, content in originals.items():
        assert (native.source_root / relative).read_bytes() == content
    assert b"disable-model-invocation: true" in originals[Path("selected/status.md")]
    assert b"${CLAUDE_PLUGIN_ROOT}/scripts/context.mjs" in originals[Path("selected/status.md")]
    assert not (home / ".claude" / "hooks").exists()


def test_native_route_preserves_other_targets_command_projection(tmp_path: Path) -> None:
    """Native Claude admission does not erase the explicit Cursor projection."""
    home = tmp_path / "home"
    package = _plugin(home / ".apm" / "apm_modules" / "fixture")

    result, _ = _integrate(home, package, targets=("claude", "cursor"))

    assert result["native_plugin"] is True
    assert result["commands"] == 1
    assert not (home / ".claude" / "commands" / "status.md").exists()
    assert (home / ".cursor" / "commands" / "status.md").is_file()
    assert not (home / ".cursor" / "commands" / "unselected.md").exists()


@pytest.mark.parametrize(
    "commands",
    [[], ["./selected/status.md"], "./selected/status.md"],
    ids=["empty", "selected-list", "selected-string"],
)
def test_native_manifest_selection_and_resources_remain_unchanged(
    tmp_path: Path, commands: list[str] | str
) -> None:
    """An explicit empty selection never falls back to commands/ discovery."""
    home = tmp_path / "home"
    source = home / ".apm" / "apm_modules" / "fixture"
    package = _plugin(source, commands=commands)
    manifest = source / ".claude-plugin" / "plugin.json"
    before = manifest.read_bytes()

    result, context = _integrate(home, package)

    assert result["commands"] == 0
    assert result["native_plugin"] is True
    assert len(context.claude_native_plugins) == 1
    assert not list((home / ".claude" / "commands").glob("*.md"))
    assert manifest.read_bytes() == before
    assert json.loads(before)["commands"] == commands
    assert (source / "scripts" / "context.mjs").is_file()
    assert (source / "resources" / "identity.txt").read_text().strip() == (
        "APM_NATIVE_COMMAND_CONTEXT"
    )


def test_dependency_target_exclusion_does_not_activate_native_claude(tmp_path: Path) -> None:
    """Per-dependency target narrowing remains authoritative before admission."""
    home = tmp_path / "home"
    package = _plugin(home / ".apm" / "apm_modules" / "fixture")

    result, _ = _integrate(home, package, targets=("claude", "cursor"), subset=["cursor"])

    assert result["native_plugin"] is False
    assert result["commands"] == 1
    assert (home / ".cursor" / "commands" / "status.md").is_file()
    assert not (home / ".claude" / "commands" / "status.md").exists()


@pytest.mark.parametrize(
    "commands", [[], ["./selected/status.md"]], ids=["empty-selection", "selected-command"]
)
def test_unreadable_retained_hooks_preserve_command_context(
    tmp_path: Path, commands: list[str]
) -> None:
    """Failed native admission cannot rewrite command resources or user commands."""
    home = tmp_path / "home"
    source = home / ".apm" / "apm_modules" / "fixture"
    package = _plugin(source, commands=commands)
    originals = {
        path.relative_to(source): path.read_bytes() for path in source.rglob("*") if path.is_file()
    }
    user_command = home / ".claude" / "commands" / "status.md"
    user_command.parent.mkdir(parents=True)
    user_command.write_bytes(b"# User-owned status\n")
    settings = home / ".claude" / "settings.json"
    settings.write_bytes(b"{not valid JSON}\n")
    context = InstallContext(project_root=home, apm_dir=home / ".apm", scope=InstallScope.USER)

    with pytest.raises(ClaudeNativePluginError, match="Hook settings were preserved") as error:
        _integrate(home, package, context=context)

    assert not context.claude_native_plugins
    assert settings.read_bytes() == b"{not valid JSON}\n"
    assert "settings.json" in str(error.value)
    assert user_command.read_bytes() == b"# User-owned status\n"
    assert sorted(path.name for path in user_command.parent.iterdir()) == ["status.md"]
    assert {
        path.relative_to(source): path.read_bytes() for path in source.rglob("*") if path.is_file()
    } == originals


def test_omitted_command_selection_retains_native_default_directory(tmp_path: Path) -> None:
    """The reported manifest omits commands and relies on native commands/ discovery."""
    home = tmp_path / "home"
    source = home / ".apm" / "apm_modules" / "implicit-commands"
    shutil.copytree(_FIXTURE, source)
    manifest_path = source / ".claude-plugin" / "plugin.json"
    manifest = json.loads(manifest_path.read_text())
    del manifest["commands"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    validation = validate_apm_package(source)
    assert validation.is_valid, validation.errors
    assert validation.package is not None
    package = PackageInfo(
        package=validation.package,
        install_path=source,
        package_type=validation.package_type,
    )

    result, context = _integrate(home, package)

    assert result["native_plugin"] is True
    assert result["commands"] == 0
    assert not list((home / ".claude" / "commands").glob("*.md"))
    native = next(iter(context.claude_native_plugins.values()))
    assert "commands" not in json.loads(
        (native.source_root / ".claude-plugin" / "plugin.json").read_text()
    )
    assert (native.source_root / "commands" / "unselected.md").is_file()


@pytest.mark.parametrize("scope", [InstallScope.PROJECT, InstallScope.USER])
def test_standalone_prompt_commands_keep_existing_behavior(
    tmp_path: Path, scope: InstallScope
) -> None:
    """Ordinary APM commands remain loose and retain input placeholder conversion."""
    home = tmp_path / "home"
    source = home / ".apm" / "apm_modules" / "standalone"
    prompts = source / ".apm" / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "status.prompt.md").write_text(
        "---\ndescription: Standalone command\ninput: [topic]\n---\nExplain ${{input:topic}}.\n",
        encoding="utf-8",
    )
    package = PackageInfo(
        package=APMPackage(name="standalone", version="1.0.0"),
        install_path=source,
        package_type=PackageType.APM_PACKAGE,
    )

    result, _ = _integrate(home, package, scope=scope)

    assert result["native_plugin"] is False
    assert result["commands"] == 1
    output = (home / ".claude" / "commands" / "status.md").read_text()
    assert "Explain $topic." in output
    assert "${{input:topic}}" not in output


def test_project_plugin_projection_is_not_silently_migrated(tmp_path: Path) -> None:
    """The bounded user-scope native route does not change project deployment."""
    home = tmp_path / "project"
    package = _plugin(home / "apm_modules" / "fixture")

    result, _ = _integrate(home, package, scope=InstallScope.PROJECT)

    assert result["native_plugin"] is False
    assert result["commands"] == 1
    assert (home / ".claude" / "commands" / "status.md").is_file()


def test_native_namespaces_preserve_distinct_roots_and_user_command(tmp_path: Path) -> None:
    """Equal command basenames in two plugins never compete with a user's command."""
    home = tmp_path / "home"
    user_command = home / ".claude" / "commands" / "status.md"
    user_command.parent.mkdir(parents=True)
    user_command.write_bytes(b"# User-owned status\n")
    plans = []
    for name in ("context-first", "context-second"):
        source = home / ".apm" / "apm_modules" / name
        package = _plugin(source, name=name)
        resource = source / "resources" / "identity.txt"
        resource.write_text(name, encoding="utf-8")

        result, context = _integrate(home, package)

        assert result["native_plugin"] is True
        plans.extend(context.claude_native_plugins.values())

    assert {plan.name for plan in plans} == {"context-first", "context-second"}
    assert len({plan.source_root for plan in plans}) == 2
    for plan in plans:
        assert (plan.source_root / "resources" / "identity.txt").read_text() == plan.name
        assert (plan.source_root / "selected" / "status.md").read_bytes() == (
            _FIXTURE / "selected" / "status.md"
        ).read_bytes()
    assert user_command.read_bytes() == b"# User-owned status\n"
    assert sorted(path.name for path in user_command.parent.iterdir()) == ["status.md"]


def test_native_resources_must_pass_shared_security_scan(tmp_path: Path) -> None:
    """A resource outside normalized prompts cannot evade the pre-deploy gate."""
    home = tmp_path / "home"
    source = home / ".apm" / "apm_modules" / "fixture"
    package = _plugin(source)
    (source / "resources" / "unsafe.md").write_text(
        "hidden \u202e command payload", encoding="utf-8"
    )

    result, context = _integrate(home, package)

    assert result["native_plugin"] is False
    assert not list((home / ".claude" / "commands").glob("*.md"))
    assert not context.claude_native_plugins


def test_apm_intent_is_not_overridden_by_incidental_native_manifest(tmp_path: Path) -> None:
    """An eligible APM package keeps its own prompt, not a co-located native command."""
    home = tmp_path / "home"
    source = home / ".apm" / "apm_modules" / "standalone-with-plugin"
    shutil.copytree(_FIXTURE, source)
    prompts = source / ".apm" / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "ordinary.prompt.md").write_text(
        "---\ndescription: Explicit APM command\n---\nAPM_LAYOUT_WINS\n",
        encoding="utf-8",
    )
    (source / "apm.yml").write_text(
        "name: standalone-with-plugin\nversion: 1.0.0\ntargets: [claude]\n",
        encoding="utf-8",
    )
    validation = validate_apm_package(source)
    assert validation.is_valid, validation.errors
    assert validation.package is not None
    assert validation.package_type is PackageType.APM_PACKAGE
    package = PackageInfo(
        package=validation.package,
        install_path=source,
        package_type=validation.package_type,
    )

    result, _ = _integrate(home, package)

    assert result["native_plugin"] is False
    assert result["commands"] == 1
    assert "APM_LAYOUT_WINS" in (home / ".claude" / "commands" / "ordinary.md").read_text()
    assert not (home / ".claude" / "commands" / "status.md").exists()
