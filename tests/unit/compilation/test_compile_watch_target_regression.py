"""Regression tests for #1345: `apm compile --watch` must honor target inputs.

Before the fix, `_watch_mode` rebuilt CompilationConfig without going
through target resolution, so apm.yml `targets:` and CLI `--target` were
silently ignored under --watch.  Initial-emission and every recompile
fell back to `target="all"` (the CompilationConfig default), generating
GEMINI.md regardless of what the user asked for.

These tests lock the structural fix:
- `_run_compile_once` (the shared body) honors apm.yml `targets:`.
- `_watch_mode` delegates to `_run_compile_once` instead of building
  CompilationConfig itself.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest


def _setup_project(root: Path, *, targets_yaml_block: str) -> None:
    """Write a minimal APM project with the given `targets:` block."""
    (root / "apm.yml").write_text(
        f"name: test-1345\nversion: 1.0.0\n{targets_yaml_block}",
        encoding="utf-8",
    )
    instructions = root / ".apm" / "instructions"
    instructions.mkdir(parents=True)
    (instructions / "sample.instructions.md").write_text(
        '---\ndescription: regression fixture\napplyTo: "**/*.md"\n---\n\nbody\n',
        encoding="utf-8",
    )


class TestRunCompileOnceRespectsApmYmlTargets:
    """The shared compile body must read apm.yml `targets:` correctly.

    This is the contract the watch path now depends on.  Before #1345 the
    watcher bypassed this path entirely; if a future change makes the
    shared body stop honoring `targets:`, both surfaces regress.
    """

    def test_apm_yml_targets_excluding_gemini_does_not_emit_gemini_md(self, tmp_path, monkeypatch):
        _setup_project(
            tmp_path,
            targets_yaml_block="targets:\n- claude\n- cursor\n",
        )
        monkeypatch.chdir(tmp_path)

        from apm_cli.commands.compile.cli import _run_compile_once
        from apm_cli.core.command_logger import CommandLogger

        _run_compile_once(
            target=None,
            output="AGENTS.md",
            chatmode=None,
            no_links=False,
            dry_run=False,
            single_agents=False,
            verbose=False,
            local_only=False,
            clean=False,
            with_constitution=True,
            logger=CommandLogger("test", verbose=False, dry_run=False),
        )

        assert (tmp_path / "AGENTS.md").exists(), "cursor family emits AGENTS.md"
        assert (tmp_path / "CLAUDE.md").exists(), "claude family emits CLAUDE.md"
        assert not (tmp_path / "GEMINI.md").exists(), (
            "GEMINI.md must not be emitted when apm.yml targets: excludes gemini (#1345)"
        )

    def test_cli_target_claude_does_not_emit_gemini_md(self, tmp_path, monkeypatch):
        """CLI `--target` must override apm.yml; if apm.yml asks for gemini
        but --target says claude only, no GEMINI.md."""
        _setup_project(
            tmp_path,
            targets_yaml_block="targets:\n- claude\n- gemini\n",
        )
        monkeypatch.chdir(tmp_path)

        from apm_cli.commands.compile.cli import _run_compile_once
        from apm_cli.core.command_logger import CommandLogger

        _run_compile_once(
            target="claude",  # CLI -t claude
            output="AGENTS.md",
            chatmode=None,
            no_links=False,
            dry_run=False,
            single_agents=False,
            verbose=False,
            local_only=False,
            clean=False,
            with_constitution=True,
            logger=CommandLogger("test", verbose=False, dry_run=False),
        )

        assert (tmp_path / "CLAUDE.md").exists()
        assert not (tmp_path / "GEMINI.md").exists(), (
            "CLI --target claude must override apm.yml `targets:` (#1345)"
        )

    def test_apm_yml_targets_including_gemini_does_emit_gemini_md(self, tmp_path, monkeypatch):
        """Companion assertion: when `gemini` IS in targets, GEMINI.md
        must be emitted.  Without this, the "exclude" assertion above is
        a tautology (it would also pass if `_run_compile_once` were
        completely broken and never produced GEMINI.md)."""
        _setup_project(
            tmp_path,
            targets_yaml_block="targets:\n- claude\n- gemini\n",
        )
        monkeypatch.chdir(tmp_path)

        from apm_cli.commands.compile.cli import _run_compile_once
        from apm_cli.core.command_logger import CommandLogger

        _run_compile_once(
            target=None,
            output="AGENTS.md",
            chatmode=None,
            no_links=False,
            dry_run=False,
            single_agents=False,
            verbose=False,
            local_only=False,
            clean=False,
            with_constitution=True,
            logger=CommandLogger("test", verbose=False, dry_run=False),
        )

        assert (tmp_path / "GEMINI.md").exists(), (
            "GEMINI.md must be emitted when apm.yml targets: includes gemini"
        )


class TestWatchModeDelegatesToSharedCompileBody:
    """Structural guard: `_watch_mode` must route through `_run_compile_once`.

    The whole point of the #1345 fix is to eliminate the parallel compile
    path inside the watcher.  If somebody re-introduces a direct
    `CompilationConfig.from_apm_yml(...)` call in watcher.py, target
    resolution will silently drift again -- this test catches that
    structurally so the regression can't sneak in via a future refactor.
    """

    def test_watcher_imports_run_compile_once(self):
        from apm_cli.commands.compile import watcher

        source = inspect.getsource(watcher._watch_mode)
        assert "_run_compile_once" in source, (
            "Watcher must delegate to _run_compile_once (cli.py).  See "
            "#1345 for why the parallel CompilationConfig path was "
            "removed."
        )

    def test_watcher_does_not_build_compilation_config_directly(self):
        """Forbid `CompilationConfig.from_apm_yml(...)` in watcher.py.

        That call without target resolution is precisely the #1345 bug.
        The shared `_run_compile_once` is the only sanctioned config
        builder for the compile surface.
        """
        from apm_cli.commands.compile import watcher

        source = inspect.getsource(watcher)
        assert "CompilationConfig.from_apm_yml" not in source, (
            "Watcher must not build CompilationConfig directly -- that "
            "was the #1345 bug.  Route compile work through "
            "_run_compile_once instead."
        )

    def test_watcher_does_not_instantiate_compiler_directly(self):
        """Forbid `AgentsCompiler(...)` instantiation in watcher.py.

        Same reasoning as the CompilationConfig check: the compiler must
        only be driven from the shared body so target resolution applies
        uniformly.
        """
        from apm_cli.commands.compile import watcher

        source = inspect.getsource(watcher)
        assert "AgentsCompiler(" not in source, (
            "Watcher must not call AgentsCompiler directly -- route "
            "through _run_compile_once instead (#1345)."
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
