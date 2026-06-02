"""Follow-ups from #1351: mid-session apm.yml reload and --clean --watch warning.

Two behaviors that #1349 left on the table when it closed #1345:

1. ``apm compile --watch`` re-runs target resolution against the *current*
   ``apm.yml`` when ``apm.yml`` itself is the file event source -- so a
   mid-session edit to ``targets:`` takes effect on the next file event
   without restarting the watcher.  Pre-fix the startup snapshot was
   reused on every recompile, so editing ``apm.yml`` mid-watch did
   nothing until the user killed and restarted the process.

2. ``apm compile --watch --clean`` now prints an explicit warning that
   ``--clean`` is ignored in watch mode (running it on every recompile
   would surprise users by deleting orphans mid-session).  Pre-fix the
   flag was silently dropped.

Both tests are toggle-verified: reverting the fix on ``main`` makes them
fail with assertion messages that point at this PR.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from apm_cli.commands.compile.cli import compile as compile_cmd
from apm_cli.commands.compile.watcher import APMFileHandler


@pytest.fixture
def fake_logger():
    return SimpleNamespace(
        progress=MagicMock(),
        success=MagicMock(),
        error=MagicMock(),
        warning=MagicMock(),
    )


# ---------------------------------------------------------------------------
# 1) Mid-session apm.yml reload
# ---------------------------------------------------------------------------


def test_recompile_on_apm_yml_change_reresolves_against_current_file(fake_logger):
    """Editing ``apm.yml`` mid-watch must reflect on the next recompile.

    The handler is constructed with the startup snapshot ``effective_target``
    = ``"claude"`` (mimicking ``targets: [claude]`` at startup).  The user
    then edits ``apm.yml`` to ``targets: [claude, gemini]`` and the watchdog
    delivers an ``apm.yml`` modification event.  ``_recompile`` must invoke
    ``_resolve_effective_target`` (re-reading the live ``apm.yml``) and
    forward the *fresh* value -- not the snapshot -- to
    ``CompilationConfig.from_apm_yml``.
    """
    snapshot = "claude"
    fresh = frozenset({"claude", "gemini"})

    handler = APMFileHandler(
        output="AGENTS.md",
        chatmode=None,
        no_links=False,
        dry_run=False,
        logger=fake_logger,
        effective_target=snapshot,
        cli_target=None,  # apm.yml is the source of truth -- no --target flag
    )

    with (
        patch(
            "apm_cli.commands.compile.cli._resolve_effective_target",
            return_value=(fresh, "apm.yml target", ["claude", "gemini"]),
        ) as mock_resolver,
        patch(
            "apm_cli.commands.compile.watcher.CompilationConfig.from_apm_yml"
        ) as mock_from_apm_yml,
        patch("apm_cli.commands.compile.watcher.AgentsCompiler") as mock_compiler_cls,
    ):
        mock_from_apm_yml.return_value = MagicMock()
        mock_compiler_cls.return_value.compile.return_value = SimpleNamespace(
            success=True, output_path="AGENTS.md", errors=[]
        )

        handler._recompile("apm.yml")

    assert mock_resolver.call_count == 1, (
        "When apm.yml changes, _recompile must call _resolve_effective_target "
        "to pick up mid-session targets: edits."
    )
    assert mock_from_apm_yml.call_args.kwargs["target"] == fresh, (
        "Watcher forwarded the startup snapshot instead of the fresh "
        "resolver output -- mid-session apm.yml edits will not take effect."
    )


def test_recompile_on_instruction_file_change_uses_snapshot(fake_logger):
    """Non-apm.yml events keep using the startup snapshot (no extra resolver work).

    Re-running the resolver on every ``.instructions.md`` edit would do
    nothing useful (those files cannot affect ``target:`` resolution) and
    would re-read ``apm.yml`` on every keystroke-triggered recompile.
    Scope the re-resolution to the file that can change the answer.
    """
    snapshot = "claude"
    handler = APMFileHandler(
        output="AGENTS.md",
        chatmode=None,
        no_links=False,
        dry_run=False,
        logger=fake_logger,
        effective_target=snapshot,
        cli_target=None,
    )

    with (
        patch("apm_cli.commands.compile.cli._resolve_effective_target") as mock_resolver,
        patch(
            "apm_cli.commands.compile.watcher.CompilationConfig.from_apm_yml"
        ) as mock_from_apm_yml,
        patch("apm_cli.commands.compile.watcher.AgentsCompiler") as mock_compiler_cls,
    ):
        mock_from_apm_yml.return_value = MagicMock()
        mock_compiler_cls.return_value.compile.return_value = SimpleNamespace(
            success=True, output_path="AGENTS.md", errors=[]
        )

        handler._recompile(".apm/instructions/style.instructions.md")

    assert mock_resolver.call_count == 0, (
        "Resolver should only re-run when apm.yml itself triggers the recompile."
    )
    assert mock_from_apm_yml.call_args.kwargs["target"] == snapshot


def test_apm_yml_change_persists_fresh_target_for_subsequent_events(fake_logger):
    """After an apm.yml-driven re-resolve, the fresh target must persist.

    Without persistence, the sequence ``apm.yml edit -> instructions edit``
    looks like this: the apm.yml event correctly emits the new family set,
    but the *next* instructions event uses the original startup snapshot
    again and silently reverts to the wrong family set.  Outputs written
    by the apm.yml-event recompile become stale and the user sees an
    inconsistent state with no error.

    This test toggles the failure mode directly: the apm.yml event flips
    the snapshot from ``"claude"`` to ``frozenset({"claude", "gemini"})``;
    the immediately-following instructions event must reuse the new
    value, not the original.
    """
    initial_snapshot = "claude"
    fresh = frozenset({"claude", "gemini"})

    handler = APMFileHandler(
        output="AGENTS.md",
        chatmode=None,
        no_links=False,
        dry_run=False,
        logger=fake_logger,
        effective_target=initial_snapshot,
        cli_target=None,
    )

    with (
        patch(
            "apm_cli.commands.compile.cli._resolve_effective_target",
            return_value=(fresh, "apm.yml target", ["claude", "gemini"]),
        ),
        patch(
            "apm_cli.commands.compile.watcher.CompilationConfig.from_apm_yml"
        ) as mock_from_apm_yml,
        patch("apm_cli.commands.compile.watcher.AgentsCompiler") as mock_compiler_cls,
    ):
        mock_from_apm_yml.return_value = MagicMock()
        mock_compiler_cls.return_value.compile.return_value = SimpleNamespace(
            success=True, output_path="AGENTS.md", errors=[]
        )

        # 1. apm.yml edit triggers re-resolve with the fresh value.
        handler._recompile("apm.yml")
        # 2. Subsequent instructions edit must reuse the fresh value,
        #    NOT revert to the initial snapshot.
        handler._recompile(".apm/instructions/style.instructions.md")

    assert mock_from_apm_yml.call_count == 2
    first_target = mock_from_apm_yml.call_args_list[0].kwargs["target"]
    second_target = mock_from_apm_yml.call_args_list[1].kwargs["target"]
    assert first_target == fresh, "First recompile (apm.yml event) must use the fresh value."
    assert second_target == fresh, (
        "Second recompile (instructions event) must reuse the fresh value persisted "
        "from the prior apm.yml event; reverting to the startup snapshot leaves "
        "AGENTS.md / GEMINI.md stale until the next apm.yml edit."
    )
    assert handler.effective_target == fresh, (
        "self.effective_target must be updated in-place after re-resolution."
    )


def test_recompile_on_lookalike_filename_does_not_reresolve(fake_logger):
    """A file named ``backup_apm.yml`` must NOT trigger re-resolution.

    Pre-fix the gate was ``changed_file.endswith(APM_YML_FILENAME)`` which
    spuriously matches any path ending in the seven characters ``apm.yml``
    (``backup_apm.yml``, ``.apm/configs/legacy_apm.yml``, etc.).  Such a
    file would silently re-read the project root ``apm.yml`` and replace
    the startup snapshot, which is wrong: those files are not the
    project's resolution input.  The basename match pins the gate to
    the exact filename so look-alikes use the snapshot path.
    """
    snapshot = "claude"
    handler = APMFileHandler(
        output="AGENTS.md",
        chatmode=None,
        no_links=False,
        dry_run=False,
        logger=fake_logger,
        effective_target=snapshot,
        cli_target=None,
    )

    with (
        patch("apm_cli.commands.compile.cli._resolve_effective_target") as mock_resolver,
        patch(
            "apm_cli.commands.compile.watcher.CompilationConfig.from_apm_yml"
        ) as mock_from_apm_yml,
        patch("apm_cli.commands.compile.watcher.AgentsCompiler") as mock_compiler_cls,
    ):
        mock_from_apm_yml.return_value = MagicMock()
        mock_compiler_cls.return_value.compile.return_value = SimpleNamespace(
            success=True, output_path="AGENTS.md", errors=[]
        )

        # All three end with "apm.yml" but none are the project root file.
        for lookalike in (
            "backup_apm.yml",
            ".apm/configs/legacy_apm.yml",
            "vendor/some-apm.yml",
        ):
            handler._recompile(lookalike)

    assert mock_resolver.call_count == 0, (
        "Re-resolution must be scoped to the exact ``apm.yml`` filename; "
        "an ``endswith`` gate would falsely trigger on look-alike paths."
    )
    # All three recompiles forwarded the snapshot, not a resolver result.
    for call in mock_from_apm_yml.call_args_list:
        assert call.kwargs["target"] == snapshot


def test_recompile_on_apm_yml_change_with_cli_target_keeps_cli_priority(fake_logger):
    """Explicit ``--target`` on the CLI wins over mid-session apm.yml edits.

    If the user launched watch mode with ``--target claude``, editing
    ``apm.yml``'s ``targets:`` mid-session should *not* override the CLI
    flag -- that matches the one-shot path's priority order.  The
    re-resolver receives the original CLI target and returns ``"claude"``
    again because ``--target`` outranks ``apm.yml`` in
    ``_resolve_effective_target``.
    """
    cli_target = "claude"
    handler = APMFileHandler(
        output="AGENTS.md",
        chatmode=None,
        no_links=False,
        dry_run=False,
        logger=fake_logger,
        effective_target="claude",
        cli_target=cli_target,
    )

    with (
        patch(
            "apm_cli.commands.compile.cli._resolve_effective_target",
            return_value=("claude", "explicit --target flag", ["claude", "gemini"]),
        ) as mock_resolver,
        patch(
            "apm_cli.commands.compile.watcher.CompilationConfig.from_apm_yml"
        ) as mock_from_apm_yml,
        patch("apm_cli.commands.compile.watcher.AgentsCompiler") as mock_compiler_cls,
    ):
        mock_from_apm_yml.return_value = MagicMock()
        mock_compiler_cls.return_value.compile.return_value = SimpleNamespace(
            success=True, output_path="AGENTS.md", errors=[]
        )

        handler._recompile("apm.yml")

    # Resolver is called with the original CLI target so it can enforce
    # the explicit-flag-beats-config-file priority order.
    assert mock_resolver.call_args.args == (cli_target,)
    assert mock_from_apm_yml.call_args.kwargs["target"] == "claude"


# ---------------------------------------------------------------------------
# 2) --clean --watch warning
# ---------------------------------------------------------------------------


def _write_minimal_apm_project(tmp_path):
    (tmp_path / "apm.yml").write_text(
        "name: Repro\nversion: 1.0.0\ntargets:\n- claude\n", encoding="utf-8"
    )
    instructions = tmp_path / ".apm" / "instructions"
    instructions.mkdir(parents=True)
    (instructions / "style.instructions.md").write_text(
        '---\ndescription: style\napplyTo: "**/*.py"\n---\n\nsnake_case.\n',
        encoding="utf-8",
    )


def test_clean_watch_emits_warning_and_does_not_run_clean(tmp_path, monkeypatch):
    """``apm compile --watch --clean`` must warn and proceed without --clean.

    Pre-fix ``--clean`` was silently swallowed on the watch path: there
    was no kwarg to forward it into ``_watch_mode``, so the user got no
    cleanup AND no signal that the flag was ignored.  This pins both
    halves: the warning fires AND the watcher still launches.
    """
    monkeypatch.chdir(tmp_path)
    _write_minimal_apm_project(tmp_path)

    with patch("apm_cli.commands.compile.cli._watch_mode") as mock_watch:
        runner = CliRunner()
        result = runner.invoke(compile_cmd, ["--watch", "--clean"])

    assert result.exit_code == 0, f"compile exited with {result.exit_code}: {result.output}"
    assert "--clean is ignored in watch mode" in result.output, (
        "Users running `apm compile --watch --clean` must see an explicit "
        "warning -- silently dropping the flag is what this PR fixes."
    )
    # Critical: the watcher *still* launched.  Warning is informational,
    # not a fatal error.
    assert mock_watch.call_count == 1


def test_watch_without_clean_does_not_emit_clean_warning(tmp_path, monkeypatch):
    """Positive control: warning must not appear when ``--clean`` is absent."""
    monkeypatch.chdir(tmp_path)
    _write_minimal_apm_project(tmp_path)

    with patch("apm_cli.commands.compile.cli._watch_mode"):
        runner = CliRunner()
        result = runner.invoke(compile_cmd, ["--watch"])

    assert result.exit_code == 0, f"compile exited with {result.exit_code}: {result.output}"
    assert "--clean is ignored in watch mode" not in result.output
