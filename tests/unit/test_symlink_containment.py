"""Tests for symlink containment enforcement across APM subsystems.

Validates that symlinked primitive files are rejected at discovery and
resolution time, preventing arbitrary local file reads.
"""

import json
import os
import shutil
import tempfile
import typing
import unittest
from pathlib import Path


def _try_symlink(link: Path, target: Path):
    """Create a symlink or skip the test on platforms that don't support it."""
    try:
        link.symlink_to(target)
    except OSError:
        raise unittest.SkipTest("Symlinks not supported on this platform")  # noqa: B904


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
        (self.project / "apm.yml").write_text("name: test\nversion: 1.0.0\n", encoding="utf-8")

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
            self.pkg,
            "*.agent.md",
            subdirs=[".apm/agents"],
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

    def test_skill_integrator_native_skill_copytree_uses_deployable_copy_filter(self):
        """Every skill copytree uses the canonical deployable copy filter.

        The filter composes ``ignore_non_content`` through
        ``build_copy_ignore`` with ``DeployableSourcePlan.copy_ignore``. This
        keeps cache markers and symlinks out while also rejecting any
        source-only content that the deployment plan did not authorize.
        """
        import inspect

        from apm_cli.integration import skill_integrator

        source = inspect.getsource(skill_integrator)
        copytree_count = source.count("shutil.copytree(")
        self.assertGreaterEqual(
            copytree_count,
            3,
            f"Expected >=3 copytree calls in skill_integrator, found {copytree_count}",
        )
        self.assertGreaterEqual(
            source.count("ignore=_build_deployable_copy_ignore("),
            copytree_count,
            "Every skill copytree must use the canonical deployable copy filter",
        )
        self.assertIn("base_ignore = build_copy_ignore(", source)
        self.assertIn("source_plan.copy_ignore(directory, contents)", source)


class TestIgnoreNonContentSourceGuard(unittest.TestCase):
    """Source-level guard: every copytree site that fans content out of
    apm_modules/ to deploy targets must use ``ignore_non_content`` so
    ``.apm-pin`` and symlinks cannot leak into the deploy tree.

    Companion to ``test_skill_integrator_native_skill_copytree_uses_ignore_non_content``
    which covers ``skill_integrator.py``; this class covers the remaining
    three modules (plugin_parser, packer, unpacker).
    """

    _MODULES: typing.ClassVar[list[tuple[str, str]]] = [
        ("apm_cli.deps", "plugin_parser"),
        ("apm_cli.bundle", "packer"),
        ("apm_cli.bundle", "unpacker"),
    ]

    def test_copytree_sites_use_ignore_non_content(self):
        """Every copytree call that copies into a deploy target must pass
        ``ignore_non_content`` (or a composing helper that wraps it).

        Source-level guard: if a future refactor drops the callback at any
        site, this test fails before a stale or malicious ``.apm-pin`` can
        leak into the deploy target.

        Asserted per call site via AST rather than by counting substrings: a
        hand-maintained occurrence floor silently loosens whenever call sites
        are merged or split, and it cannot tell a real argument from the same
        identifier mentioned in a docstring.
        """
        import ast
        import importlib
        import inspect

        def _is_shutil_copytree(node: ast.AST) -> bool:
            """Match ``shutil.copytree(...)`` and a bare imported ``copytree``.

            Deliberately not "any attribute named copytree": an unrelated
            helper exposing that name is not what this guard is about, and
            demanding ``ignore=`` from it would be a false failure.
            """
            if not isinstance(node, ast.Call):
                return False
            func = node.func
            if isinstance(func, ast.Attribute):
                return func.attr == "copytree" and (
                    isinstance(func.value, ast.Name) and func.value.id == "shutil"
                )
            return isinstance(func, ast.Name) and func.id == "copytree"

        def _guarded(node: ast.Call) -> bool:
            """True when the call's ``ignore=`` names the content filter.

            The argument's whole subtree is searched, so a composing helper
            (``ignore=_wrap(ignore_non_content)``) still counts while
            ``ignore=None`` or an unrelated callback does not. Presence of
            the keyword alone is not enough -- that was the hole the earlier
            occurrence count happened to cover.
            """
            for kw in node.keywords:
                if kw.arg != "ignore":
                    continue
                return any(
                    isinstance(sub, ast.Name) and sub.id == "ignore_non_content"
                    for sub in ast.walk(kw.value)
                )
            return False

        for pkg, mod_name in self._MODULES:
            with self.subTest(module=f"{pkg}.{mod_name}"):
                mod = importlib.import_module(f"{pkg}.{mod_name}")
                tree = ast.parse(inspect.getsource(mod))

                sites = [node for node in ast.walk(tree) if _is_shutil_copytree(node)]
                self.assertTrue(
                    sites,
                    f"{pkg}.{mod_name}: no copytree call sites found -- the guard "
                    "is no longer watching anything; retarget or remove it",
                )

                unguarded = [node.lineno for node in sites if not _guarded(node)]
                self.assertEqual(
                    unguarded,
                    [],
                    f"{pkg}.{mod_name}: copytree at line(s) {unguarded} do not pass "
                    "ignore=ignore_non_content -- .apm-pin and symlinks can leak "
                    "into the deploy tree",
                )


if __name__ == "__main__":
    unittest.main()
