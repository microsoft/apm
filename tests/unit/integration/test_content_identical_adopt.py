"""Regression tests: silently adopt byte-identical pre-existing files.

Closes the catch-22 where a degraded lockfile (missing ``deployed_files``
for non-skill packages) could never self-heal because the per-file loops
in ``agent_integrator``, ``instruction_integrator``, ``prompt_integrator``
and ``command_integrator`` treated content-identical files as
"user-authored" collisions, skipped them, and emitted an empty
``deployed_files`` -- which then tripped ``required-packages-deployed``
on the next install.

``skill_integrator._promote_sub_skills`` already had this short-circuit
(``target.exists()`` + ``_dirs_equal`` -> append + continue). These tests
lock in the symmetric behavior across non-skill primitives.

Scenarios per integrator:
    1. Pre-existing target byte-identical to source + ``managed_files=None``
       -> silently adopted (target_path appended, files_skipped == 0).
    2. Pre-existing target with DIFFERENT content + ``managed_files=None``
       -> still treated as user-authored collision (existing behavior).
    3. Pure helper: ``BaseIntegrator.is_content_identical_to_source``
       returns the right answer for present/absent/identical/divergent
       file pairs.
"""

from datetime import datetime
from pathlib import Path

from apm_cli.integration.agent_integrator import AgentIntegrator
from apm_cli.integration.base_integrator import BaseIntegrator
from apm_cli.integration.instruction_integrator import InstructionIntegrator
from apm_cli.integration.prompt_integrator import PromptIntegrator
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.models.apm_package import (
    APMPackage,
    GitReferenceType,
    PackageInfo,
    ResolvedReference,
)


def _make_package_info(package_dir: Path, name: str = "test-pkg") -> PackageInfo:
    package = APMPackage(
        name=name,
        version="1.0.0",
        package_path=package_dir,
        source=f"github.com/test/{name}",
    )
    resolved_ref = ResolvedReference(
        original_ref="main",
        ref_type=GitReferenceType.BRANCH,
        resolved_commit="abc123",
        ref_name="main",
    )
    return PackageInfo(
        package=package,
        install_path=package_dir,
        resolved_reference=resolved_ref,
        installed_at=datetime.now().isoformat(),
    )


# ---------------------------------------------------------------------------
# Pure helper
# ---------------------------------------------------------------------------


class TestIsContentIdenticalToSource:
    def test_identical_files_return_true(self, tmp_path: Path) -> None:
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.write_bytes(b"hello world\n")
        b.write_bytes(b"hello world\n")
        assert BaseIntegrator.is_content_identical_to_source(a, b) is True

    def test_divergent_files_return_false(self, tmp_path: Path) -> None:
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.write_bytes(b"hello world\n")
        b.write_bytes(b"different bytes\n")
        assert BaseIntegrator.is_content_identical_to_source(a, b) is False

    def test_target_missing_returns_false(self, tmp_path: Path) -> None:
        a = tmp_path / "missing"
        b = tmp_path / "present"
        b.write_bytes(b"x")
        assert BaseIntegrator.is_content_identical_to_source(a, b) is False

    def test_source_missing_returns_false(self, tmp_path: Path) -> None:
        a = tmp_path / "present"
        b = tmp_path / "missing"
        a.write_bytes(b"x")
        assert BaseIntegrator.is_content_identical_to_source(a, b) is False


# ---------------------------------------------------------------------------
# Instruction integrator -- the user's reproducer (zava-storefront)
# ---------------------------------------------------------------------------


