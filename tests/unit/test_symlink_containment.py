"""Tests for symlink containment enforcement across APM subsystems.

Validates that symlinked primitive files are rejected at discovery and
resolution time, preventing arbitrary local file reads.
"""

import json
import os
import tempfile
import shutil
import unittest
from pathlib import Path


def _try_symlink(link: Path, target: Path):
    """Create a symlink or skip the test on platforms that don't support it."""
    try:
        link.symlink_to(target)
    except OSError:
        raise unittest.SkipTest("Symlinks not supported on this platform")


class TestPromptCompilerSymlinkContainment(unittest.TestCase):
    """PromptCompiler._resolve_prompt_file rejects external symlinks."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.project = Path(self.tmpdir) / "project"
        self.project.mkdir()
        self.outside = Path(self.tmpdir) / "outside"
        self.outside.mkdir()
        # Create a file outside the project
        self.secret = self.outside / "secret.txt"
        self.secret.write_text("sensitive-data", encoding="utf-8")
        # Create apm.yml so the project is valid
        (self.project / "apm.yml").write_text(
            "name: test\nversion: 1.0.0\n", encoding="utf-8"
        )

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_symlinked_prompt_outside_project_rejected(self):
        """Symlinked .prompt.md is rejected with clear error message."""
        from apm_cli.core.script_runner import PromptCompiler

        prompts_dir = self.project / ".apm" / "prompts"
        prompts_dir.mkdir(parents=True)
        symlink = prompts_dir / "evil.prompt.md"
        _try_symlink(symlink, self.secret)

        compiler = PromptCompiler()
        old_cwd = os.getcwd()
        try:
            os.chdir(self.project)
            with self.assertRaises(FileNotFoundError) as ctx:
                compiler._resolve_prompt_file(".apm/prompts/evil.prompt.md")
            self.assertIn("symlink", str(ctx.exception).lower())
        finally:
            os.chdir(old_cwd)

    def test_normal_prompt_within_project_allowed(self):
        """Non-symlinked prompt files within the project are allowed."""
        from apm_cli.core.script_runner import PromptCompiler

        prompts_dir = self.project / ".apm" / "prompts"
        prompts_dir.mkdir(parents=True)
        prompt = prompts_dir / "safe.prompt.md"
        prompt.write_text("# Safe prompt", encoding="utf-8")

        compiler = PromptCompiler()
        old_cwd = os.getcwd()
        try:
            os.chdir(self.project)
            result = compiler._resolve_prompt_file(".apm/prompts/safe.prompt.md")
            self.assertTrue(result.exists())
        finally:
            os.chdir(old_cwd)


class TestPrimitiveDiscoverySymlinkContainment(unittest.TestCase):
    """find_primitive_files rejects symlinks outside base directory."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.project = Path(self.tmpdir) / "project"
        self.project.mkdir()
        self.outside = Path(self.tmpdir) / "outside"
        self.outside.mkdir()
        self.secret = self.outside / "leak.instructions.md"
        self.secret.write_text("---\napplyTo: '**'\n---\nLeaked!", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_symlinked_instruction_outside_base_rejected(self):
        """Symlinked .instructions.md outside base_dir is filtered out."""
        from apm_cli.primitives.discovery import find_primitive_files

        instructions_dir = self.project / ".github" / "instructions"
        instructions_dir.mkdir(parents=True)
        symlink = instructions_dir / "evil.instructions.md"
        _try_symlink(symlink, self.secret)

        # Also add a normal file
        normal = instructions_dir / "safe.instructions.md"
        normal.write_text("---\napplyTo: '**'\n---\nSafe", encoding="utf-8")

        results = find_primitive_files(
            str(self.project),
            [".github/instructions/*.instructions.md"],
        )
        names = [f.name for f in results]
        self.assertIn("safe.instructions.md", names)
        self.assertNotIn("evil.instructions.md", names)


class TestBaseIntegratorSymlinkContainment(unittest.TestCase):
    """BaseIntegrator.find_files_by_glob rejects external symlinks."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.pkg = Path(self.tmpdir) / "pkg"
        self.pkg.mkdir()
        self.outside = Path(self.tmpdir) / "outside"
        self.outside.mkdir()
        self.secret = self.outside / "leak.agent.md"
        self.secret.write_text("# Leaked agent", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_symlinked_agent_outside_package_rejected(self):
        """Symlinked .agent.md outside package dir is filtered out."""
        from apm_cli.integration.base_integrator import BaseIntegrator

        agents_dir = self.pkg / ".apm" / "agents"
        agents_dir.mkdir(parents=True)
        symlink = agents_dir / "evil.agent.md"
        _try_symlink(symlink, self.secret)

        normal = agents_dir / "safe.agent.md"
        normal.write_text("# Safe agent", encoding="utf-8")

        results = BaseIntegrator.find_files_by_glob(
            self.pkg, "*.agent.md", subdirs=[".apm/agents"],
        )
        names = [f.name for f in results]
        self.assertIn("safe.agent.md", names)
        self.assertNotIn("evil.agent.md", names)


class TestHookIntegratorSymlinkContainment(unittest.TestCase):
    """HookIntegrator.find_hook_files rejects external symlinks."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.pkg = Path(self.tmpdir) / "pkg"
        self.pkg.mkdir()
        self.outside = Path(self.tmpdir) / "outside"
        self.outside.mkdir()
        self.secret = self.outside / "evil.json"
        self.secret.write_text(json.dumps({"hooks": {}}), encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_symlinked_hook_json_outside_package_rejected(self):
        """Symlinked hook JSON outside package dir is filtered out."""
        from apm_cli.integration.hook_integrator import HookIntegrator

        hooks_dir = self.pkg / ".apm" / "hooks"
        hooks_dir.mkdir(parents=True)
        symlink = hooks_dir / "evil.json"
        _try_symlink(symlink, self.secret)

        normal = hooks_dir / "safe.json"
        normal.write_text(json.dumps({"hooks": {}}), encoding="utf-8")

        integrator = HookIntegrator()
        results = integrator.find_hook_files(self.pkg)
        names = [f.name for f in results]
        self.assertIn("safe.json", names)
        self.assertNotIn("evil.json", names)


class TestSkillIntegratorCopytreeSymlinkContainment(unittest.TestCase):
    """skill_integrator copytree paths drop symlinks via security gate.

    Three copytree call sites in
    `apm_cli.integration.skill_integrator` deploy a skill bundle into
    target directories. Any of them must drop symlinks contained in
    the source tree -- otherwise a malicious package could ship a
    symlink pointing at `/etc/passwd` (or any path outside the
    package) and have it materialised inside the target's runtime
    directory after `apm install`.

    These tests do not exercise the higher-level integrator pipeline;
    they validate the contract by importing
    `apm_cli.security.gate.ignore_symlinks` and confirming each call
    site invokes ``shutil.copytree`` with that callback (or a
    composition that includes it).
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.src = Path(self.tmpdir) / "src"
        self.src.mkdir()
        self.outside = Path(self.tmpdir) / "outside"
        self.outside.mkdir()
        self.secret = self.outside / "secret.txt"
        self.secret.write_text("sensitive", encoding="utf-8")
        self.dest = Path(self.tmpdir) / "dest"

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _build_source_with_symlink(self):
        """Create a source dir with a real file plus a symlink to outside."""
        (self.src / "real.md").write_text("real", encoding="utf-8")
        nested = self.src / "agents"
        nested.mkdir()
        (nested / "real.agent.md").write_text("real", encoding="utf-8")
        link = self.src / "evil.txt"
        _try_symlink(link, self.secret)
        nested_link = nested / "evil-nested.txt"
        _try_symlink(nested_link, self.secret)

    def test_ignore_symlinks_callback_excludes_top_level_and_nested(self):
        """The shared ignore_symlinks callback drops symlinks at every depth."""
        from apm_cli.security.gate import ignore_symlinks

        self._build_source_with_symlink()
        shutil.copytree(self.src, self.dest, ignore=ignore_symlinks)

        copied = sorted(p.relative_to(self.dest).as_posix() for p in self.dest.rglob("*"))
        self.assertIn("real.md", copied)
        self.assertIn("agents", copied)
        self.assertIn("agents/real.agent.md", copied)
        self.assertNotIn("evil.txt", copied)
        self.assertNotIn("agents/evil-nested.txt", copied)

    def test_skill_integrator_native_skill_copytree_uses_ignore_symlinks(self):
        """integrate_native_skills passes ignore_symlinks to shutil.copytree.

        Source-level guard: if a future refactor drops the callback,
        this test fails before any malicious package can exploit it.
        """
        import inspect
        from apm_cli.integration import skill_integrator

        source = inspect.getsource(skill_integrator)
        # All three copytree calls in skill_integrator.py must reference
        # ignore_symlinks (directly or via a composing helper).
        copytree_count = source.count("shutil.copytree(")
        ignore_symlinks_refs = source.count("ignore_symlinks")
        self.assertGreaterEqual(
            copytree_count,
            3,
            f"Expected >=3 copytree calls in skill_integrator, found {copytree_count}",
        )
        # Each copytree must be matched by at least one ignore_symlinks
        # reference (the helper at line 818 composes one ignore_symlinks
        # import + one usage inside a closure -- still >=copytree_count).
        self.assertGreaterEqual(
            ignore_symlinks_refs,
            copytree_count,
            f"Expected >={copytree_count} ignore_symlinks references "
            f"(one per copytree); found {ignore_symlinks_refs}",
        )


if __name__ == "__main__":
    unittest.main()
