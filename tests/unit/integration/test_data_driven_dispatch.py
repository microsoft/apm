"""Tests for the data-driven target x primitive dispatch architecture.

Validates that:
- Target gating correctly restricts which directories are written.
- Every (target, primitive) pair has a dispatch path.
- Synthetic TargetProfiles work without code changes.
- partition_managed_files produces correct bucket keys.
"""

import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from apm_cli.integration.base_integrator import BaseIntegrator, IntegrationResult
from apm_cli.integration.targets import KNOWN_TARGETS, PrimitiveMapping, TargetProfile
from apm_cli.commands.install import _integrate_package_primitives


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _make_integration_result(n=0):
    """Return an IntegrationResult with *n* files integrated."""
    return IntegrationResult(
        files_integrated=n,
        files_updated=0,
        files_skipped=0,
        target_paths=[],
        links_resolved=0,
    )


def _make_hook_result(n=0):
    """Return a MagicMock mimicking HookIntegrationResult."""
    hr = MagicMock()
    hr.files_integrated = n
    hr.target_paths = []
    return hr


def _make_skill_result():
    """Return a MagicMock mimicking SkillIntegrationResult."""
    sr = MagicMock()
    sr.skill_created = False
    sr.sub_skills_promoted = 0
    sr.target_paths = []
    return sr


def _make_mock_integrators():
    """Build a dict of mock integrators matching _integrate_package_primitives kwargs."""
    prompt = MagicMock()
    prompt.integrate_prompts_for_target = MagicMock(return_value=_make_integration_result())

    agent = MagicMock()
    agent.integrate_agents_for_target = MagicMock(return_value=_make_integration_result())

    command = MagicMock()
    command.integrate_commands_for_target = MagicMock(return_value=_make_integration_result())

    instruction = MagicMock()
    instruction.integrate_instructions_for_target = MagicMock(return_value=_make_integration_result())

    hook = MagicMock()
    hook.integrate_hooks_for_target = MagicMock(return_value=_make_hook_result())

    skill = MagicMock()
    skill.integrate_package_skill = MagicMock(return_value=_make_skill_result())

    return {
        "prompt_integrator": prompt,
        "agent_integrator": agent,
        "command_integrator": command,
        "instruction_integrator": instruction,
        "hook_integrator": hook,
        "skill_integrator": skill,
    }


def _dispatch(targets, integrators=None, package_info=None, project_root=None):
    """Call _integrate_package_primitives with defaults for convenience."""
    if integrators is None:
        integrators = _make_mock_integrators()
    if package_info is None:
        package_info = MagicMock()
    if project_root is None:
        project_root = Path("/fake/root")
    return _integrate_package_primitives(
        package_info,
        project_root,
        targets=targets,
        force=False,
        managed_files=set(),
        diagnostics=None,
        **integrators,
    ), integrators


# ===================================================================
# 1. TestTargetGatingRegression
# ===================================================================