class TestInstructionIntegratorAdopt:
    """Covers the secure-baseline scenario reported by zava-storefront."""

    def _build(self, tmp_path: Path, source_bytes: bytes) -> tuple[Path, Path, PackageInfo]:
        pkg_dir = tmp_path / "pkg"
        inst_dir = pkg_dir / ".apm" / "instructions"
        inst_dir.mkdir(parents=True)
        source = inst_dir / "secure-coding.instructions.md"
        source.write_bytes(source_bytes)

        deploy_dir = tmp_path / ".github" / "instructions"
        deploy_dir.mkdir(parents=True)
        target = deploy_dir / "secure-coding.instructions.md"

        return source, target, _make_package_info(pkg_dir)

    def test_identical_pre_existing_file_is_adopted_when_managed_none(self, tmp_path: Path) -> None:
        """Lockfile lost deployed_files -> next install must adopt, not skip.

        This is the exact catch-22: file already on disk, byte-identical
        to source, but absent from managed_files. Pre-fix: skipped, empty
        deployed_files -> policy block. Post-fix: adopted, target_paths
        populated -> deployed_files restored.
        """
        body = b"---\napplyTo: '**/*.py'\n---\n# Secure coding base\n"
        _, target, pkg_info = self._build(tmp_path, body)
        target.write_bytes(body)  # pre-existing, byte-identical

        result = InstructionIntegrator().integrate_instructions_for_target(
            KNOWN_TARGETS["copilot"],
            pkg_info,
            tmp_path,
            force=False,
            managed_files=None,  # <-- the degraded-lockfile state
        )

        assert target in result.target_paths, (
            "Byte-identical pre-existing file must be adopted into target_paths "
            "so apm.lock.deployed_files repopulates and policy gate passes."
        )
        assert result.files_skipped == 0, (
            "Adopted file must NOT count as skipped (would mislead users)."
        )
        # Bytes preserved (no-op write or no write at all -- both acceptable).
        assert target.read_bytes() == body

    def test_divergent_pre_existing_file_is_still_skipped(self, tmp_path: Path) -> None:
        """User-authored content with different bytes keeps the existing
        protection: skipped, content preserved."""
        source_body = b"---\napplyTo: '**/*.py'\n---\n# APM-provided\n"
        user_body = b"# my hand-rolled rules\n"
        _, target, pkg_info = self._build(tmp_path, source_body)
        target.write_bytes(user_body)

        result = InstructionIntegrator().integrate_instructions_for_target(
            KNOWN_TARGETS["copilot"],
            pkg_info,
            tmp_path,
            force=False,
            managed_files=None,
        )

        assert target not in result.target_paths
        assert result.files_skipped >= 1
        assert target.read_bytes() == user_body, "User-authored content must not be overwritten."


# ---------------------------------------------------------------------------
# Agent integrator -- secure-baseline ships .agent.md too
# ---------------------------------------------------------------------------


class TestAgentIntegratorAdopt:
    def _build(self, tmp_path: Path, body: bytes) -> tuple[Path, Path, PackageInfo]:
        pkg_dir = tmp_path / "pkg"
        agents_dir_src = pkg_dir / ".apm" / "agents"
        agents_dir_src.mkdir(parents=True)
        source = agents_dir_src / "security.agent.md"
        source.write_bytes(body)

        deploy_dir = tmp_path / ".github" / "agents"
        deploy_dir.mkdir(parents=True)
        # copilot keeps the .agent.md suffix (no rename for primary target)
        target = deploy_dir / "security.agent.md"

        return source, target, _make_package_info(pkg_dir)

    def test_identical_pre_existing_agent_is_adopted(self, tmp_path: Path) -> None:
        body = b"---\nname: security\n---\n# Security agent\n"
        _, target, pkg_info = self._build(tmp_path, body)
        target.write_bytes(body)

        result = AgentIntegrator().integrate_agents_for_target(
            KNOWN_TARGETS["copilot"],
            pkg_info,
            tmp_path,
            force=False,
            managed_files=None,
        )

        assert target in result.target_paths
        assert result.files_skipped == 0
        assert target.read_bytes() == body


# ---------------------------------------------------------------------------
# Prompt integrator
# ---------------------------------------------------------------------------


class TestPromptIntegratorAdopt:
    def test_identical_pre_existing_prompt_is_adopted(self, tmp_path: Path) -> None:
        pkg_dir = tmp_path / "pkg"
        prompts_src = pkg_dir / ".apm" / "prompts"
        prompts_src.mkdir(parents=True)
        body = b"---\nmode: agent\n---\n# Sample prompt\n"
        (prompts_src / "sample.prompt.md").write_bytes(body)

        deploy_dir = tmp_path / ".github" / "prompts"
        deploy_dir.mkdir(parents=True)
        target = deploy_dir / "sample.prompt.md"
        target.write_bytes(body)

        pkg_info = _make_package_info(pkg_dir)
        result = PromptIntegrator().integrate_prompts_for_target(
            KNOWN_TARGETS["copilot"],
            pkg_info,
            tmp_path,
            force=False,
            managed_files=None,
        )

        assert target in result.target_paths
        assert result.files_skipped == 0


