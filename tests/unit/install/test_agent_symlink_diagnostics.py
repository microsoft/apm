"""Real-filesystem regressions for diagnostic-only agent symlink rejection."""

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.install.deployable_source_plan import DeployableSourcePlan
from apm_cli.install.services import (
    IntegratorBundle,
    integrate_local_content,
    integrate_package_primitives,
)
from apm_cli.integration.agent_integrator import AgentIntegrator
from apm_cli.integration.command_integrator import CommandIntegrator
from apm_cli.integration.hook_integrator import HookIntegrator
from apm_cli.integration.instruction_integrator import InstructionIntegrator
from apm_cli.integration.prompt_integrator import PromptIntegrator
from apm_cli.integration.skill_integrator import SkillIntegrator
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.models.apm_package import APMPackage, PackageInfo, PackageType
from apm_cli.utils.diagnostics import DiagnosticCollector

pytestmark = pytest.mark.component


@pytest.mark.parametrize("verbose", [False, True])
def test_own_project_cli_reports_symlink_skip_on_install_and_reinstall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verbose: bool
) -> None:
    """The default and verbose CLI expose the cause without ledger expansion."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "apm.yml").write_text(
        "name: probe\nversion: 0.0.0\ndependencies:\n  apm: []\n  mcp: []\n",
        encoding="utf-8",
    )
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".apm").mkdir()
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "repro.agent.md").write_text(
        "---\nname: repro\ndescription: A repro agent.\n---\n\nBody.\n",
        encoding="utf-8",
    )
    (tmp_path / ".apm" / "agents").symlink_to("../agents", target_is_directory=True)
    args = ["install", "--target", "claude", *(["--verbose"] if verbose else [])]
    runner = CliRunner()
    for _ in range(2):
        result = runner.invoke(cli, args)
        assert result.exit_code == 0, result.output
        assert result.output.count("Skipped symlinked agent source: .apm/agents") == 1
        assert "real files" in result.output
        assert not (tmp_path / ".claude" / "agents").exists()
        assert not (tmp_path / "apm.lock.yaml").exists()


@pytest.mark.parametrize("own_project", [True, False], ids=["own-project", "git-package"])
@pytest.mark.parametrize("linked", [True, False], ids=["symlink", "real-directory"])
def test_agent_source_skip_is_actionable_without_deployment_expansion(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    own_project: bool,
    linked: bool,
) -> None:
    """Both install routes warn once, retain empty ledgers, and deploy real sources."""
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    (project / ".github").mkdir()
    source = project if own_project else project / "apm_modules" / "owner" / "bundle"
    agents = source / ("agents" if linked else ".apm/agents")
    agents.mkdir(parents=True)
    (agents / "repro-agent.agent.md").write_text(
        "---\nname: repro-agent\ndescription: A repro agent.\n---\n\nBody.\n",
        encoding="utf-8",
    )
    if linked:
        (source / ".apm").mkdir()
        (source / ".apm" / "agents").symlink_to("../agents", target_is_directory=True)
    diagnostics = DiagnosticCollector()
    integrators = IntegratorBundle(
        prompt=PromptIntegrator(),
        agent=AgentIntegrator(),
        command=CommandIntegrator(),
        instruction=InstructionIntegrator(),
        hook=HookIntegrator(),
        skill=SkillIntegrator(),
    )
    targets = [KNOWN_TARGETS["claude"], KNOWN_TARGETS["copilot"]]
    if own_project:
        result = integrate_local_content(
            project,
            targets=targets,
            prompt_integrator=integrators.prompt,
            agent_integrator=integrators.agent,
            command_integrator=integrators.command,
            instruction_integrator=integrators.instruction,
            hook_integrator=integrators.hook,
            skill_integrator=integrators.skill,
            diagnostics=diagnostics,
            force=False,
            managed_files=set(),
        )
    else:
        package = PackageInfo(
            package=APMPackage(name="bundle", version="0.0.0", source="github"),
            install_path=source,
            package_type=PackageType.APM_PACKAGE,
        )
        result = integrate_package_primitives(
            package,
            project,
            targets=targets,
            integrators=integrators,
            force=False,
            managed_files=set(),
            diagnostics=diagnostics,
            package_name="owner/bundle",
        )

    expected = (
        []
        if linked
        else [
            ".claude/agents/repro-agent.md",
            ".github/agents/repro-agent.agent.md",
        ]
    )
    assert result["agents"] == len(expected)
    assert sorted(result["deployed_files"]) == expected
    assert (
        sorted(
            path.relative_to(project).as_posix()
            for target in (".claude", ".github")
            for path in (project / target).rglob("*")
            if path.is_file()
        )
        == expected
    )
    if linked:
        warnings = diagnostics.by_category().get("warning", [])
        assert len(warnings) == 1
        assert warnings[0].package == ("_local" if own_project else "owner/bundle")
        assert "Skipped symlinked agent source: .apm/agents" in warnings[0].message
        assert "Symlinked agent sources are not deployed." in warnings[0].message
        assert "real files" in warnings[0].message
        assert "apm install" in warnings[0].message
        diagnostics.render_summary()
        output = capsys.readouterr().out
        assert "Skipped symlinked agent source: .apm/agents" in output
        assert "real files" in output
    else:
        assert diagnostics.by_category().get("warning", []) == []


@pytest.mark.parametrize(
    ("relative", "directory"),
    [
        ("linked.agent.md", False),
        (".apm/agents/linked.md", False),
        (".apm/agents/nested.md", True),
        (".apm", True),
    ],
)
@pytest.mark.parametrize("destination", ["contained", "external", "dangling"])
def test_plan_reports_rejected_links_without_traversing_them(
    tmp_path: Path, relative: str, directory: bool, destination: str
) -> None:
    """Link rejection retains lexical paths and never authorizes link targets."""
    source = tmp_path / "package"
    source.mkdir()
    target = (source if destination == "contained" else tmp_path) / "destination"
    if destination != "dangling":
        if directory:
            (target / "agents").mkdir(parents=True)
            (target / "agents" / "hidden.agent.md").write_text("Body.\n", encoding="utf-8")
        else:
            target.write_text("Body.\n", encoding="utf-8")
    link = source / relative
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target, target_is_directory=directory)
    (source / "real.agent.md").write_text("Body.\n", encoding="utf-8")
    diagnostics = DiagnosticCollector()
    package = PackageInfo(
        package=APMPackage(name="bundle", version="0.0.0"),
        install_path=source,
        package_type=PackageType.APM_PACKAGE,
    )
    import os

    with patch("apm_cli.install.deployable_source_plan.os.walk", wraps=os.walk) as walk:
        plan = DeployableSourcePlan.create(
            package,
            [KNOWN_TARGETS["claude"], KNOWN_TARGETS["copilot"]],
            skill_subset=None,
            hooks_approved=False,
            canvas_approved=False,
            skip_bin=True,
            diagnostics=diagnostics,
            package_name="owner/bundle",
        )

    assert plan.paths == frozenset({"real.agent.md"})
    assert all(call.args[0] not in (link, target) for call in walk.call_args_list)
    warnings = diagnostics.by_category()["warning"]
    assert len(warnings) == 1
    expected = ".apm/agents" if relative == ".apm" else relative
    assert warnings[0].message.startswith(f"Skipped symlinked agent source: {expected}.")
    assert warnings[0].package == "owner/bundle"


@pytest.mark.parametrize("agents_enabled", [True, False])
@pytest.mark.windows_compat
def test_agent_skip_diagnostics_are_target_gated_and_printable(
    tmp_path: Path, agents_enabled: bool
) -> None:
    """Unselected agents stay quiet; selected source labels cannot inject output."""
    name = "linked-\u00e9.agent.md"
    (tmp_path / name).symlink_to(tmp_path / "missing")
    diagnostics = DiagnosticCollector()
    package = PackageInfo(
        package=APMPackage(name="bundle", version="0.0.0"),
        install_path=tmp_path,
        package_type=PackageType.APM_PACKAGE,
    )
    plan = DeployableSourcePlan.create(
        package,
        [KNOWN_TARGETS["claude"]] if agents_enabled else [],
        skill_subset=None,
        hooks_approved=False,
        canvas_approved=False,
        skip_bin=True,
        diagnostics=diagnostics,
        package_name="bundle\n\u00e9",
    )
    assert plan.paths == frozenset()
    warnings = diagnostics.by_category().get("warning", [])
    assert len(warnings) == int(agents_enabled)
    if agents_enabled:
        assert warnings[0].message.startswith("Skipped symlinked agent source: linked-?.agent.md.")
        assert warnings[0].package == "bundle??"
        assert all(0x20 <= ord(char) <= 0x7E for char in warnings[0].message)
