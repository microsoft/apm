from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.install import services
from apm_cli.install.services import IntegratorBundle
from apm_cli.integration.base_integrator import IntegrationResult


def make_package_info(install_path: Path) -> MagicMock:
    pkg_info = MagicMock()
    pkg_info.install_path = str(install_path)
    pkg_info.package = MagicMock()
    pkg_info.package.name = "test-pkg"
    return pkg_info


def make_target(
    name: str = "claude",
    *,
    root_dir: str = ".claude",
    primitives: dict[str, object] | None = None,
    resolved_deploy_root: Path | None = None,
    hooks_config_display: str | None = None,
) -> MagicMock:
    target = MagicMock()
    target.name = name
    target.root_dir = root_dir
    target.primitives = primitives or {}
    target.auto_create = True
    target.hooks_config_display = hooks_config_display
    target.resolved_deploy_root = resolved_deploy_root
    if resolved_deploy_root is not None:
        target.managed_deploy_root = resolved_deploy_root
    else:
        _root = Path(root_dir)
        target.managed_deploy_root = _root if _root.is_absolute() else None
    return target


def make_mapping(
    *,
    deploy_root: str | None = None,
    subdir: str = "",
    format_id: str = "plain",
    output_compare: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        deploy_root=deploy_root,
        subdir=subdir,
        format_id=format_id,
        output_compare=output_compare,
    )


def make_dispatch_entry(
    *,
    multi_target: bool = False,
    integrate_method: str = "integrate_prompts_for_target",
    counter_key: str = "prompts",
) -> MagicMock:
    entry = MagicMock()
    entry.multi_target = multi_target
    entry.integrate_method = integrate_method
    entry.counter_key = counter_key
    return entry


def make_integration_result(
    *,
    files_integrated: int = 1,
    target_paths: list[Path] | None = None,
    links_resolved: int = 0,
    files_adopted: int = 0,
) -> IntegrationResult:
    return IntegrationResult(
        files_integrated=files_integrated,
        files_updated=0,
        files_skipped=0,
        target_paths=target_paths or [],
        links_resolved=links_resolved,
        files_adopted=files_adopted,
    )


def make_skill_result(
    *,
    target_paths: list[Path] | None = None,
    skill_created: bool = False,
    sub_skills_promoted: int = 0,
    bin_deployed: int = 0,
    bin_skipped_reason: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        target_paths=target_paths or [],
        skill_created=skill_created,
        sub_skills_promoted=sub_skills_promoted,
        bin_deployed=bin_deployed,
        bin_skipped_reason=bin_skipped_reason,
    )


def make_ctx(*, verbose: bool = False) -> MagicMock:
    ctx = MagicMock()
    ctx.verbose = verbose
    ctx.cowork_nonsupported_warned = False
    return ctx


def tree_messages(logger: MagicMock) -> list[str]:
    return [call.args[0] for call in logger.tree_item.call_args_list]


def invoke_integrate(
    tmp_path: Path,
    *,
    targets: list[MagicMock] | None = None,
    dispatch_table: dict[str, MagicMock] | None = None,
    integrator_results: dict[str, IntegrationResult] | None = None,
    skill_result: object | None = None,
    logger: MagicMock | None = None,
    ctx: MagicMock | None = None,
    package_dir: Path | None = None,
    package_name: str = "test-pkg",
    scratch_root: Path | None = None,
    skill_subset: tuple | None = None,
    project_root: Path | None = None,
) -> tuple[dict, dict[str, MagicMock], MagicMock, MagicMock]:
    package_dir = package_dir or (tmp_path / "pkg")
    package_dir.mkdir(parents=True, exist_ok=True)
    pkg_info = make_package_info(package_dir)
    diagnostics = MagicMock()
    logger = logger or MagicMock()
    integrators = {
        "prompt_integrator": MagicMock(),
        "agent_integrator": MagicMock(),
        "skill_integrator": MagicMock(),
        "instruction_integrator": MagicMock(),
        "command_integrator": MagicMock(),
        "hook_integrator": MagicMock(),
    }
    results = integrator_results or {}
    default_skill_result = make_skill_result()
    integrators["skill_integrator"].integrate_package_skill.return_value = (
        skill_result or default_skill_result
    )

    method_map = {
        "prompts": ("prompt_integrator", "integrate_prompts_for_target"),
        "agents": ("agent_integrator", "integrate_agents_for_target"),
        "instructions": (
            "instruction_integrator",
            "integrate_instructions_for_target",
        ),
        "commands": ("command_integrator", "integrate_commands_for_target"),
        "hooks": ("hook_integrator", "integrate_hooks_for_target"),
    }
    for primitive, (integrator_key, method_name) in method_map.items():
        value = results.get(primitive, make_integration_result(files_integrated=0))
        getattr(integrators[integrator_key], method_name).return_value = value

    with patch(
        "apm_cli.integration.dispatch.get_dispatch_table", return_value=dispatch_table or {}
    ):
        result = services.integrate_package_primitives(
            pkg_info,
            project_root or tmp_path,
            targets=targets or [],
            force=False,
            managed_files=set(),
            diagnostics=diagnostics,
            package_name=package_name,
            logger=logger,
            scope=None,
            skill_subset=skill_subset,
            ctx=ctx,
            scratch_root=scratch_root,
            integrators=IntegratorBundle(
                prompt=integrators["prompt_integrator"],
                agent=integrators["agent_integrator"],
                skill=integrators["skill_integrator"],
                instruction=integrators["instruction_integrator"],
                command=integrators["command_integrator"],
                hook=integrators["hook_integrator"],
            ),
        )
    return result, integrators, diagnostics, logger