# ---------------------------------------------------------------------------
# Symlink-rejection guard (TOCTOU defense in is_content_identical_to_source)
# ---------------------------------------------------------------------------


class TestIsContentIdenticalSymlinkGuard:
    """The adopt branch fires *before* check_collision's containment
    guard. Without symlink rejection, a co-tenant who pre-places a
    symlink at the deploy path -- pointing to an out-of-project file
    whose bytes match source -- could slip the symlink target into
    ``deployed_files``. These tests lock in the guard.
    """

    def test_target_is_symlink_returns_false(self, tmp_path: Path) -> None:
        body = b"identical bytes\n"
        source = tmp_path / "source"
        source.write_bytes(body)
        # Pre-place a symlink at target whose link-target is byte-identical
        decoy = tmp_path / "decoy_outside"
        decoy.write_bytes(body)
        target = tmp_path / "target"
        target.symlink_to(decoy)

        # Both files exist; both have identical bytes when followed.
        # Guard MUST refuse to adopt the symlink.
        assert BaseIntegrator.is_content_identical_to_source(target, source) is False

    def test_source_is_symlink_returns_false(self, tmp_path: Path) -> None:
        body = b"identical bytes\n"
        target = tmp_path / "target"
        target.write_bytes(body)
        decoy = tmp_path / "decoy_outside"
        decoy.write_bytes(body)
        source = tmp_path / "source"
        source.symlink_to(decoy)

        assert BaseIntegrator.is_content_identical_to_source(target, source) is False

    def test_identical_regular_files_still_adopt(self, tmp_path: Path) -> None:
        """Regression trap: the symlink guard must not block the happy path."""
        body = b"identical bytes\n"
        source = tmp_path / "source"
        target = tmp_path / "target"
        source.write_bytes(body)
        target.write_bytes(body)
        assert BaseIntegrator.is_content_identical_to_source(target, source) is True


# ---------------------------------------------------------------------------
# files_adopted counter -- visibility of silent-adopt work
# ---------------------------------------------------------------------------


class TestFilesAdoptedCounter:
    """Pre-fix the adopt branch was invisible: target_paths grew but no
    counter incremented and the install summary printed nothing in
    adopt-only runs. These tests lock in the visibility contract.
    """

    def test_instruction_adopt_increments_files_adopted(self, tmp_path: Path) -> None:
        body = b"---\napplyTo: '**/*.py'\n---\n# rule\n"
        pkg_dir = tmp_path / "pkg"
        inst_dir = pkg_dir / ".apm" / "instructions"
        inst_dir.mkdir(parents=True)
        (inst_dir / "x.instructions.md").write_bytes(body)
        deploy_dir = tmp_path / ".github" / "instructions"
        deploy_dir.mkdir(parents=True)
        (deploy_dir / "x.instructions.md").write_bytes(body)

        result = InstructionIntegrator().integrate_instructions_for_target(
            KNOWN_TARGETS["copilot"],
            _make_package_info(pkg_dir),
            tmp_path,
            force=False,
            managed_files=None,
        )

        assert result.files_adopted == 1
        assert result.files_integrated == 0
        assert result.files_skipped == 0

    def test_prompt_adopt_increments_files_adopted(self, tmp_path: Path) -> None:
        body = b"---\nmode: agent\n---\n# p\n"
        pkg_dir = tmp_path / "pkg"
        src = pkg_dir / ".apm" / "prompts"
        src.mkdir(parents=True)
        (src / "p.prompt.md").write_bytes(body)
        deploy = tmp_path / ".github" / "prompts"
        deploy.mkdir(parents=True)
        (deploy / "p.prompt.md").write_bytes(body)

        result = PromptIntegrator().integrate_prompts_for_target(
            KNOWN_TARGETS["copilot"],
            _make_package_info(pkg_dir),
            tmp_path,
            force=False,
            managed_files=None,
        )
        assert result.files_adopted == 1
        assert result.files_integrated == 0

    def test_agent_adopt_increments_files_adopted(self, tmp_path: Path) -> None:
        body = b"---\nname: a\n---\n# a\n"
        pkg_dir = tmp_path / "pkg"
        src = pkg_dir / ".apm" / "agents"
        src.mkdir(parents=True)
        (src / "a.agent.md").write_bytes(body)
        deploy = tmp_path / ".github" / "agents"
        deploy.mkdir(parents=True)
        (deploy / "a.agent.md").write_bytes(body)

        result = AgentIntegrator().integrate_agents_for_target(
            KNOWN_TARGETS["copilot"],
            _make_package_info(pkg_dir),
            tmp_path,
            force=False,
            managed_files=None,
        )
        assert result.files_adopted == 1
        assert result.files_integrated == 0