class TestTargetGatingRegression:
    """Verify that the dispatch loop only invokes integrators for the
    primitives declared by each target, preventing cross-target writes."""

    def test_opencode_only_does_not_write_github_dirs(self):
        """With targets=[opencode], no .github/ primitive is dispatched."""
        targets = [KNOWN_TARGETS["opencode"]]
        _result, mocks = _dispatch(targets)

        # opencode does not declare prompts or instructions (those are copilot/cursor)
        for call_args in mocks["prompt_integrator"].integrate_prompts_for_target.call_args_list:
            target = call_args[0][0]
            assert target.root_dir != ".github"

        for call_args in mocks["instruction_integrator"].integrate_instructions_for_target.call_args_list:
            target = call_args[0][0]
            assert target.root_dir != ".github"

        # opencode has no hooks -- hook integrator should NOT be called for .github
        for call_args in mocks["hook_integrator"].integrate_hooks_for_target.call_args_list:
            target = call_args[0][0]
            assert target.root_dir != ".github"

    def test_cursor_only_does_not_write_claude_or_github(self):
        """With targets=[cursor], no .claude/ or .github/ primitives fire."""
        targets = [KNOWN_TARGETS["cursor"]]
        _result, mocks = _dispatch(targets)

        all_calls = []
        for name in ("prompt_integrator", "agent_integrator",
                      "command_integrator", "instruction_integrator",
                      "hook_integrator"):
            for method_name, method in vars(mocks[name]).items():
                if hasattr(method, "call_args_list"):
                    for call_args in method.call_args_list:
                        if call_args[0]:
                            target = call_args[0][0]
                            if hasattr(target, "root_dir"):
                                all_calls.append(target.root_dir)

        assert ".claude" not in all_calls
        assert ".github" not in all_calls

    def test_copilot_only_does_not_write_cursor_or_opencode(self):
        """With targets=[copilot], no .cursor/ or .opencode/ primitives fire."""
        targets = [KNOWN_TARGETS["copilot"]]
        _result, mocks = _dispatch(targets)

        dispatched_roots = set()
        for name in ("prompt_integrator", "agent_integrator",
                      "command_integrator", "instruction_integrator",
                      "hook_integrator"):
            for attr_name in dir(mocks[name]):
                method = getattr(mocks[name], attr_name)
                if hasattr(method, "call_args_list"):
                    for call_args in method.call_args_list:
                        if call_args[0] and hasattr(call_args[0][0], "root_dir"):
                            dispatched_roots.add(call_args[0][0].root_dir)

        assert ".cursor" not in dispatched_roots
        assert ".opencode" not in dispatched_roots

    def test_codex_only_does_not_write_github_or_claude_dirs(self):
        """With targets=[codex], no .github/ or .claude/ primitive is dispatched."""
        targets = [KNOWN_TARGETS["codex"]]
        _result, mocks = _dispatch(targets)
        dispatched_roots = set()
        for name in ("prompt_integrator", "agent_integrator",
                      "command_integrator", "instruction_integrator",
                      "hook_integrator"):
            for attr_name in dir(mocks[name]):
                method = getattr(mocks[name], attr_name)
                if hasattr(method, "call_args_list"):
                    for call_args in method.call_args_list:
                        if call_args[0] and hasattr(call_args[0][0], "root_dir"):
                            dispatched_roots.add(call_args[0][0].root_dir)
        assert ".github" not in dispatched_roots
        assert ".claude" not in dispatched_roots
        assert ".cursor" not in dispatched_roots
        assert ".opencode" not in dispatched_roots

    def test_empty_targets_returns_zeros(self):
        """With targets=[], all counters are 0 and no integrators are called."""
        result, mocks = _dispatch(targets=[])

        assert result["prompts"] == 0
        assert result["agents"] == 0
        assert result["instructions"] == 0
        assert result["commands"] == 0
        assert result["hooks"] == 0
        assert result["skills"] == 0
        assert result["deployed_files"] == []

        # No target-driven methods should have been called
        mocks["prompt_integrator"].integrate_prompts_for_target.assert_not_called()
        mocks["agent_integrator"].integrate_agents_for_target.assert_not_called()
        mocks["command_integrator"].integrate_commands_for_target.assert_not_called()
        mocks["instruction_integrator"].integrate_instructions_for_target.assert_not_called()
        mocks["hook_integrator"].integrate_hooks_for_target.assert_not_called()
        # Skills are also gated by early return
        mocks["skill_integrator"].integrate_package_skill.assert_not_called()

    def test_all_targets_dispatches_all_primitives(self):
        """With all 4 targets, every primitive in every target is dispatched."""
        all_targets = list(KNOWN_TARGETS.values())
        _result, mocks = _dispatch(targets=all_targets)

        # Collect (target_name, method_name) pairs that were called
        dispatched = set()
        method_map = {
            "prompt_integrator": "integrate_prompts_for_target",
            "agent_integrator": "integrate_agents_for_target",
            "command_integrator": "integrate_commands_for_target",
            "instruction_integrator": "integrate_instructions_for_target",
            "hook_integrator": "integrate_hooks_for_target",
        }
        prim_from_method = {
            "integrate_prompts_for_target": "prompts",
            "integrate_agents_for_target": "agents",
            "integrate_commands_for_target": "commands",
            "integrate_instructions_for_target": "instructions",
            "integrate_hooks_for_target": "hooks",
        }

        for int_name, method_name in method_map.items():
            method = getattr(mocks[int_name], method_name)
            for call_args in method.call_args_list:
                target = call_args[0][0]
                prim = prim_from_method[method_name]
                dispatched.add((target.name, prim))

        # Verify every non-skills primitive in each target was dispatched
        for target in all_targets:
            for prim_name in target.primitives:
                if prim_name == "skills":
                    continue  # skills handled separately
                assert (target.name, prim_name) in dispatched, (
                    f"Expected ({target.name}, {prim_name}) to be dispatched"
                )