class TestDeployedPathEntry:
    def test_targets_none_falls_back_to_project_relative(self, tmp_path: Path) -> None:
        project_root = tmp_path / "project"
        target_path = project_root / ".claude" / "skills" / "demo" / "SKILL.md"
        target_path.parent.mkdir(parents=True)
        target_path.write_text("demo")

        assert services._deployed_path_entry(target_path, project_root, None) == (
            ".claude/skills/demo/SKILL.md"
        )

    def test_empty_targets_falls_back_to_project_relative(self, tmp_path: Path) -> None:
        project_root = tmp_path / "project"
        target_path = project_root / ".cursor" / "rules" / "guide.mdc"
        target_path.parent.mkdir(parents=True)
        target_path.write_text("guide")

        assert services._deployed_path_entry(target_path, project_root, []) == (
            ".cursor/rules/guide.mdc"
        )

    def test_outside_project_with_no_targets_raises(self, tmp_path: Path) -> None:
        project_root = tmp_path / "project"
        project_root.mkdir()
        outside = tmp_path / "outside" / "file.txt"
        outside.parent.mkdir()
        outside.write_text("x")

        with pytest.raises(RuntimeError, match="Cannot translate"):
            services._deployed_path_entry(outside, project_root, None)

    def test_dynamic_target_uses_cowork_lockfile_path(self, tmp_path: Path) -> None:
        project_root = tmp_path / "project"
        project_root.mkdir()
        deploy_root = tmp_path / "cowork"
        target_path = deploy_root / "skills" / "demo" / "SKILL.md"
        target = make_target(
            name="copilot-cowork",
            root_dir=".github",
            resolved_deploy_root=deploy_root,
        )

        with patch(
            "apm_cli.integration.copilot_cowork_paths.to_lockfile_path",
            return_value="cowork://skills/demo/SKILL.md",
        ) as mock_lockfile_path:
            result = services._deployed_path_entry(target_path, project_root, [target])

        assert result == "cowork://skills/demo/SKILL.md"
        mock_lockfile_path.assert_called_once_with(target_path, deploy_root)

    def test_copilot_app_target_uses_lockfile_uri(self, tmp_path: Path) -> None:
        project_root = tmp_path / "project"
        project_root.mkdir()
        deploy_root = tmp_path / "copilot-app"
        target_path = deploy_root / "workflows" / "wf-123"
        target = make_target(
            name="copilot-app",
            root_dir=".github",
            resolved_deploy_root=deploy_root,
        )

        with patch(
            "apm_cli.integration.copilot_app_db.to_lockfile_uri",
            return_value="copilot-app-db://workflows/wf-123",
        ) as mock_uri:
            result = services._deployed_path_entry(target_path, project_root, [target])

        assert result == "copilot-app-db://workflows/wf-123"
        mock_uri.assert_called_once_with("wf-123")

    def test_outside_project_with_dynamic_targets_uses_fallback_loop(self, tmp_path: Path) -> None:
        project_root = tmp_path / "project"
        project_root.mkdir()
        unmatched_root = tmp_path / "other"
        matched_root = tmp_path / "cowork"
        target_path = tmp_path / "elsewhere" / "skill.md"
        targets = [
            make_target(name="copilot-cowork", resolved_deploy_root=unmatched_root),
            make_target(name="copilot-cowork", resolved_deploy_root=matched_root),
        ]

        with patch(
            "apm_cli.integration.copilot_cowork_paths.to_lockfile_path",
            return_value="cowork://fallback/skill.md",
        ) as mock_lockfile_path:
            result = services._deployed_path_entry(target_path, project_root, targets)

        assert result == "cowork://fallback/skill.md"
        mock_lockfile_path.assert_called_once_with(target_path, unmatched_root)

    def test_outside_project_with_copilot_app_fallback_uses_uri(self, tmp_path: Path) -> None:
        project_root = tmp_path / "project"
        project_root.mkdir()
        target_path = tmp_path / "outside" / "workflow-7"
        targets = [make_target(name="copilot-app", resolved_deploy_root=tmp_path / "app-root")]

        with patch(
            "apm_cli.integration.copilot_app_db.to_lockfile_uri",
            return_value="copilot-app-db://workflows/workflow-7",
        ) as mock_uri:
            result = services._deployed_path_entry(target_path, project_root, targets)

        assert result == "copilot-app-db://workflows/workflow-7"
        mock_uri.assert_called_once_with("workflow-7")