# ---------------------------------------------------------------------------
# Legacy multi-target adopt: integrate_package_agents (cursor + claude)
# ---------------------------------------------------------------------------


class TestIntegratePackageAgentsAdopt:
    """The legacy ``integrate_package_agents`` auto-fans agents to
    .claude/agents/ and .cursor/agents/ when those dirs exist. Each fan
    site has its own adopt branch; pre-fix none were tested. These
    tests lock in the secondary adopt sites and the new
    ``ensure_path_within`` containment guard added at each.
    """

    def _make_pkg(self, tmp_path: Path, body: bytes) -> tuple[Path, PackageInfo]:
        pkg_dir = tmp_path / "pkg"
        src = pkg_dir / ".apm" / "agents"
        src.mkdir(parents=True)
        (src / "sec.agent.md").write_bytes(body)
        return pkg_dir, _make_package_info(pkg_dir)

    def test_claude_secondary_adopt_fires_for_byte_identical(self, tmp_path: Path) -> None:
        body = b"---\nname: sec\n---\n# sec\n"
        _pkg_dir, pkg_info = self._make_pkg(tmp_path, body)

        # Pre-create .claude/agents/ with byte-identical pre-existing file
        claude_agents = tmp_path / ".claude" / "agents"
        claude_agents.mkdir(parents=True)
        claude_target = claude_agents / "sec.md"  # claude strips .agent.md
        claude_target.write_bytes(body)

        # Need the copilot deploy dir too (primary target)
        (tmp_path / ".github" / "agents").mkdir(parents=True)

        result = AgentIntegrator().integrate_package_agents(
            pkg_info,
            tmp_path,
            force=False,
            managed_files=None,
        )

        assert claude_target in result.target_paths, (
            "Claude secondary adopt branch must fire for byte-identical pre-existing file"
        )
        assert result.files_adopted >= 1

    def test_cursor_secondary_adopt_fires_for_byte_identical(self, tmp_path: Path) -> None:
        body = b"---\nname: sec\n---\n# sec\n"
        _pkg_dir, pkg_info = self._make_pkg(tmp_path, body)

        cursor_agents = tmp_path / ".cursor" / "agents"
        cursor_agents.mkdir(parents=True)
        cursor_target = cursor_agents / "sec.md"
        cursor_target.write_bytes(body)

        (tmp_path / ".github" / "agents").mkdir(parents=True)

        result = AgentIntegrator().integrate_package_agents(
            pkg_info,
            tmp_path,
            force=False,
            managed_files=None,
        )

        assert cursor_target in result.target_paths, (
            "Cursor secondary adopt branch must fire for byte-identical pre-existing file"
        )
        assert result.files_adopted >= 1

    def test_claude_secondary_skips_user_authored_divergent(self, tmp_path: Path) -> None:
        body = b"---\nname: sec\n---\n# sec\n"
        user_body = b"# user-authored claude agent\n"
        _pkg_dir, pkg_info = self._make_pkg(tmp_path, body)

        claude_agents = tmp_path / ".claude" / "agents"
        claude_agents.mkdir(parents=True)
        claude_target = claude_agents / "sec.md"
        claude_target.write_bytes(user_body)

        (tmp_path / ".github" / "agents").mkdir(parents=True)

        AgentIntegrator().integrate_package_agents(
            pkg_info,
            tmp_path,
            force=False,
            managed_files=None,
        )

        # User content preserved -- adopt didn't fire (divergent), and
        # check_collision's force=False kept the file.
        assert claude_target.read_bytes() == user_body