# ===================================================================
# 2. TestExhaustivenessChecks
# ===================================================================

class TestExhaustivenessChecks:
    """Structural checks ensuring no target x primitive pair is orphaned."""

    def test_every_target_primitive_has_dispatch_path(self):
        """For each (target, primitive) in KNOWN_TARGETS, verify the dispatch
        table has a corresponding entry."""
        from apm_cli.integration.dispatch import get_dispatch_table
        dispatch = get_dispatch_table()

        for target_name, profile in KNOWN_TARGETS.items():
            for prim_name in profile.primitives:
                assert prim_name in dispatch, (
                    f"Primitive '{prim_name}' in target '{target_name}' has no "
                    f"entry in the dispatch table."
                )

    def test_partition_parity_with_old_buckets(self):
        """Verify partition_managed_files produces the expected bucket keys
        that callers rely on (backward-compat aliases applied)."""
        # Use an empty set -- we only care about the keys produced
        buckets = BaseIntegrator.partition_managed_files(set())

        # Expected keys from the old hardcoded version:
        expected_keys = {
            "prompts",             # was prompts_copilot, aliased
            "agents_github",       # was agents_copilot, aliased
            "agents_claude",
            "agents_cursor",
            "agents_opencode",
            "agents_codex",
            "commands",            # was commands_claude, aliased
            "commands_gemini",
            "commands_opencode",
            "instructions",        # was instructions_copilot, aliased
            "rules_cursor",        # was instructions_cursor, aliased
            "rules_claude",        # was instructions_claude, aliased
            "skills",              # cross-target bucket
            "hooks",               # cross-target bucket
        }

        assert expected_keys == set(buckets.keys()), (
            f"Bucket keys mismatch.\n"
            f"  Missing: {expected_keys - set(buckets.keys())}\n"
            f"  Extra:   {set(buckets.keys()) - expected_keys}"
        )


# ===================================================================
# 3. TestSyntheticTargetProfile
# ===================================================================