class TestIntegratePackagePrimitives:
    def test_empty_targets_returns_early(self, tmp_path: Path) -> None:
        result, integrators, _, _ = invoke_integrate(tmp_path, targets=[])

        assert result == {
            "prompts": 0,
            "agents": 0,
            "skills": 0,
            "sub_skills": 0,
            "instructions": 0,
            "commands": 0,
            "hooks": 0,
            "canvases": 0,
            "links_resolved": 0,
            "deployed_files": [],
        }
        integrators["skill_integrator"].integrate_package_skill.assert_not_called()

    def test_scratch_root_validation_uses_ensure_path_within(self, tmp_path: Path) -> None:
        scratch_root = tmp_path / "scratch"
        scratch_root.mkdir()

        with patch("apm_cli.utils.path_security.ensure_path_within") as mock_ensure:
            invoke_integrate(
                tmp_path,
                targets=[make_target(primitives={})],
                scratch_root=scratch_root,
                project_root=scratch_root,
            )

        mock_ensure.assert_called_once_with(scratch_root.resolve(), scratch_root.resolve())

    def test_cowork_warning_emits_once_and_sets_context(self, tmp_path: Path) -> None:
        package_dir = tmp_path / "pkg"
        agents_dir = package_dir / ".apm" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "agent.md").write_text("agent")
        ctx = make_ctx()
        logger = MagicMock()
        diagnostics = MagicMock()
        target = make_target(name="copilot-cowork", primitives={})

        with patch("apm_cli.integration.dispatch.get_dispatch_table", return_value={}):
            services.integrate_package_primitives(
                make_package_info(package_dir),
                tmp_path,
                targets=[target],
                integrators=IntegratorBundle(
                    prompt=MagicMock(),
                    agent=MagicMock(),
                    skill=MagicMock(
                        integrate_package_skill=MagicMock(return_value=make_skill_result())
                    ),
                    instruction=MagicMock(),
                    command=MagicMock(),
                    hook=MagicMock(),
                ),
                force=False,
                managed_files=set(),
                diagnostics=diagnostics,
                package_name="warning-pkg",
                logger=logger,
                ctx=ctx,
            )

        assert ctx.cowork_nonsupported_warned is True
        logger.warning.assert_called_once()
        diagnostics.warn.assert_called_once()
        message = diagnostics.warn.call_args.args[0]
        assert "warning-pkg" in message
        assert "agents" in message

    def test_dispatch_calls_prompt_integrator_and_updates_counter(self, tmp_path: Path) -> None:
        target_path = tmp_path / ".claude" / "prompts" / "demo.md"
        prompt_target = make_target(
            primitives={"prompts": make_mapping(subdir="prompts")},
        )
        entry = make_dispatch_entry(counter_key="prompts")

        with patch(
            "apm_cli.install.services._deployed_path_entry",
            return_value=".claude/prompts/demo.md",
        ) as mock_entry:
            result, integrators, _, _ = invoke_integrate(
                tmp_path,
                targets=[prompt_target],
                dispatch_table={"prompts": entry},
                integrator_results={
                    "prompts": make_integration_result(
                        files_integrated=2,
                        target_paths=[target_path],
                        links_resolved=3,
                    )
                },
            )

        integrators["prompt_integrator"].integrate_prompts_for_target.assert_called_once()
        assert result["prompts"] == 2
        assert result["links_resolved"] == 3
        assert result["deployed_files"] == [".claude/prompts/demo.md"]
        mock_entry.assert_called_once_with(target_path, tmp_path, [prompt_target])

    def test_dispatch_calls_agent_integrator(self, tmp_path: Path) -> None:
        target = make_target(primitives={"agents": make_mapping(subdir="agents")})
        entry = make_dispatch_entry(
            integrate_method="integrate_agents_for_target",
            counter_key="agents",
        )

        result, integrators, _, _ = invoke_integrate(
            tmp_path,
            targets=[target],
            dispatch_table={"agents": entry},
            integrator_results={"agents": make_integration_result(files_integrated=1)},
        )

        integrators["agent_integrator"].integrate_agents_for_target.assert_called_once()
        assert result["agents"] == 1

    def test_instruction_cursor_rules_use_rule_label(self, tmp_path: Path) -> None:
        target = make_target(
            primitives={
                "instructions": make_mapping(
                    subdir="rules", format_id="cursor_rules", output_compare=True
                )
            }
        )
        entry = make_dispatch_entry(
            integrate_method="integrate_instructions_for_target",
            counter_key="instructions",
        )
        logger = MagicMock()

        invoke_integrate(
            tmp_path,
            targets=[target],
            dispatch_table={"instructions": entry},
            integrator_results={"instructions": make_integration_result(files_integrated=1)},
            logger=logger,
        )

        assert tree_messages(logger) == ["  |-- 1 rule(s) integrated -> .claude/rules/"]

    def test_instruction_plain_format_uses_instruction_label(self, tmp_path: Path) -> None:
        target = make_target(
            primitives={"instructions": make_mapping(subdir="docs", format_id="plain")}
        )
        entry = make_dispatch_entry(
            integrate_method="integrate_instructions_for_target",
            counter_key="instructions",
        )
        logger = MagicMock()

        invoke_integrate(
            tmp_path,
            targets=[target],
            dispatch_table={"instructions": entry},
            integrator_results={"instructions": make_integration_result(files_integrated=1)},
            logger=logger,
        )

        assert tree_messages(logger) == ["  |-- 1 instruction(s) integrated -> .claude/docs/"]

    def test_hooks_use_hooks_config_display(self, tmp_path: Path) -> None:
        target = make_target(
            primitives={"hooks": make_mapping(subdir="ignored")},
            hooks_config_display="hooks.yaml",
        )
        entry = make_dispatch_entry(
            integrate_method="integrate_hooks_for_target",
            counter_key="hooks",
        )
        logger = MagicMock()

        invoke_integrate(
            tmp_path,
            targets=[target],
            dispatch_table={"hooks": entry},
            integrator_results={"hooks": make_integration_result(files_integrated=1)},
            logger=logger,
        )

        assert tree_messages(logger) == ["  |-- 1 hook(s) integrated -> hooks.yaml"]

    def test_commands_use_plain_label(self, tmp_path: Path) -> None:
        target = make_target(
            primitives={"commands": make_mapping(deploy_root=".github", subdir="commands")}
        )
        entry = make_dispatch_entry(
            integrate_method="integrate_commands_for_target",
            counter_key="commands",
        )

        result, _, _, logger = invoke_integrate(
            tmp_path,
            targets=[target],
            dispatch_table={"commands": entry},
            integrator_results={"commands": make_integration_result(files_integrated=1)},
            logger=MagicMock(),
        )

        assert result["commands"] == 1
        assert tree_messages(logger) == ["  |-- 1 commands integrated -> .github/commands/"]

    def test_multi_target_dispatch_entries_are_skipped(self, tmp_path: Path) -> None:
        target = make_target(primitives={"prompts": make_mapping(subdir="prompts")})
        entry = make_dispatch_entry(multi_target=True)

        result, integrators, _, logger = invoke_integrate(
            tmp_path,
            targets=[target],
            dispatch_table={"prompts": entry},
            logger=MagicMock(),
        )

        integrators["prompt_integrator"].integrate_prompts_for_target.assert_not_called()
        assert result["prompts"] == 0
        assert tree_messages(logger) == ["  |-- (files unchanged)"]

    def test_missing_mapping_is_skipped(self, tmp_path: Path) -> None:
        target = make_target(primitives={})
        entry = make_dispatch_entry(counter_key="prompts")

        result, integrators, _, _ = invoke_integrate(
            tmp_path,
            targets=[target],
            dispatch_table={"prompts": entry},
        )

        integrators["prompt_integrator"].integrate_prompts_for_target.assert_not_called()
        assert result["prompts"] == 0

    def test_adopted_only_logs_adopted_without_incrementing_counter(self, tmp_path: Path) -> None:
        target = make_target(primitives={"prompts": make_mapping(subdir="prompts")})
        entry = make_dispatch_entry(counter_key="prompts")

        result, _, _, logger = invoke_integrate(
            tmp_path,
            targets=[target],
            dispatch_table={"prompts": entry},
            integrator_results={
                "prompts": make_integration_result(files_integrated=0, files_adopted=2)
            },
            logger=MagicMock(),
        )

        assert result["prompts"] == 0
        assert tree_messages(logger) == [
            "  |-- 2 prompts adopted -> .claude/prompts/",
            "  |-- (files unchanged)",
        ]

    def test_non_int_adopted_value_is_treated_as_zero(self, tmp_path: Path) -> None:
        target = make_target(primitives={"prompts": make_mapping(subdir="prompts")})
        entry = make_dispatch_entry(counter_key="prompts")
        odd_result = MagicMock()
        odd_result.files_integrated = 0
        odd_result.links_resolved = 0
        odd_result.target_paths = []
        odd_result.files_adopted = MagicMock()

        result, _, _, logger = invoke_integrate(
            tmp_path,
            targets=[target],
            dispatch_table={"prompts": entry},
            integrator_results={"prompts": odd_result},
            logger=MagicMock(),
        )

        assert result["prompts"] == 0
        assert tree_messages(logger) == ["  |-- (files unchanged)"]

    def test_deployed_files_include_skill_paths(self, tmp_path: Path) -> None:
        skill_path = tmp_path / ".claude" / "skills" / "demo" / "SKILL.md"

        with patch(
            "apm_cli.install.services._deployed_path_entry",
            side_effect=[".claude/skills/demo/SKILL.md"],
        ):
            result, _, _, _ = invoke_integrate(
                tmp_path,
                targets=[make_target(primitives={})],
                skill_result=make_skill_result(
                    target_paths=[skill_path],
                    skill_created=True,
                ),
            )

        assert result["deployed_files"] == [".claude/skills/demo/SKILL.md"]

    def test_deployed_files_expand_skill_dir_to_contained_files(self, tmp_path: Path) -> None:
        # #1716 regression trap: a deployed skill DIRECTORY must also record
        # its contained files so per-file content hashes
        # (compute_deployed_hashes -> content-integrity) cover
        # SKILL.md / assets / scripts. Without the expansion, skills are
        # dir-only entries and skill content drift escapes the documented
        # ``apm audit --ci --no-drift`` gate.
        skill_dir = tmp_path / ".agents" / "skills" / "demo"
        (skill_dir / "assets").mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# demo skill\n")
        (skill_dir / "assets" / "schema.json").write_text("{}\n")

        result, _, _, _ = invoke_integrate(
            tmp_path,
            targets=[make_target(primitives={})],
            skill_result=make_skill_result(
                target_paths=[skill_dir],
                skill_created=True,
            ),
        )

        df = result["deployed_files"]
        # Directory entry retained (cleanup's directory-rejection gate +
        # manifest dir-exclusion contract depend on it).
        assert ".agents/skills/demo" in df
        # Per-file entries added -> content-integrity coverage.
        assert ".agents/skills/demo/SKILL.md" in df
        assert ".agents/skills/demo/assets/schema.json" in df

    def test_two_target_paths_are_collapsed_on_one_line(self, tmp_path: Path) -> None:
        targets = [
            make_target(name="claude", root_dir=".claude", primitives={"prompts": make_mapping()}),
            make_target(name="cursor", root_dir=".cursor", primitives={"prompts": make_mapping()}),
        ]
        entry = make_dispatch_entry(counter_key="prompts")

        result, integrators, _, logger = invoke_integrate(
            tmp_path,
            targets=targets,
            dispatch_table={"prompts": entry},
            integrator_results={"prompts": make_integration_result(files_integrated=1)},
            logger=MagicMock(),
        )

        assert integrators["prompt_integrator"].integrate_prompts_for_target.call_count == 2
        assert result["prompts"] == 2
        assert tree_messages(logger) == ["  |-- 2 prompts integrated -> .claude/, .cursor/"]

    def test_three_target_paths_collapse_to_count(self, tmp_path: Path) -> None:
        targets = [
            make_target(name="claude", root_dir=".claude", primitives={"prompts": make_mapping()}),
            make_target(name="cursor", root_dir=".cursor", primitives={"prompts": make_mapping()}),
            make_target(name="codex", root_dir=".codex", primitives={"prompts": make_mapping()}),
        ]
        entry = make_dispatch_entry(counter_key="prompts")

        _, _, _, logger = invoke_integrate(
            tmp_path,
            targets=targets,
            dispatch_table={"prompts": entry},
            integrator_results={"prompts": make_integration_result(files_integrated=1)},
            logger=MagicMock(),
        )

        assert tree_messages(logger) == ["  |-- 3 prompts integrated -> 3 targets"]

    def test_verbose_mode_expands_multiple_paths(self, tmp_path: Path) -> None:
        targets = [
            make_target(name="claude", root_dir=".claude", primitives={"prompts": make_mapping()}),
            make_target(name="cursor", root_dir=".cursor", primitives={"prompts": make_mapping()}),
        ]
        entry = make_dispatch_entry(counter_key="prompts")
        ctx = make_ctx(verbose=True)

        _, _, _, logger = invoke_integrate(
            tmp_path,
            targets=targets,
            dispatch_table={"prompts": entry},
            integrator_results={"prompts": make_integration_result(files_integrated=1)},
            logger=MagicMock(),
            ctx=ctx,
        )

        assert tree_messages(logger) == [
            "  |-- 2 prompts integrated:",
            "  |     -> .claude/",
            "  |     -> .cursor/",
        ]

    def test_duplicate_target_paths_are_deduplicated(self, tmp_path: Path) -> None:
        targets = [
            make_target(name="claude", root_dir=".claude", primitives={"prompts": make_mapping()}),
            make_target(
                name="claude-2", root_dir=".claude", primitives={"prompts": make_mapping()}
            ),
        ]
        entry = make_dispatch_entry(counter_key="prompts")

        _, _, _, logger = invoke_integrate(
            tmp_path,
            targets=targets,
            dispatch_table={"prompts": entry},
            integrator_results={"prompts": make_integration_result(files_integrated=1)},
            logger=MagicMock(),
        )

        assert tree_messages(logger) == ["  |-- 2 prompts integrated -> .claude/"]

    def test_skill_created_logs_single_path(self, tmp_path: Path) -> None:
        skill_path = tmp_path / ".claude" / "skills" / "demo" / "SKILL.md"

        result, _, _, logger = invoke_integrate(
            tmp_path,
            targets=[make_target(primitives={})],
            skill_result=make_skill_result(target_paths=[skill_path], skill_created=True),
            logger=MagicMock(),
        )

        assert result["skills"] == 1
        assert tree_messages(logger) == ["  |-- Skill integrated -> .claude/skills/"]

    def test_sub_skills_promoted_logs_count(self, tmp_path: Path) -> None:
        skill_path = tmp_path / ".claude" / "skills" / "demo" / "sub" / "SKILL.md"

        result, _, _, logger = invoke_integrate(
            tmp_path,
            targets=[make_target(primitives={})],
            skill_result=make_skill_result(target_paths=[skill_path], sub_skills_promoted=3),
            logger=MagicMock(),
        )

        assert result["sub_skills"] == 3
        assert tree_messages(logger) == ["  |-- 3 skill(s) integrated -> .claude/skills/"]

    def test_verbose_skill_paths_are_expanded(self, tmp_path: Path) -> None:
        skill_paths = [
            tmp_path / ".claude" / "skills" / "demo" / "SKILL.md",
            tmp_path / ".cursor" / "skills" / "demo" / "SKILL.md",
        ]

        _, _, _, logger = invoke_integrate(
            tmp_path,
            targets=[make_target(primitives={})],
            skill_result=make_skill_result(target_paths=skill_paths, skill_created=True),
            ctx=make_ctx(verbose=True),
            logger=MagicMock(),
        )

        assert tree_messages(logger) == [
            "  |-- Skill integrated:",
            "  |     -> .claude/skills/",
            "  |     -> .cursor/skills/",
        ]

    def test_out_of_tree_skill_paths_collapse_to_cowork(self, tmp_path: Path) -> None:
        skill_path = (
            tmp_path.parent / f"{tmp_path.name}-outside" / "skills" / "demo" / "SKILL.md"
        ).resolve()

        with patch(
            "apm_cli.install.services._deployed_path_entry",
            return_value="cowork://skills/demo/SKILL.md",
        ):
            _, _, _, logger = invoke_integrate(
                tmp_path,
                targets=[make_target(primitives={})],
                skill_result=make_skill_result(target_paths=[skill_path], skill_created=True),
                logger=MagicMock(),
            )

        assert tree_messages(logger) == ["  |-- Skill integrated -> copilot-cowork/skills/"]

    def test_empty_skill_target_paths_use_default_skills_dir(self, tmp_path: Path) -> None:
        _, _, _, logger = invoke_integrate(
            tmp_path,
            targets=[make_target(primitives={})],
            skill_result=make_skill_result(skill_created=True),
            logger=MagicMock(),
        )

        assert tree_messages(logger) == ["  |-- Skill integrated -> skills/"]

    def test_files_unchanged_annotation_when_nothing_integrated(self, tmp_path: Path) -> None:
        _, _, _, logger = invoke_integrate(
            tmp_path,
            targets=[make_target(primitives={})],
            logger=MagicMock(),
        )

        assert tree_messages(logger) == ["  |-- (files unchanged)"]

    def test_files_unchanged_not_logged_when_work_happened(self, tmp_path: Path) -> None:
        target = make_target(primitives={"prompts": make_mapping(subdir="prompts")})
        entry = make_dispatch_entry(counter_key="prompts")

        _, _, _, logger = invoke_integrate(
            tmp_path,
            targets=[target],
            dispatch_table={"prompts": entry},
            integrator_results={"prompts": make_integration_result(files_integrated=1)},
            logger=MagicMock(),
        )

        assert "  |-- (files unchanged)" not in tree_messages(logger)

    def test_copilot_app_hint_is_logged_for_integrated_files(self, tmp_path: Path) -> None:
        target = make_target(
            primitives={"commands": make_mapping(deploy_root="copilot-app", subdir="workflows")}
        )
        entry = make_dispatch_entry(
            integrate_method="integrate_commands_for_target",
            counter_key="commands",
        )

        _, _, _, logger = invoke_integrate(
            tmp_path,
            targets=[target],
            dispatch_table={"commands": entry},
            integrator_results={"commands": make_integration_result(files_integrated=1)},
            logger=MagicMock(),
        )

        assert tree_messages(logger) == [
            "  |-- 1 commands integrated -> copilot-app/workflows/",
            "  |-- workflows arrive disabled; enable from the Copilot App's Workflows tab",
        ]

    def test_copilot_app_hint_is_not_logged_for_adopted_only(self, tmp_path: Path) -> None:
        target = make_target(
            primitives={"commands": make_mapping(deploy_root="copilot-app", subdir="workflows")}
        )
        entry = make_dispatch_entry(
            integrate_method="integrate_commands_for_target",
            counter_key="commands",
        )

        _, _, _, logger = invoke_integrate(
            tmp_path,
            targets=[target],
            dispatch_table={"commands": entry},
            integrator_results={
                "commands": make_integration_result(files_integrated=0, files_adopted=1)
            },
            logger=MagicMock(),
        )

        assert tree_messages(logger) == [
            "  |-- 1 commands adopted -> copilot-app/workflows/",
            "  |-- (files unchanged)",
        ]

    def test_skill_integrator_receives_targets_and_subset(self, tmp_path: Path) -> None:
        targets = [make_target(primitives={})]
        subset = ("demo",)

        _, integrators, _, _ = invoke_integrate(
            tmp_path,
            targets=targets,
            skill_subset=subset,
        )

        call = integrators["skill_integrator"].integrate_package_skill.call_args
        assert call.kwargs["targets"] == targets
        assert call.kwargs["skill_subset"] == subset

    def test_package_name_override_is_used_in_cowork_warning(self, tmp_path: Path) -> None:
        package_dir = tmp_path / "pkg"
        prompts_dir = package_dir / ".apm" / "prompts"
        prompts_dir.mkdir(parents=True)
        (prompts_dir / "prompt.md").write_text("prompt")
        logger = MagicMock()
        diagnostics = MagicMock()
        ctx = make_ctx()

        with patch("apm_cli.integration.dispatch.get_dispatch_table", return_value={}):
            services.integrate_package_primitives(
                make_package_info(package_dir),
                tmp_path,
                targets=[make_target(name="copilot-cowork", primitives={})],
                integrators=IntegratorBundle(
                    prompt=MagicMock(),
                    agent=MagicMock(),
                    skill=MagicMock(
                        integrate_package_skill=MagicMock(return_value=make_skill_result())
                    ),
                    instruction=MagicMock(),
                    command=MagicMock(),
                    hook=MagicMock(),
                ),
                force=False,
                managed_files=set(),
                diagnostics=diagnostics,
                package_name="override-name",
                logger=logger,
                ctx=ctx,
            )

        assert "override-name" in diagnostics.warn.call_args.args[0]

    def test_package_info_name_is_used_when_package_name_empty(self, tmp_path: Path) -> None:
        package_dir = tmp_path / "pkg"
        hooks_dir = package_dir / ".apm" / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "hook.sh").write_text("hook")
        diagnostics = MagicMock()
        ctx = make_ctx()
        pkg_info = make_package_info(package_dir)
        pkg_info.name = "pkg-info-name"

        with patch("apm_cli.integration.dispatch.get_dispatch_table", return_value={}):
            services.integrate_package_primitives(
                pkg_info,
                tmp_path,
                targets=[make_target(name="copilot-cowork", primitives={})],
                integrators=IntegratorBundle(
                    prompt=MagicMock(),
                    agent=MagicMock(),
                    skill=MagicMock(
                        integrate_package_skill=MagicMock(return_value=make_skill_result())
                    ),
                    instruction=MagicMock(),
                    command=MagicMock(),
                    hook=MagicMock(),
                ),
                force=False,
                managed_files=set(),
                diagnostics=diagnostics,
                package_name="",
                logger=MagicMock(),
                ctx=ctx,
            )

        assert "pkg-info-name" in diagnostics.warn.call_args.args[0]

    def test_dispatch_uses_deploy_root_when_present(self, tmp_path: Path) -> None:
        target = make_target(
            primitives={"prompts": make_mapping(deploy_root=".github", subdir="prompts")}
        )
        entry = make_dispatch_entry(counter_key="prompts")

        _, _, _, logger = invoke_integrate(
            tmp_path,
            targets=[target],
            dispatch_table={"prompts": entry},
            integrator_results={"prompts": make_integration_result(files_integrated=1)},
            logger=MagicMock(),
        )

        assert tree_messages(logger) == ["  |-- 1 prompts integrated -> .github/prompts/"]

    def test_deployed_path_entry_called_for_each_target_path(self, tmp_path: Path) -> None:
        target_paths = [tmp_path / "a", tmp_path / "b"]
        target = make_target(primitives={"prompts": make_mapping()})
        entry = make_dispatch_entry(counter_key="prompts")

        with patch(
            "apm_cli.install.services._deployed_path_entry",
            side_effect=["a", "b"],
        ) as mock_entry:
            result, _, _, _ = invoke_integrate(
                tmp_path,
                targets=[target],
                dispatch_table={"prompts": entry},
                integrator_results={
                    "prompts": make_integration_result(
                        files_integrated=1,
                        target_paths=target_paths,
                    )
                },
            )

        assert result["deployed_files"] == ["a", "b"]
        assert mock_entry.call_count == 2


