"""Regression tests for hand-authored project root context files."""

from pathlib import Path
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.commands.compile.cli import (
    _report_distributed_live_success,
    _report_protected_no_write,
)
from apm_cli.commands.compile.watcher import APMFileHandler, _report_retained_watch_outputs
from apm_cli.compilation.agents_compiler import AgentsCompiler
from apm_cli.compilation.claude_formatter import CLAUDE_HEADER
from apm_cli.compilation.constants import AGENTS_MD_GENERATED_MARKER
from apm_cli.compilation.distributed_compiler import (
    AGENTS_MD_GENERATED_MARKER as DISTRIBUTED_AGENTS_MARKER,
)
from apm_cli.compilation.distributed_compiler import DistributedAgentsCompiler

pytestmark = pytest.mark.component

_MANUAL_CLAUDE = "# CLAUDE.md\n\nHand-authored project memory.\n"
_MANUAL_AGENTS = "# AGENTS.md\n\nHand-authored agent guidance.\n"


def _seed_project(project: Path) -> None:
    """Create a compilable project with hand-authored root context files."""
    (project / "apm.yml").write_text(
        "name: root-context-protection\n"
        "version: 1.0.0\n"
        "targets: [claude, codex]\n"
        "dependencies:\n"
        "  apm: []\n"
        "  mcp: []\n",
        encoding="utf-8",
    )
    instructions = project / ".apm" / "instructions"
    instructions.mkdir(parents=True)
    (instructions / "standards.instructions.md").write_text(
        "---\ndescription: standards\n---\n# Standards\nUse US English.\n",
        encoding="utf-8",
    )
    (project / "CLAUDE.md").write_text(_MANUAL_CLAUDE, encoding="utf-8")
    (project / "AGENTS.md").write_text(_MANUAL_AGENTS, encoding="utf-8")


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("single_agents", [False, True])
def test_compile_preserves_hand_authored_project_root_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dry_run: bool,
    single_agents: bool,
) -> None:
    """Distributed and single-file compiles must protect hand-authored roots."""
    _seed_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    args = ["compile"]
    if single_agents:
        args.append("--single-agents")
    if dry_run:
        args.append("--dry-run")

    result = CliRunner().invoke(cli, args, catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8") == _MANUAL_CLAUDE
    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") == _MANUAL_AGENTS
    assert result.output.count("hand-authored file will not be overwritten") == 2
    assert "Protected CLAUDE.md" in result.output
    assert "Protected AGENTS.md" in result.output
    assert "produced no output files" not in result.output
    if dry_run and single_agents:
        assert "would be retained" in " ".join(result.output.split())
        assert "Generated Content Preview" not in result.output
    if dry_run and not single_agents:
        assert "Would retain 2 hand-authored root files" in " ".join(result.output.split())


def test_partial_compile_reports_generated_and_retained_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live distributed output must lead with both generated and retained counts."""
    _seed_project(tmp_path)
    (tmp_path / "CLAUDE.md").unlink()
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli, ["compile"])

    assert result.exit_code == 0, result.output
    assert "retained 1 hand-authored root file" in " ".join(result.output.split())


def test_watch_reporting_surfaces_retained_outputs_and_warnings() -> None:
    """Watch mode must not hide a protected-root result."""
    logger = Mock()
    result = Mock(
        warnings=["Protected AGENTS.md: hand-authored file will not be overwritten."],
        stats={
            "agents_files_generated": 1,
            "root_context_files_protected": 1,
        },
    )

    retained = _report_retained_watch_outputs(logger, result, dry_run=False)

    assert retained
    logger.warning.assert_called_once()
    assert "retained 1 hand-authored root file" in logger.progress.call_args.args[0]
    assert "generated 1 other output file" in logger.progress.call_args.args[0]


def test_watch_dry_run_reports_generated_and_retained_outputs() -> None:
    """Watch dry-run reporting must describe every planned output outcome."""
    logger = Mock()
    result = Mock(
        warnings=[],
        stats={
            "agents_files_generated": 2,
            "root_context_files_protected": 1,
        },
    )

    retained = _report_retained_watch_outputs(logger, result, dry_run=True)

    assert retained
    message = logger.progress.call_args.args[0]
    assert "Would generate 2 other output files" in message
    assert "retain 1 hand-authored root file" in message


def test_watch_recompile_retains_hand_authored_root_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real watcher-to-compiler path must surface retained roots."""
    _seed_project(tmp_path)
    logger = Mock()
    monkeypatch.chdir(tmp_path)
    handler = APMFileHandler(
        output="AGENTS.md",
        chatmode=None,
        no_links=False,
        dry_run=False,
        logger=logger,
    )

    handler._recompile(str(tmp_path / ".apm" / "instructions" / "standards.instructions.md"))

    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") == _MANUAL_AGENTS
    warning_text = " ".join(call.args[0] for call in logger.warning.call_args_list)
    assert "Protected AGENTS.md" in warning_text
    progress_text = " ".join(call.args[0] for call in logger.progress.call_args_list)
    assert "retained" in progress_text
    assert "generated 2 other output files" in progress_text


def test_mixed_outcome_reports_retained_and_nested_skipped_outputs() -> None:
    """A mixed summary must not hide either non-write outcome."""
    logger = Mock()

    _report_distributed_live_success(
        logger,
        {
            "nested_git_placements_skipped": 1,
            "root_context_files_protected": 1,
        },
        [],
        files_written=1,
        agents_generated=1,
    )

    message = logger.success.call_args.args[0]
    assert "retained 1 hand-authored root file" in message
    assert "skipped 1 nested Git repository placement" in message


def test_no_write_outcome_reports_retained_and_nested_skipped_outputs() -> None:
    """A zero-write summary must retain all skip information."""
    logger = Mock()

    _report_protected_no_write(
        logger,
        {
            "nested_git_placements_skipped": 1,
            "root_context_files_protected": 2,
        },
        [],
    )

    message = logger.progress.call_args.args[0]
    assert "Retained 2 hand-authored root files" in message
    assert "skipped 1 nested Git repository placement" in message


def test_compile_root_preserves_hand_authored_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A redirected compile root must receive the same overwrite protection."""
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    _seed_project(source)
    (destination / "CLAUDE.md").write_text(_MANUAL_CLAUDE, encoding="utf-8")
    monkeypatch.chdir(source)

    result = CliRunner().invoke(
        cli,
        ["compile", "--target", "claude", "--root", str(destination)],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert (destination / "CLAUDE.md").read_text(encoding="utf-8") == _MANUAL_CLAUDE
    assert "Protected CLAUDE.md" in result.output


def test_compile_root_preserves_hand_authored_agents_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A redirected compile root must also protect hand-authored AGENTS.md."""
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    _seed_project(source)
    (destination / "AGENTS.md").write_text(_MANUAL_AGENTS, encoding="utf-8")
    monkeypatch.chdir(source)

    result = CliRunner().invoke(
        cli,
        ["compile", "--target", "codex", "--root", str(destination)],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert (destination / "AGENTS.md").read_text(encoding="utf-8") == _MANUAL_AGENTS
    assert "Protected AGENTS.md" in result.output


def test_single_file_managed_section_updates_hand_authored_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Managed-section mode must update only the explicitly owned block."""
    _seed_project(tmp_path)
    (tmp_path / "AGENTS.md").write_text(
        "# Team guidance\n\n"
        "Human-authored content.\n\n"
        "<!-- apm:start -->\nOld APM block.\n<!-- apm:end -->\n",
        encoding="utf-8",
    )
    with (tmp_path / "apm.yml").open("a", encoding="utf-8") as handle:
        handle.write(
            "compilation:\n"
            "  agents_md:\n"
            "    mode: managed_section\n"
            '    start_marker: "<!-- apm:start -->"\n'
            '    end_marker: "<!-- apm:end -->"\n'
        )
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli, ["compile", "--single-agents"], catch_exceptions=False)

    written = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert result.exit_code == 0, result.output
    assert "Human-authored content." in written
    assert "Use US English." in written
    assert "Old APM block." not in written


@pytest.mark.parametrize("first_single", [False, True])
def test_compile_switches_between_generated_agents_formats(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    first_single: bool,
) -> None:
    """Both APM markers must remain writable when compile strategy changes."""
    _seed_project(tmp_path)
    (tmp_path / "AGENTS.md").unlink()
    monkeypatch.chdir(tmp_path)
    first_args = ["compile", "--single-agents"] if first_single else ["compile"]
    second_args = ["compile"] if first_single else ["compile", "--single-agents"]

    first = CliRunner().invoke(cli, first_args, catch_exceptions=False)
    second = CliRunner().invoke(cli, second_args, catch_exceptions=False)

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert "Protected AGENTS.md" not in second.output
    expected_marker = (
        "<!-- Generated by APM CLI from distributed .apm/ primitives -->"
        if first_single
        else "<!-- Generated by APM CLI from .apm/ primitives -->"
    )
    assert expected_marker in (tmp_path / "AGENTS.md").read_text(encoding="utf-8")


def test_custom_single_file_output_remains_replaceable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Root protection must not change the explicit custom-output contract."""
    _seed_project(tmp_path)
    custom_output = tmp_path / "notes.md"
    custom_output.write_text("old custom output\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        cli,
        ["compile", "--single-agents", "--output", "notes.md"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert custom_output.read_text(encoding="utf-8") != "old custom output\n"
    assert "Protected notes.md" not in result.output


@pytest.mark.parametrize("absolute", [False, True])
def test_aliased_output_path_preserves_hand_authored_root_agents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    absolute: bool,
) -> None:
    """Path aliases must not bypass root context protection."""
    _seed_project(tmp_path)
    (tmp_path / "alias").mkdir()
    monkeypatch.chdir(tmp_path)
    output = str(tmp_path / "AGENTS.md") if absolute else "alias/../AGENTS.md"

    result = CliRunner().invoke(
        cli,
        ["compile", "--single-agents", "--output", output],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") == _MANUAL_AGENTS
    assert "Protected" in result.output


def test_case_variant_output_preserves_root_agents_on_insensitive_filesystem(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Filesystem aliases that differ only by case must not bypass protection."""
    _seed_project(tmp_path)
    canonical = tmp_path / "AGENTS.md"
    case_variant = tmp_path / "agents.md"
    if not case_variant.exists() or not case_variant.samefile(canonical):
        pytest.skip("fixture filesystem is case-sensitive")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        cli,
        ["compile", "--single-agents", "--output", "agents.md"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert canonical.read_text(encoding="utf-8") == _MANUAL_AGENTS
    assert "Protected agents.md" in result.output


def test_case_variant_generated_agents_remains_replaceable_on_insensitive_filesystem(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Canonical identity must select both accepted AGENTS.md marker formats."""
    _seed_project(tmp_path)
    canonical = tmp_path / "AGENTS.md"
    canonical.write_text(
        f"# AGENTS.md\n{DISTRIBUTED_AGENTS_MARKER}\nGenerated content.\n",
        encoding="utf-8",
    )
    case_variant = tmp_path / "agents.md"
    if not case_variant.exists() or not case_variant.samefile(canonical):
        pytest.skip("fixture filesystem is case-sensitive")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        cli,
        ["compile", "--single-agents", "--output", "agents.md"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert "Protected agents.md" not in result.output
    assert AGENTS_MD_GENERATED_MARKER in canonical.read_text(encoding="utf-8")


def test_generated_claude_root_remains_replaceable_as_explicit_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Canonical CLAUDE identity must select its own ownership marker."""
    _seed_project(tmp_path)
    claude_path = tmp_path / "CLAUDE.md"
    claude_path.write_text(f"{CLAUDE_HEADER}\nGenerated content.\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        cli,
        [
            "compile",
            "--target",
            "codex",
            "--single-agents",
            "--output",
            "CLAUDE.md",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert "Protected CLAUDE.md" not in result.output
    assert AGENTS_MD_GENERATED_MARKER in claude_path.read_text(encoding="utf-8")


def test_claude_only_single_file_compile_does_not_finalize_agents_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Claude-only target must not route its preview through AGENTS.md finalization."""
    _seed_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        cli,
        ["compile", "--target", "claude", "--single-agents"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") == _MANUAL_AGENTS


def test_case_variant_custom_symlink_remains_replaceable_on_sensitive_filesystem(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lowercase custom output is not a canonical root file on sensitive filesystems."""
    _seed_project(tmp_path)
    case_variant = tmp_path / "agents.md"
    if case_variant.exists():
        pytest.skip("fixture filesystem is case-insensitive")
    target = tmp_path / "custom-output.md"
    target.write_text("Custom linked output.\n", encoding="utf-8")
    try:
        case_variant.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"file symlinks unavailable: {exc}")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        cli,
        ["compile", "--single-agents", "--output", "agents.md"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert not case_variant.is_symlink()
    assert "Protected agents.md" not in result.output
    assert target.read_text(encoding="utf-8") == "Custom linked output.\n"


@pytest.mark.parametrize("dangling", [False, True])
def test_full_file_compile_retains_root_agents_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dangling: bool,
) -> None:
    """Full-file compilation must never replace a root context symlink."""
    _seed_project(tmp_path)
    target = tmp_path / "notes" / "guidance.md"
    target.parent.mkdir()
    if not dangling:
        target.write_text("Hand-authored linked guidance.\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").unlink()
    try:
        (tmp_path / "AGENTS.md").symlink_to(target)
    except OSError as exc:
        pytest.skip(f"file symlinks unavailable: {exc}")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli, ["compile", "--single-agents"])

    assert result.exit_code == 0, result.output
    assert (tmp_path / "AGENTS.md").is_symlink()
    assert "root context symlinks are not overwritten" in result.output
    if not dangling:
        assert target.read_text(encoding="utf-8") == "Hand-authored linked guidance.\n"


def test_distributed_compile_retains_external_root_agents_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Distributed full mode must retain a root symlink before containment checks."""
    _seed_project(tmp_path)
    external = tmp_path.parent / f"{tmp_path.name}-external-full-agents.md"
    external.write_text("External hand-authored guidance.\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").unlink()
    try:
        (tmp_path / "AGENTS.md").symlink_to(external)
    except OSError as exc:
        pytest.skip(f"file symlinks unavailable: {exc}")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli, ["compile"])

    assert result.exit_code == 0, result.output
    assert (tmp_path / "AGENTS.md").is_symlink()
    assert external.read_text(encoding="utf-8") == "External hand-authored guidance.\n"
    assert "root context symlinks are not overwritten" in " ".join(result.output.split())


@pytest.mark.parametrize("dangling", [False, True])
@pytest.mark.parametrize("single_agents", [False, True])
def test_managed_section_rejects_root_agents_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dangling: bool,
    single_agents: bool,
) -> None:
    """Managed-section compilation must fail closed on root context symlinks."""
    _seed_project(tmp_path)
    target = tmp_path / "notes" / "guidance.md"
    target.parent.mkdir()
    if not dangling:
        target.write_text(
            "<!-- apm:start -->\nHand-authored linked guidance.\n<!-- apm:end -->\n",
            encoding="utf-8",
        )
    (tmp_path / "AGENTS.md").unlink()
    try:
        (tmp_path / "AGENTS.md").symlink_to(target)
    except OSError as exc:
        pytest.skip(f"file symlinks unavailable: {exc}")
    with (tmp_path / "apm.yml").open("a", encoding="utf-8") as handle:
        handle.write(
            "compilation:\n"
            "  agents_md:\n"
            "    mode: managed_section\n"
            '    start_marker: "<!-- apm:start -->"\n'
            '    end_marker: "<!-- apm:end -->"\n'
        )
    monkeypatch.chdir(tmp_path)

    args = ["compile"]
    if single_agents:
        args.append("--single-agents")
    result = CliRunner().invoke(cli, args)

    assert result.exit_code == 1
    assert (tmp_path / "AGENTS.md").is_symlink()
    if not dangling:
        assert "Hand-authored linked guidance." in target.read_text(encoding="utf-8")


def test_compile_retains_undecodable_root_context_and_explains_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unverifiable ownership must fail closed with actionable guidance."""
    _seed_project(tmp_path)
    agents_path = tmp_path / "AGENTS.md"
    original = b"# AGENTS.md\n\xff\xfe\n"
    agents_path.write_bytes(original)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli, ["compile", "--single-agents"])

    assert result.exit_code == 0, result.output
    assert agents_path.read_bytes() == original
    assert "could not verify the APM-generated marker" in result.output
    assert "Fix file access, UTF-8 encoding, or path containment" in " ".join(result.output.split())


def test_generated_marker_survives_multibyte_prefix_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid trailing codepoint split at byte 4096 must not hide ownership."""
    _seed_project(tmp_path)
    agents_path = tmp_path / "AGENTS.md"
    header = f"# AGENTS.md\n{AGENTS_MD_GENERATED_MARKER}\n".encode()
    agents_path.write_bytes(header + b"a" * (4095 - len(header)) + b"\xc3\xa9\n")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli, ["compile", "--single-agents"])

    assert result.exit_code == 0, result.output
    assert "Protected AGENTS.md" not in result.output


def test_blocked_single_file_does_not_report_constitution_as_applied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retained AGENTS.md must bypass constitution inspection and reporting."""
    _seed_project(tmp_path)
    constitution = tmp_path / ".specify" / "memory" / "constitution.md"
    constitution.parent.mkdir(parents=True)
    constitution.write_text("# Constitution\n\nDo no harm.\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        cli,
        ["compile", "--single-agents", "--with-constitution"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") == _MANUAL_AGENTS
    assert "NOT APPLIED" in result.output


def test_mixed_target_single_agents_excludes_claude_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Single AGENTS output must not concatenate another root-context artifact."""
    _seed_project(tmp_path)
    (tmp_path / "AGENTS.md").unlink()
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        cli,
        ["compile", "--single-agents", "--target", "claude,codex"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    content = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert AGENTS_MD_GENERATED_MARKER in content
    assert "CLAUDE.md Preview:" not in content
    assert "CLAUDE.md Compilation Summary" not in content


def test_marker_mentioned_in_body_does_not_grant_write_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a generated header line may identify an APM-owned root file."""
    _seed_project(tmp_path)
    manual = (
        "# AGENTS.md\n\nThis guide mentions <!-- Generated by APM CLI from .apm/ primitives -->.\n"
    )
    (tmp_path / "AGENTS.md").write_text(manual, encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli, ["compile", "--single-agents"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") == manual
    assert "Protected AGENTS.md" in result.output


def test_cleanup_marker_detection_requires_exact_header_lines(tmp_path: Path) -> None:
    """Cleanup must not treat prose that mentions a marker as generated ownership."""
    _seed_project(tmp_path)
    agents_path = tmp_path / "AGENTS.md"
    agents_path.write_text(
        "# AGENTS.md\n\nThe generated marker is "
        f"{DISTRIBUTED_AGENTS_MARKER} when APM owns this file.\n",
        encoding="utf-8",
    )
    claude_path = tmp_path / "CLAUDE.md"
    claude_path.write_text(
        f"# CLAUDE.md\n\nAPM-generated files contain {CLAUDE_HEADER} near the top.\n",
        encoding="utf-8",
    )

    assert not DistributedAgentsCompiler(tmp_path)._file_has_apm_marker(agents_path)
    assert not AgentsCompiler(str(tmp_path))._detect_stale_claude_md().has_marker


def test_cleanup_retains_root_context_symlinks(tmp_path: Path) -> None:
    """Cleanup marker detection must fail closed for root context symlinks."""
    _seed_project(tmp_path)
    agents_target = tmp_path / "generated-agents-target.md"
    agents_target.write_text(
        f"{DISTRIBUTED_AGENTS_MARKER}\nGenerated content.\n",
        encoding="utf-8",
    )
    claude_target = tmp_path / "generated-claude-target.md"
    claude_target.write_text(f"{CLAUDE_HEADER}\nGenerated content.\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").unlink()
    (tmp_path / "CLAUDE.md").unlink()
    try:
        (tmp_path / "AGENTS.md").symlink_to(agents_target)
        (tmp_path / "CLAUDE.md").symlink_to(claude_target)
    except OSError as exc:
        pytest.skip(f"file symlinks unavailable: {exc}")

    assert not DistributedAgentsCompiler(tmp_path)._file_has_apm_marker(tmp_path / "AGENTS.md")
    detection = AgentsCompiler(str(tmp_path))._detect_stale_claude_md()
    assert not detection.has_marker
    assert detection.read_error is not None


def test_managed_section_rejects_external_agents_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Managed-section reads must remain inside the compilation root."""
    _seed_project(tmp_path)
    external = tmp_path.parent / f"{tmp_path.name}-external-agents.md"
    external.write_text(
        "<!-- apm:start -->\nExternal content.\n<!-- apm:end -->\n",
        encoding="utf-8",
    )
    (tmp_path / "AGENTS.md").unlink()
    try:
        (tmp_path / "AGENTS.md").symlink_to(external)
    except OSError as exc:
        pytest.skip(f"file symlinks unavailable: {exc}")
    with (tmp_path / "apm.yml").open("a", encoding="utf-8") as handle:
        handle.write(
            "compilation:\n"
            "  agents_md:\n"
            "    mode: managed_section\n"
            '    start_marker: "<!-- apm:start -->"\n'
            '    end_marker: "<!-- apm:end -->"\n'
        )
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli, ["compile", "--single-agents"])

    assert result.exit_code == 1
    assert "Compilation failed with 1 errors" in result.output
    assert external.read_text(encoding="utf-8").startswith("<!-- apm:start -->")