class TestSyntheticTargetProfile:
    """Verify that a hand-built TargetProfile works end-to-end without
    any code changes -- proving the architecture is truly data-driven."""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.root = Path(self.temp_dir)

    def teardown_method(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_synthetic_target_integrates_successfully(self):
        """A synthetic TargetProfile with a custom root_dir (.newcode)
        passes through integrate_commands_for_target without errors."""
        from apm_cli.integration.command_integrator import CommandIntegrator

        synthetic = TargetProfile(
            name="newcode",
            root_dir=".newcode",
            primitives={
                "commands": PrimitiveMapping("cmds", ".md", "newcode_cmd"),
            },
            auto_create=True,
            detect_by_dir=False,
        )

        # CommandIntegrator.find_prompt_files() discovers .prompt.md files
        # in .apm/prompts/ and transforms them to command format.
        pkg_dir = self.root / "packages" / "test-pkg"
        prompts_dir = pkg_dir / ".apm" / "prompts"
        prompts_dir.mkdir(parents=True)
        (prompts_dir / "hello.prompt.md").write_text(
            "---\nname: hello\n---\nHello world"
        )

        # Create the target root so integration proceeds
        (self.root / ".newcode").mkdir(parents=True)

        package_info = MagicMock()
        package_info.install_path = pkg_dir
        package_info.resolved_reference = None
        package_info.package = MagicMock()
        package_info.package.name = "test-pkg"

        integrator = CommandIntegrator()
        result = integrator.integrate_commands_for_target(
            synthetic, package_info, self.root,
            force=False, managed_files=set(),
        )

        assert result.files_integrated == 1
        assert len(result.target_paths) == 1
        # Verify the file landed under the synthetic root_dir
        deployed = result.target_paths[0]
        assert ".newcode" in deployed.parts
        assert "cmds" in deployed.parts
        assert deployed.name == "hello.md"

    def test_synthetic_target_sync_computes_correct_prefix(self):
        """sync_for_target uses the synthetic target's root_dir/subdir
        to compute the correct prefix for file removal."""
        from apm_cli.integration.command_integrator import CommandIntegrator

        synthetic = TargetProfile(
            name="newcode",
            root_dir=".newcode",
            primitives={
                "commands": PrimitiveMapping("cmds", ".md", "newcode_cmd"),
            },
            auto_create=True,
            detect_by_dir=False,
        )

        apm_package = MagicMock()
        apm_package.name = "test-pkg"

        integrator = CommandIntegrator()

        # Provide managed files under the synthetic prefix
        managed = {
            ".newcode/cmds/hello.md",
            ".newcode/cmds/goodbye.md",
            ".claude/commands/other.md",  # should NOT be removed
        }

        # Create the files so sync can actually remove them
        cmds_dir = self.root / ".newcode" / "cmds"
        cmds_dir.mkdir(parents=True)
        (cmds_dir / "hello.md").write_text("test")
        (cmds_dir / "goodbye.md").write_text("test")

        claude_dir = self.root / ".claude" / "commands"
        claude_dir.mkdir(parents=True)
        (claude_dir / "other.md").write_text("test")

        # sync_for_target passes targets=[synthetic] through to
        # validate_deploy_path, so .newcode/ prefix is accepted
        # without patching.
        result = integrator.sync_for_target(
            synthetic, apm_package, self.root,
            managed_files=managed,
        )

        # The .newcode files should be removed
        assert result["files_removed"] == 2
        assert not (cmds_dir / "hello.md").exists()
        assert not (cmds_dir / "goodbye.md").exists()
        # The .claude file should still exist (different prefix)
        assert (claude_dir / "other.md").exists()


# ===================================================================
# 4. TestSkillTargetGating  (Issue #482 regression)
# ===================================================================

class TestSkillTargetGating:
    """Verify that the skill integrator respects the targets parameter
    passed from the dispatch loop, preventing cross-target skill writes."""

    def test_skill_integrator_receives_targets_from_dispatch(self):
        """_integrate_package_primitives passes its targets list to
        skill_integrator.integrate_package_skill (Issue #482)."""
        cursor_only = [KNOWN_TARGETS["cursor"]]
        _result, mocks = _dispatch(targets=cursor_only)

        # Verify skill integrator was called with targets= kwarg
        call_kwargs = mocks["skill_integrator"].integrate_package_skill.call_args
        assert call_kwargs is not None, "skill integrator was not called"
        assert "targets" in call_kwargs.kwargs, (
            "targets= not passed to skill integrator"
        )
        passed_targets = call_kwargs.kwargs["targets"]
        assert len(passed_targets) == 1
        assert passed_targets[0].name == "cursor"

    def test_opencode_target_does_not_pass_copilot_to_skills(self):
        """With targets=[opencode], skill integrator only gets opencode."""
        opencode_only = [KNOWN_TARGETS["opencode"]]
        _result, mocks = _dispatch(targets=opencode_only)

        call_kwargs = mocks["skill_integrator"].integrate_package_skill.call_args
        passed_targets = call_kwargs.kwargs["targets"]
        assert all(t.name == "opencode" for t in passed_targets)

    def test_empty_targets_skips_skill_integrator(self):
        """With targets=[], skill integrator is not called at all."""
        _result, mocks = _dispatch(targets=[])
        mocks["skill_integrator"].integrate_package_skill.assert_not_called()


# ===================================================================
# 5. TestPartitionBucketKey
# ===================================================================

class TestPartitionBucketKey:
    """Verify that partition_bucket_key produces the correct aliased keys."""

    def test_copilot_prompts_alias(self):
        assert BaseIntegrator.partition_bucket_key("prompts", "copilot") == "prompts"

    def test_copilot_agents_alias(self):
        assert BaseIntegrator.partition_bucket_key("agents", "copilot") == "agents_github"

    def test_claude_commands_alias(self):
        assert BaseIntegrator.partition_bucket_key("commands", "claude") == "commands"

    def test_cursor_instructions_alias(self):
        assert BaseIntegrator.partition_bucket_key("instructions", "cursor") == "rules_cursor"

    def test_claude_instructions_alias(self):
        assert BaseIntegrator.partition_bucket_key("instructions", "claude") == "rules_claude"

    def test_unaliased_key_passthrough(self):
        assert BaseIntegrator.partition_bucket_key("agents", "cursor") == "agents_cursor"


# ===================================================================
# 6. TestCodexPartitionRouting
# ===================================================================

class TestCodexPartitionRouting:
    """Verify that Codex deployed_files are routed to correct buckets."""

    def test_partition_routes_codex_paths_correctly(self):
        """Codex deployed_files are routed to correct buckets."""
        managed = {
            ".codex/agents/my-agent.toml",
            ".agents/skills/my-skill/SKILL.md",
            ".codex/hooks/pkg/script.sh",
        }
        buckets = BaseIntegrator.partition_managed_files(managed)
        assert ".agents/skills/my-skill/SKILL.md" in buckets["skills"]
        # Codex agents under .codex/agents/ route to the agents_codex bucket.
        assert ".codex/agents/my-agent.toml" in buckets["agents_codex"]
        # Only true Codex hook paths route to the hooks bucket.
        assert ".codex/hooks/pkg/script.sh" in buckets["hooks"]


class TestClaudeRulesPartitionRouting:
    """Verify that Claude rules deployed_files are routed to the correct bucket."""

    def test_partition_routes_claude_rules_correctly(self):
        managed = {
            ".claude/rules/python.md",
            ".claude/rules/testing.md",
            ".claude/agents/reviewer.md",
        }
        buckets = BaseIntegrator.partition_managed_files(managed)
        assert ".claude/rules/python.md" in buckets["rules_claude"]
        assert ".claude/rules/testing.md" in buckets["rules_claude"]
        assert ".claude/agents/reviewer.md" in buckets["agents_claude"]


# ===================================================================
# 7. TestIntegrationPrefixSecurity
# ===================================================================

class TestIntegrationPrefixSecurity:
    """Verify integration prefixes include deploy_root paths."""

    def test_integration_prefixes_include_agents_dir(self):
        """get_integration_prefixes() includes .agents/ from deploy_root."""
        from apm_cli.integration.targets import get_integration_prefixes
        prefixes = get_integration_prefixes()
        assert ".agents/" in prefixes
        assert ".codex/" in prefixes

    def test_deploy_root_validation(self):
        """validate_deploy_path accepts .agents/skills/ paths."""
        root = Path("/fake/project")
        assert BaseIntegrator.validate_deploy_path(".agents/skills/my-skill/SKILL.md", root)
        assert BaseIntegrator.validate_deploy_path(".codex/agents/my-agent.toml", root)


# ===================================================================
# 8. TestGetIntegrationPrefixesTargetsParam
# ===================================================================


class TestGetIntegrationPrefixesTargetsParam:
    """Verify get_integration_prefixes with explicit targets parameter."""

    def test_prefixes_from_resolved_copilot(self):
        """Resolved copilot target yields .copilot/ prefix."""
        from dataclasses import replace
        from apm_cli.integration.targets import get_integration_prefixes

        resolved = replace(KNOWN_TARGETS["copilot"], root_dir=".copilot")
        prefixes = get_integration_prefixes(targets=[resolved])
        assert ".copilot/" in prefixes
        # Should NOT include .github/ since we only passed resolved copilot
        assert ".github/" not in prefixes

    def test_prefixes_backward_compat(self):
        """No targets param returns default KNOWN_TARGETS prefixes."""
        from apm_cli.integration.targets import get_integration_prefixes

        prefixes = get_integration_prefixes()
        assert ".github/" in prefixes
        assert ".claude/" in prefixes


# ===================================================================
# 9. TestScopeResolvedPartition
# ===================================================================


class TestScopeResolvedPartition:
    """Verify partition_managed_files and validate_deploy_path with
    scope-resolved targets."""

    def test_partition_with_user_scope_copilot_targets(self):
        """Partition routes .copilot/ paths when given resolved targets."""
        from dataclasses import replace

        copilot = KNOWN_TARGETS["copilot"]
        resolved = replace(copilot, root_dir=".copilot")
        managed = {
            ".copilot/agents/my-agent.md",
            ".copilot/skills/my-skill/SKILL.md",
        }
        buckets = BaseIntegrator.partition_managed_files(managed, targets=[resolved])
        assert ".copilot/agents/my-agent.md" in buckets.get("agents_github", set())
        assert ".copilot/skills/my-skill/SKILL.md" in buckets.get("skills", set())

    def test_partition_with_opencode_user_scope(self):
        """Partition routes .config/opencode/ paths correctly."""
        from dataclasses import replace

        opencode = KNOWN_TARGETS["opencode"]
        resolved = replace(opencode, root_dir=".config/opencode")
        managed = {
            ".config/opencode/agents/reviewer.md",
            ".config/opencode/commands/test.md",
            ".config/opencode/skills/my-skill/SKILL.md",
        }
        buckets = BaseIntegrator.partition_managed_files(managed, targets=[resolved])
        assert ".config/opencode/agents/reviewer.md" in buckets.get(
            "agents_opencode", set()
        )
        assert ".config/opencode/commands/test.md" in buckets.get(
            "commands_opencode", set()
        )
        assert ".config/opencode/skills/my-skill/SKILL.md" in buckets.get(
            "skills", set()
        )

    def test_partition_backward_compat_no_targets(self):
        """Without targets param, uses KNOWN_TARGETS (existing behavior)."""
        managed = {
            ".github/prompts/test.prompt.md",
            ".claude/commands/test.md",
        }
        buckets = BaseIntegrator.partition_managed_files(managed)
        assert ".github/prompts/test.prompt.md" in buckets.get("prompts", set())
        assert ".claude/commands/test.md" in buckets.get("commands", set())

    def test_validate_deploy_path_with_resolved_targets(self):
        """validate_deploy_path accepts .copilot/ with resolved targets."""
        from dataclasses import replace

        copilot = KNOWN_TARGETS["copilot"]
        resolved = replace(copilot, root_dir=".copilot")
        root = Path("/fake/home")
        assert BaseIntegrator.validate_deploy_path(
            ".copilot/agents/my-agent.md",
            root,
            targets=[resolved],
        )

    def test_validate_deploy_path_rejects_copilot_without_resolved(self):
        """validate_deploy_path rejects .copilot/ without resolved targets."""
        root = Path("/fake/home")
        assert not BaseIntegrator.validate_deploy_path(
            ".copilot/agents/my-agent.md",
            root,
        )

    def test_validate_deploy_path_backward_compat(self):
        """Default (no targets) preserves existing behavior."""
        root = Path("/fake/project")
        assert BaseIntegrator.validate_deploy_path(".github/prompts/test.md", root)
        assert BaseIntegrator.validate_deploy_path(".claude/commands/test.md", root)
        assert not BaseIntegrator.validate_deploy_path("../escape.md", root)

    def test_partition_codex_still_works(self):
        """Codex deployed_files are routed to correct buckets."""
        managed = {
            ".codex/agents/my-agent.toml",
            ".agents/skills/my-skill/SKILL.md",
            ".codex/hooks/pkg/script.sh",
        }
        buckets = BaseIntegrator.partition_managed_files(managed)
        assert ".codex/agents/my-agent.toml" in buckets.get("agents_codex", set())
        assert ".agents/skills/my-skill/SKILL.md" in buckets.get("skills", set())
        assert ".codex/hooks/pkg/script.sh" in buckets.get("hooks", set())


# ===================================================================
# 8. TestForScope
# ===================================================================


class TestForScope:
    """Verify TargetProfile.for_scope() and resolve_targets()."""

    def test_project_scope_returns_self(self):
        """for_scope(user_scope=False) returns the same object."""
        from apm_cli.integration.targets import KNOWN_TARGETS
        copilot = KNOWN_TARGETS["copilot"]
        assert copilot.for_scope(user_scope=False) is copilot

    def test_codex_is_supported_at_user_scope(self):
        """Codex resolves cleanly at user scope."""
        from apm_cli.integration.targets import KNOWN_TARGETS
        codex = KNOWN_TARGETS["codex"]
        assert codex.user_supported == "partial"
        resolved = codex.for_scope(user_scope=True)
        assert resolved is not None
        assert resolved.root_dir == ".codex"

    def test_resolves_root_dir_to_user_root(self):
        """for_scope replaces root_dir with user_root_dir."""
        from apm_cli.integration.targets import KNOWN_TARGETS
        copilot = KNOWN_TARGETS["copilot"]
        resolved = copilot.for_scope(user_scope=True)
        assert resolved is not None
        assert resolved.root_dir == ".copilot"
        assert resolved.name == "copilot"

    def test_user_root_dir_none_keeps_root_dir(self):
        """When user_root_dir is None, root_dir stays unchanged."""
        from apm_cli.integration.targets import KNOWN_TARGETS
        claude = KNOWN_TARGETS["claude"]
        assert claude.user_root_dir is None
        resolved = claude.for_scope(user_scope=True)
        assert resolved is not None
        assert resolved.root_dir == ".claude"

    def test_filters_unsupported_primitives(self):
        """for_scope removes unsupported primitives from the dict."""
        from apm_cli.integration.targets import KNOWN_TARGETS
        copilot = KNOWN_TARGETS["copilot"]
        assert "prompts" in copilot.primitives
        assert "instructions" in copilot.primitives
        resolved = copilot.for_scope(user_scope=True)
        assert "prompts" not in resolved.primitives
        assert "instructions" not in resolved.primitives
        # Supported primitives remain
        assert "agents" in resolved.primitives
        assert "skills" in resolved.primitives
        assert "hooks" in resolved.primitives

    def test_no_unsupported_primitives_keeps_all(self):
        """Targets with empty unsupported_user_primitives keep all primitives."""
        from apm_cli.integration.targets import KNOWN_TARGETS
        claude = KNOWN_TARGETS["claude"]
        assert claude.unsupported_user_primitives == ()
        resolved = claude.for_scope(user_scope=True)
        assert resolved.primitives == claude.primitives

    def test_prefix_property_reflects_resolved_root(self):
        """The prefix property uses the resolved root_dir."""
        from apm_cli.integration.targets import KNOWN_TARGETS
        copilot = KNOWN_TARGETS["copilot"]
        resolved = copilot.for_scope(user_scope=True)
        assert resolved.prefix == ".copilot/"
        assert copilot.prefix == ".github/"

    def test_opencode_resolves_to_config_dir(self):
        """OpenCode resolves to .config/opencode at user scope."""
        from apm_cli.integration.targets import KNOWN_TARGETS
        opencode = KNOWN_TARGETS["opencode"]
        resolved = opencode.for_scope(user_scope=True)
        assert resolved is not None
        assert resolved.root_dir == ".config/opencode"

    def test_resolve_targets_project_scope(self):
        """resolve_targets at project scope returns unmodified profiles."""
        from apm_cli.integration.targets import resolve_targets
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # No target dirs exist, so fallback to copilot
            targets = resolve_targets(root, user_scope=False)
            assert len(targets) >= 1
            assert targets[0].name == "copilot"
            assert targets[0].root_dir == ".github"

    def test_resolve_targets_filters_unsupported(self):
        """resolve_targets at user scope includes all user-capable targets."""
        from apm_cli.integration.targets import resolve_targets, KNOWN_TARGETS
        from pathlib import Path
        targets = resolve_targets(Path.home(), user_scope=True, explicit_target="all")
        target_names = {t.name for t in targets}
        assert "codex" in target_names


# ===================================================================
# TestPrimitiveCoverage
# ===================================================================


class TestPrimitiveCoverage:
    """Verify that every primitive in KNOWN_TARGETS has a dispatch handler."""

    def test_all_primitives_covered(self):
        """Every primitive in KNOWN_TARGETS must have an integrator."""
        from apm_cli.integration.coverage import check_primitive_coverage
        from apm_cli.integration.dispatch import get_dispatch_table

        dispatch = get_dispatch_table()
        # Should not raise
        check_primitive_coverage(dispatch)

    def test_missing_primitive_raises(self):
        """A primitive without a handler triggers RuntimeError."""
        from apm_cli.integration.coverage import check_primitive_coverage
        import pytest

        # Deliberately omit "instructions"
        incomplete_dispatch = {
            "prompts": None, "agents": None, "commands": None,
        }
        with pytest.raises(RuntimeError, match="instructions"):
            check_primitive_coverage(
                incomplete_dispatch,
                special_cases={"skills", "hooks"},
            )

    def test_special_cases_excluded(self):
        """Primitives in special_cases are not required in the dispatch table."""
        from apm_cli.integration.coverage import check_primitive_coverage

        dispatch_keys = {
            "prompts", "agents", "commands", "instructions",
        }
        # skills and hooks are special-cased
        check_primitive_coverage(
            {k: None for k in dispatch_keys},
            special_cases={"skills", "hooks"},
        )


# ===================================================================
# 12. TestDispatchTable
# ===================================================================


class TestDispatchTable:
    """Verify the unified dispatch table."""

    def test_dispatch_table_has_all_primitives(self):
        from apm_cli.integration.dispatch import get_dispatch_table
        dispatch = get_dispatch_table()
        assert "prompts" in dispatch
        assert "agents" in dispatch
        assert "commands" in dispatch
        assert "instructions" in dispatch
        assert "hooks" in dispatch
        assert "skills" in dispatch

    def test_skills_is_multi_target(self):
        from apm_cli.integration.dispatch import get_dispatch_table
        dispatch = get_dispatch_table()
        assert dispatch["skills"].multi_target is True
        for name in ("prompts", "agents", "commands", "instructions", "hooks"):
            assert dispatch[name].multi_target is False

    def test_dispatch_entries_have_valid_methods(self):
        from apm_cli.integration.dispatch import get_dispatch_table
        dispatch = get_dispatch_table()
        for name, entry in dispatch.items():
            integrator = entry.integrator_class()
            assert hasattr(integrator, entry.integrate_method), (
                f"{entry.integrator_class.__name__} missing {entry.integrate_method}"
            )
            assert hasattr(integrator, entry.sync_method), (
                f"{entry.integrator_class.__name__} missing {entry.sync_method}"
            )

    def test_dispatch_counter_keys_match_result_dict(self):
        """Counter keys in dispatch match the keys used in install result."""
        from apm_cli.integration.dispatch import get_dispatch_table
        dispatch = get_dispatch_table()
        expected_counters = {"prompts", "agents", "commands", "instructions", "hooks", "skills"}
        actual_counters = {entry.counter_key for entry in dispatch.values()}
        assert actual_counters == expected_counters

    def test_lazy_initialization(self):
        """get_dispatch_table returns the same object on repeated calls."""
        from apm_cli.integration.dispatch import get_dispatch_table
        table1 = get_dispatch_table()
        table2 = get_dispatch_table()
        assert table1 is table2


# ===================================================================
# 13. TestCoverageReverse + TestHookResultShim
# ===================================================================


class TestCoverageReverse:
    """Verify bidirectional coverage checks."""

    def test_dead_dispatch_entry_raises(self):
        """An entry in the dispatch table with no matching target raises."""
        from apm_cli.integration.coverage import check_primitive_coverage
        import pytest

        # Add a fake primitive not in any target
        dispatch = {
            "prompts": None, "agents": None, "commands": None,
            "instructions": None, "hooks": None, "skills": None,
            "phantoms": None,  # not in any KNOWN_TARGETS
        }
        with pytest.raises(RuntimeError, match="phantoms"):
            check_primitive_coverage(dispatch)

    def test_full_dispatch_table_passes_bidirectional(self):
        """The real dispatch table passes both forward and reverse checks."""
        from apm_cli.integration.coverage import check_primitive_coverage
        from apm_cli.integration.dispatch import get_dispatch_table
        # Should not raise (checks both directions + method existence)
        check_primitive_coverage(get_dispatch_table())


class TestHookResultShim:
    """Verify HookIntegrationResult backward-compat construction."""

    def test_old_style_construction(self):
        """Old-style HookIntegrationResult(hooks_integrated=N) works."""
        from apm_cli.integration.hook_integrator import HookIntegrationResult
        r = HookIntegrationResult(hooks_integrated=5, scripts_copied=3, target_paths=[])
        assert r.hooks_integrated == 5
        assert r.files_integrated == 5
        assert r.scripts_copied == 3

    def test_old_style_no_target_paths(self):
        """Old-style construction without target_paths defaults to []."""
        from apm_cli.integration.hook_integrator import HookIntegrationResult
        r = HookIntegrationResult(hooks_integrated=0, scripts_copied=0)
        assert r.hooks_integrated == 0
        assert r.target_paths == []

    def test_new_style_construction(self):
        """New-style IntegrationResult fields work on HookIntegrationResult."""
        from apm_cli.integration.hook_integrator import HookIntegrationResult
        r = HookIntegrationResult(
            files_integrated=3, files_updated=0, files_skipped=0,
            target_paths=[], scripts_copied=1,
        )
        assert r.files_integrated == 3
        assert r.hooks_integrated == 3  # property alias