class TestIntegrateLocalContent:
    def test_calls_integrate_package_primitives_via_module_lookup(self, tmp_path: Path) -> None:
        with patch(
            "apm_cli.install.services.integrate_package_primitives",
            return_value={"ok": True},
        ) as mock_integrate:
            result = services.integrate_local_content(
                tmp_path,
                targets=[],
                prompt_integrator=MagicMock(),
                agent_integrator=MagicMock(),
                skill_integrator=MagicMock(),
                instruction_integrator=MagicMock(),
                command_integrator=MagicMock(),
                hook_integrator=MagicMock(),
                force=False,
                managed_files=set(),
                diagnostics=MagicMock(),
            )

        assert result == {"ok": True}
        local_info = mock_integrate.call_args.args[0]
        assert local_info.package.name == "_local"
        assert local_info.install_path == tmp_path
        assert mock_integrate.call_args.kwargs["package_name"] == "_local"

    def test_missing_apm_dir_means_no_local_integration(self, tmp_path: Path) -> None:
        logger = MagicMock()
        result = services.integrate_local_content(
            tmp_path,
            targets=[make_target(primitives={})],
            prompt_integrator=MagicMock(),
            agent_integrator=MagicMock(),
            skill_integrator=MagicMock(
                integrate_package_skill=MagicMock(return_value=make_skill_result())
            ),
            instruction_integrator=MagicMock(),
            command_integrator=MagicMock(),
            hook_integrator=MagicMock(),
            force=False,
            managed_files=set(),
            diagnostics=MagicMock(),
            logger=logger,
        )

        assert result["prompts"] == 0
        assert result["agents"] == 0
        assert result["skills"] == 0
        assert tree_messages(logger) == ["  |-- (files unchanged)"]
