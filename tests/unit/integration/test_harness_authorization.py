"""Native adapter preflight and source authorization use the selected implementation."""

from types import SimpleNamespace

import pytest

import apm_cli.harnesses.registry as registry_module
from apm_cli.harnesses import HarnessAdapter, PrimitiveMapping, TargetCapability, TargetProfile
from apm_cli.install.context import InstallContext
from apm_cli.install.deployable_source_plan import DeployableSourcePlan
from apm_cli.install.errors import DirectDependencyError
from apm_cli.install.template import preflight_skill_materializations
from apm_cli.integration.hook_integrator import HookIntegrator
from apm_cli.integration.hook_source_selection import HookSourceSelection
from apm_cli.integration.skill_integrator import SkillIntegrator
from apm_cli.models.apm_package import APMPackage
from apm_cli.utils.diagnostics import DiagnosticCollector


def _capability() -> TargetCapability:
    return TargetCapability(
        name="x-vendor-native",
        aliases=(),
        description="Synthetic native adapter",
        in_all=False,
        explicit_only=True,
        experimental_flag=None,
        mcp_only=False,
        primitive_profile="x-vendor-native",
        compile_family=None,
        runtimes=(),
        commands=frozenset({"compile", "install", "update"}),
    )


def test_custom_hook_sources_are_authorized_only_for_approved_selected_target(
    tmp_path, monkeypatch
):
    source = tmp_path / "package"
    hooks = source / ".apm" / "hooks"
    hooks.mkdir(parents=True)
    descriptor = hooks / "native.json"
    script = hooks / "run.sh"
    descriptor.write_text('{"hooks": {}}', encoding="utf-8")
    script.write_text("echo harmless\n", encoding="utf-8")
    (hooks / "unrelated.txt").write_text("Not deployed\n", encoding="utf-8")

    class NativeHooks(HookIntegrator):
        @classmethod
        def select_deployable_hook_sources(cls, package_path, target_names, **kwargs):
            return HookSourceSelection(
                {name: frozenset({descriptor}) for name in target_names},
                {name: frozenset({script}) for name in target_names},
            )

    capability = _capability()
    profile = TargetProfile(
        capability=capability,
        root_dir=".native",
        primitives={"hooks": PrimitiveMapping("hooks", ".json", "native_hooks")},
    )
    adapter = HarnessAdapter(
        capability=capability, profile=profile, integrators={"hooks": NativeHooks}
    )
    monkeypatch.setattr(
        registry_module,
        "get_harness_registry",
        lambda: SimpleNamespace(adapters={capability.name: adapter}),
    )
    info = SimpleNamespace(install_path=source)
    arguments = dict(skill_subset=None, canvas_approved=False, skip_bin=True)
    approved = DeployableSourcePlan.create(info, [profile], hooks_approved=True, **arguments)
    assert approved.paths == frozenset({".apm/hooks/native.json", ".apm/hooks/run.sh"})
    assert approved.hook_source_selection.descriptors_for(capability.name) == [descriptor]
    assert (
        DeployableSourcePlan.create(info, [profile], hooks_approved=False, **arguments).paths
        == frozenset()
    )
    assert (
        DeployableSourcePlan.create(info, [], hooks_approved=True, **arguments).paths == frozenset()
    )


def test_batch_preflight_compares_custom_rendered_skill_bytes_before_any_writes(
    tmp_path, monkeypatch
):
    source = tmp_path / ".apm" / "skills" / "review" / "SKILL.md"
    source.parent.mkdir(parents=True)
    original = "---\nname: review\ndescription: Review\n---\nOriginal links.\n"
    source.write_text(original, encoding="utf-8")
    (tmp_path / "apm.yml").write_text("name: consumer\nversion: 1.0.0\n", encoding="utf-8")
    destination = tmp_path / ".native" / "skills" / "review" / "SKILL.md"
    destination.parent.mkdir(parents=True)
    rendered = original.replace("Original", "Native")
    destination.write_text(rendered, encoding="utf-8")

    class NativeSkills(SkillIntegrator):
        def resolve_links(self, content, source, destination, *, preserved_source_root=None):
            return content.replace("Original", "Native"), 1

    capability = _capability()
    profile = TargetProfile(
        capability=capability,
        root_dir=".native",
        primitives={"skills": PrimitiveMapping("skills", "/SKILL.md", "skill_standard")},
    )
    adapter = HarnessAdapter(
        capability=capability, profile=profile, integrators={"skills": NativeSkills}
    )
    monkeypatch.setattr(
        registry_module,
        "get_harness_registry",
        lambda: SimpleNamespace(adapters={capability.name: adapter}),
    )
    ctx = InstallContext(project_root=tmp_path, apm_dir=tmp_path / "apm_modules")
    ctx.apm_package = APMPackage(name="consumer", version="1.0.0", package_path=tmp_path)
    ctx.targets = [profile]
    ctx.integrators = {"skill": SkillIntegrator()}
    ctx.diagnostics = DiagnosticCollector()
    ctx.root_has_local_primitives = True
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    preflight_skill_materializations(ctx, [])
    assert before == {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    destination.write_text("User edit\n", encoding="utf-8")
    with pytest.raises(DirectDependencyError):
        preflight_skill_materializations(ctx, [])
    assert destination.read_text(encoding="utf-8") == "User edit\n"
    assert source.read_text(encoding="utf-8") == original
