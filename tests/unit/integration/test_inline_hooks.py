"""Selected-source and native-scope boundaries for standalone inline hooks."""

from pathlib import Path

import pytest

from apm_cli.hook_contract import HookContractError
from apm_cli.install.deployable_source_plan import DeployableSourcePlan
from apm_cli.install.exec_gate import check_executable_approval
from apm_cli.integration.hook_source_selection import _referenced_hook_source_files
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.models.apm_package import APMPackage, PackageInfo, PackageType
from apm_cli.security.executables import scan_package_executables
from apm_cli.utils.yaml_io import yaml_to_str

pytestmark = pytest.mark.component

_HOOKS = {"PreToolUse": [{"hooks": [{"type": "command", "command": "echo harmless"}]}]}


def _write(path: Path, *, hooks: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata: dict[str, object] = {"description": "A harmless source"}
    if hooks:
        metadata["hooks"] = _HOOKS
    path.write_text(f"---\n{yaml_to_str(metadata)}---\nContent.\n", encoding="utf-8")


def _plan(
    root: Path, *, approved: bool = False, subset: tuple[str, ...] | None = None
) -> DeployableSourcePlan:
    return DeployableSourcePlan.create(
        PackageInfo(
            package=APMPackage(name="fixture", version="1.0.0"),
            install_path=root,
            package_type=PackageType.SKILL_BUNDLE,
        ),
        [KNOWN_TARGETS["claude"], KNOWN_TARGETS["copilot"]],
        skill_subset=subset,
        hooks_approved=approved,
        canvas_approved=False,
        skip_bin=True,
    )


def test_inline_declarations_use_only_selected_skill_sources(tmp_path: Path) -> None:
    _write(tmp_path / "skills/protected/SKILL.md")
    _write(tmp_path / "skills/plain/SKILL.md", hooks=False)
    plan = _plan(tmp_path, subset=("plain",))
    declaration = scan_package_executables(tmp_path, "fixture", "1.0.0", source_plan=plan)
    assert not declaration.has_executables
    assert plan.inline_hook_documents == {}


def test_native_inline_documents_are_never_global_descriptors(tmp_path: Path) -> None:
    source = tmp_path / ".apm/agents/guard.agent.md"
    _write(source)
    plan = _plan(tmp_path, approved=True)
    selected = plan.hook_source_selection
    assert selected.inline_for("claude") == {source: {"hooks": _HOOKS}}
    assert selected.inline_for("copilot") == {}
    assert selected.descriptors_for("claude") == []
    assert selected.descriptors_for("copilot") == []


def test_withheld_root_skill_retains_independent_primitive_members(tmp_path: Path) -> None:
    _write(tmp_path / "SKILL.md")
    _write(tmp_path / "guide.txt", hooks=False)
    _write(tmp_path / "plain.agent.md", hooks=False)
    _write(tmp_path / ".apm/skills/plain/SKILL.md", hooks=False)
    plan = _plan(tmp_path).for_target("claude")
    assert not plan.includes("SKILL.md")
    assert not plan.includes("guide.txt")
    assert plan.includes("plain.agent.md")
    assert plan.includes(".apm/skills/plain/SKILL.md")


def test_native_plugin_agent_fields_are_not_activated(tmp_path: Path) -> None:
    _write(tmp_path / ".apm/agents/guard.agent.md")
    marker = tmp_path / ".claude-plugin/plugin.json"
    marker.parent.mkdir()
    marker.write_text('{"name":"opaque"}\n', encoding="utf-8")
    plan = _plan(tmp_path, approved=True)
    assert plan.inline_hook_documents == {}
    assert plan.hook_source_selection.inline_for("claude") == {}


def test_local_display_name_does_not_grant_first_party_trust(tmp_path: Path) -> None:
    package = PackageInfo(
        package=APMPackage(name="fixture", version="1.0.0"), install_path=tmp_path
    )
    assert check_executable_approval("_local", package, {}) == (False,) * 5
    assert check_executable_approval("_local", package, {}, is_first_party=True) == (True,) * 5


@pytest.mark.parametrize("prefix", ["./", "${PLUGIN_ROOT}/"])
def test_inline_resource_symlink_is_rejected_without_changing_legacy_selection(
    tmp_path: Path, prefix: str
) -> None:
    source = tmp_path / "guard.py"
    source.write_text("raise SystemExit(0)\n")
    link = tmp_path / "linked.py"
    link.symlink_to(source)
    document = {
        "hooks": {
            "PreToolUse": [{"hooks": [{"type": "command", "command": f"python {prefix}linked.py"}]}]
        }
    }
    assert _referenced_hook_source_files(document, tmp_path, tmp_path) == {source}
    with pytest.raises(HookContractError, match="symlink"):
        _referenced_hook_source_files(document, tmp_path, tmp_path, strict=True)


def test_empty_inline_hook_map_has_no_executable_declaration(tmp_path: Path) -> None:
    from apm_cli.integration.command_integrator import CommandIntegrator

    source = tmp_path / "plain.prompt.md"
    source.write_text("---\ndescription: Empty hook control\nhooks: {}\n---\nPlain.\n")
    plan = _plan(tmp_path, approved=True)
    assert plan.inline_hook_documents == {}
    _, post, _, _ = CommandIntegrator()._transform_prompt_to_command(source)
    assert "hooks" not in post.metadata
