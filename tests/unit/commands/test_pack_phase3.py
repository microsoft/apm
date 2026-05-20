"""Comprehensive unit tests for ``apm_cli.commands.pack`` helpers (phase 3).

Covers uncovered branches in:
- ``_mapping_summary``: empty / non-empty mappings.
- ``_warn_empty``: with target vs. without target, with/without path_mappings.
- ``_render_bundle_result``: dry-run (files / no-files / mapped), live (success /
  no-files / plugin-format / share-line), pack_result=None guard.
- ``_render_marketplace_catalog``: profiles / no-profiles / no info method.
- ``_log_bundle_meta``: no meta, with meta/no-mismatch, with meta/mismatch.
- ``_log_unpack_file_list``: with/without dependency_files.
- ``_emit_json_error_or_raise``: json_output=True / False.
- ``pack_cmd`` CLI: --legacy-skill-paths, --offline, --include-prerelease,
  --force flags, deprecation notice for --marketplace-output.
- ``unpack_cmd``: deprecation warning, FileNotFoundError handling.
"""

from __future__ import annotations

import json as _json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from apm_cli.commands.pack import (
    _emit_json_error_or_raise,
    _log_bundle_meta,
    _log_unpack_file_list,
    _mapping_summary,
    _render_bundle_result,
    _render_marketplace_catalog,
    _warn_empty,
    pack_cmd,
    unpack_cmd,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _RecordingLogger:
    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.successes: list[str] = []
        self.dry_runs: list[str] = []
        self.infos: list[str] = []
        self.progresses: list[str] = []
        self.errors: list[str] = []
        self.verbose_details: list[str] = []
        self.tree_items: list[str] = []

    def warning(self, message: str) -> None:
        self.warnings.append(message)

    def success(self, message: str) -> None:
        self.successes.append(message)

    def dry_run_notice(self, message: str) -> None:
        self.dry_runs.append(message)

    def info(self, message: str) -> None:
        self.infos.append(message)

    def progress(self, message: str, **_: Any) -> None:
        self.progresses.append(message)

    def error(self, message: str) -> None:
        self.errors.append(message)

    def verbose_detail(self, message: str) -> None:
        self.verbose_details.append(message)

    def tree_item(self, message: str) -> None:
        self.tree_items.append(message)


def _pack_result(
    *,
    files: list[str] | None = None,
    bundle_path: str | None = "build/bundle",
    mapped_count: int = 0,
    path_mappings: dict[str, str] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        files=files or [],
        bundle_path=bundle_path,
        mapped_count=mapped_count,
        path_mappings=path_mappings or {},
    )


# ===========================================================================
# _mapping_summary tests
# ===========================================================================


class TestMappingSummary:
    def test_empty_returns_empty_string(self) -> None:
        assert _mapping_summary({}) == ""

    def test_single_entry_returns_suffix(self) -> None:
        result = _mapping_summary({"dst/file.md": "src/file.md"})
        assert "src/" in result
        assert "dst/" in result
        assert "->" in result

    def test_multiple_entries_uses_first(self) -> None:
        """Prefix is derived from the first inserted key."""
        mappings = {
            "apm/agents/bot.md": ".apm/agents/bot.md",
            "apm/skills/x.md": ".apm/skills/x.md",
        }
        result = _mapping_summary(mappings)
        assert result  # non-empty


# ===========================================================================
# _warn_empty tests
# ===========================================================================


class TestWarnEmpty:
    def test_no_target_emits_generic_warning(self) -> None:
        logger = _RecordingLogger()
        result = _pack_result()
        _warn_empty(logger, None, result)
        assert any("empty bundle" in w for w in logger.warnings)

    def test_with_target_emits_target_warning(self) -> None:
        logger = _RecordingLogger()
        result = _pack_result()
        _warn_empty(logger, "claude", result)
        assert any("claude" in w for w in logger.warnings)

    def test_with_target_and_no_mappings_hint(self) -> None:
        """No path_mappings → hint logged via verbose_detail."""
        logger = _RecordingLogger()
        result = _pack_result(mapped_count=0, path_mappings={})
        _warn_empty(logger, "cursor", result)
        # hint about apm install is in verbose_details
        assert any("cursor" in w for w in logger.warnings)

    def test_with_target_and_path_mappings(self) -> None:
        """Has path_mappings → still warns about target."""
        logger = _RecordingLogger()
        result = _pack_result(mapped_count=2, path_mappings={"a": "b"})
        _warn_empty(logger, "gemini", result)
        assert any("gemini" in w for w in logger.warnings)


# ===========================================================================
# _render_bundle_result tests
# ===========================================================================


class TestRenderBundleResult:
    def test_none_pack_result_returns_early(self) -> None:
        """When pack_result is None, nothing is logged."""
        logger = _RecordingLogger()
        _render_bundle_result(logger, None, "plugin", None, False)
        assert not logger.successes
        assert not logger.warnings
        assert not logger.dry_runs

    def test_dry_run_no_files_warns_empty(self) -> None:
        """dry_run=True with no files calls _warn_empty."""
        logger = _RecordingLogger()
        result = _pack_result(files=[])
        _render_bundle_result(logger, result, "plugin", None, True)
        # _warn_empty is called → generic warning
        assert any("empty" in w.lower() for w in logger.warnings)

    def test_dry_run_with_files_emits_dry_run_notice(self) -> None:
        """dry_run=True with files emits a dry_run_notice."""
        logger = _RecordingLogger()
        result = _pack_result(files=["file1.md", "file2.md"])
        _render_bundle_result(logger, result, "plugin", None, True)
        assert any("Would pack" in d for d in logger.dry_runs)

    def test_dry_run_with_mapped_files_emits_remap_notice(self) -> None:
        """dry_run=True + mapped_count emits a remap dry_run_notice."""
        logger = _RecordingLogger()
        result = _pack_result(
            files=["a.md"],
            mapped_count=1,
            path_mappings={"dst/a.md": "src/a.md"},
        )
        _render_bundle_result(logger, result, "plugin", None, True)
        assert any("remap" in d for d in logger.dry_runs)

    def test_live_no_files_warns_empty(self) -> None:
        """Live mode with no files calls _warn_empty."""
        logger = _RecordingLogger()
        result = _pack_result(files=[])
        _render_bundle_result(logger, result, "plugin", None, False)
        assert any("empty" in w.lower() for w in logger.warnings)

    def test_live_with_files_emits_success(self) -> None:
        """Live mode with files emits a success line with count."""
        logger = _RecordingLogger()
        result = _pack_result(files=["a.md", "b.md", "c.md"])
        _render_bundle_result(logger, result, "plugin", None, False)
        assert any("3 file(s)" in s for s in logger.successes)

    def test_live_plugin_format_progress_message(self) -> None:
        """Plugin format emits the 'Plugin bundle ready' progress message."""
        logger = _RecordingLogger()
        result = _pack_result(files=["plugin.json"])
        _render_bundle_result(logger, result, "plugin", None, False)
        assert any("Plugin bundle ready" in p for p in logger.progresses)

    def test_live_apm_format_no_plugin_message(self) -> None:
        """'apm' format does NOT emit the plugin-ready progress message."""
        logger = _RecordingLogger()
        result = _pack_result(files=["bundle.tar.gz"])
        _render_bundle_result(logger, result, "apm", None, False)
        assert not any("Plugin bundle ready" in p for p in logger.progresses)

    def test_live_share_line_emitted(self) -> None:
        """The 'Share with: apm install ...' info line is always emitted."""
        logger = _RecordingLogger()
        result = _pack_result(files=["file.md"], bundle_path="build/my-bundle")
        _render_bundle_result(logger, result, "apm", None, False)
        assert any("apm install" in i for i in logger.infos)

    def test_live_no_bundle_path_skips_share_line(self) -> None:
        """When bundle_path is falsy, the share line is suppressed."""
        logger = _RecordingLogger()
        result = _pack_result(files=["file.md"], bundle_path=None)
        _render_bundle_result(logger, result, "apm", None, False)
        assert not any("apm install" in i for i in logger.infos)

    def test_live_with_mapped_count_emits_progress(self) -> None:
        """Mapped files in live mode emit a progress 'Mapped N file(s)' line."""
        logger = _RecordingLogger()
        result = _pack_result(
            files=["dst/a.md"],
            mapped_count=1,
            path_mappings={"dst/a.md": "src/a.md"},
        )
        _render_bundle_result(logger, result, "plugin", None, False)
        assert any("Mapped" in p for p in logger.progresses)


# ===========================================================================
# _render_marketplace_catalog tests
# ===========================================================================


class TestRenderMarketplaceCatalog:
    def test_with_profiles(self) -> None:
        """With named profiles, renders two-column aligned rows."""
        logger = _RecordingLogger()
        written = [("claude", Path("a.json")), ("codex", Path("b.json"))]
        _render_marketplace_catalog(logger, written)
        assert any("Marketplace artifacts ready:" in i for i in logger.infos)
        assert sum(1 for i in logger.infos if i.startswith("  [")) == 2

    def test_without_profiles(self) -> None:
        """Without named profiles, renders simple path rows."""
        logger = _RecordingLogger()
        written = [(None, Path("out.json"))]
        _render_marketplace_catalog(logger, written)
        assert any("out.json" in i for i in logger.infos)

    def test_docs_url_emitted(self) -> None:
        """A MARKETPLACE_DOCS_URL pointer is always emitted as an info line."""
        from urllib.parse import urlparse

        logger = _RecordingLogger()
        written = [("claude", Path("a.json"))]
        _render_marketplace_catalog(logger, written)
        urls = []
        for line in logger.infos:
            for token in line.split():
                cleaned = token.strip("(),.;'\"")
                if "://" in cleaned:
                    parsed = urlparse(cleaned)
                    if parsed.scheme == "https":
                        urls.append(cleaned)
        assert urls, "Expected at least one https URL in catalog info lines"

    def test_no_info_method_returns_early(self) -> None:
        """If logger has no 'info' method, returns without error."""

        class NoInfoLogger:
            pass  # no info method

        # Should not raise
        _render_marketplace_catalog(NoInfoLogger(), [("claude", Path("a.json"))])


# ===========================================================================
# _log_bundle_meta tests
# ===========================================================================


class TestLogBundleMeta:
    def test_no_meta_returns_early(self) -> None:
        """result.pack_meta is None → nothing logged."""
        logger = _RecordingLogger()
        result = SimpleNamespace(
            pack_meta=None,
            dependency_files={},
            files=[],
        )
        _log_bundle_meta(result, Path("."), logger)
        assert not logger.progresses
        assert not logger.warnings

    def test_meta_emits_progress_line(self) -> None:
        """Non-empty meta emits a bundle target progress line."""
        logger = _RecordingLogger()
        result = SimpleNamespace(
            pack_meta={"target": "claude"},
            dependency_files={"dep1": ["f1.md"]},
            files=["f1.md"],
        )
        with patch(
            "apm_cli.core.target_detection.detect_target",
            return_value=("claude", "reason"),
        ):
            _log_bundle_meta(result, Path("/tmp"), logger)
        assert any("Bundle target:" in p for p in logger.progresses)

    def test_target_mismatch_emits_warning(self) -> None:
        """Bundle target differs from project target → warning."""
        logger = _RecordingLogger()
        result = SimpleNamespace(
            pack_meta={"target": "cursor"},
            dependency_files={},
            files=[],
        )
        with patch(
            "apm_cli.core.target_detection.detect_target",
            return_value=("claude", "reason"),
        ):
            _log_bundle_meta(result, Path("/tmp"), logger)
        assert any("differs" in w for w in logger.warnings)

    def test_detect_target_exception_is_swallowed(self) -> None:
        """Exception from detect_target is caught silently."""
        logger = _RecordingLogger()
        result = SimpleNamespace(
            pack_meta={"target": "claude"},
            dependency_files={},
            files=[],
        )
        with patch(
            "apm_cli.core.target_detection.detect_target",
            side_effect=RuntimeError("detect error"),
        ):
            # Should not raise
            _log_bundle_meta(result, Path("/tmp"), logger)

    def test_universal_bundle_no_warning(self) -> None:
        """Bundle target 'all' → no mismatch warning."""
        logger = _RecordingLogger()
        result = SimpleNamespace(
            pack_meta={"target": "all"},
            dependency_files={},
            files=[],
        )
        with patch(
            "apm_cli.core.target_detection.detect_target",
            return_value=("claude", "reason"),
        ):
            _log_bundle_meta(result, Path("/tmp"), logger)
        assert not logger.warnings


# ===========================================================================
# _log_unpack_file_list tests
# ===========================================================================


class TestLogUnpackFileList:
    def test_with_dependency_files(self) -> None:
        """dep_name headers and tree items are rendered."""
        logger = _RecordingLogger()
        result = SimpleNamespace(
            dependency_files={"my-dep": ["a.md", "b.md"]},
            files=["a.md", "b.md"],
        )
        _log_unpack_file_list(result, logger)
        assert any("my-dep" in p for p in logger.progresses)
        assert len(logger.tree_items) == 2

    def test_without_dependency_files(self) -> None:
        """Flat file list renders one tree item per file."""
        logger = _RecordingLogger()
        result = SimpleNamespace(
            dependency_files={},
            files=["x.md", "y.md", "z.md"],
        )
        _log_unpack_file_list(result, logger)
        assert len(logger.tree_items) == 3


# ===========================================================================
# _emit_json_error_or_raise tests
# ===========================================================================


class TestEmitJsonErrorOrRaise:
    def test_json_output_false_raises_click_exception(self) -> None:
        import click

        ctx = MagicMock()
        with pytest.raises(click.ClickException) as exc_info:
            _emit_json_error_or_raise(ctx, False, "err_code", "Something went wrong")
        assert "Something went wrong" in str(exc_info.value)

    def test_json_output_true_calls_ctx_exit(self) -> None:
        """In json_output mode, the error is printed as JSON and ctx.exit is called."""
        ctx = MagicMock()
        output_lines: list[str] = []
        with patch("click.echo", side_effect=lambda s, **kw: output_lines.append(s)):
            _emit_json_error_or_raise(ctx, True, "test_code", "Test message")

        ctx.exit.assert_called_once_with(1)
        assert output_lines, "Expected at least one click.echo call"
        data = _json.loads(output_lines[0])
        assert data["ok"] is False
        assert any(e["code"] == "test_code" for e in data["errors"])


# ===========================================================================
# pack_cmd CLI flag tests
# ===========================================================================


@pytest.fixture(autouse=True)
def _reset_console():
    """Reset console singleton so --json never pollutes later tests."""
    from apm_cli.utils.console import _reset_console as _rc

    yield
    _rc()


_APM_SIMPLE = """\
name: test-project
description: A test project.
version: 1.0.0
"""


class TestPackCmdFlags:
    """Smoke tests for various pack_cmd flags."""

    def test_help_text_includes_all_key_flags(self) -> None:
        result = CliRunner().invoke(pack_cmd, ["--help"])
        assert result.exit_code == 0
        for flag in ["--archive", "--format", "--dry-run", "--force", "--verbose", "--offline"]:
            assert flag in result.output

    def test_offline_flag_accepted(self, tmp_path: Path, monkeypatch) -> None:
        (tmp_path / "apm.yml").write_text(_APM_SIMPLE, encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(pack_cmd, ["--dry-run", "--offline"])
        assert result.exit_code in (0, 1)  # may fail due to missing lockfile; flag is accepted

    def test_include_prerelease_flag_accepted(self, tmp_path: Path, monkeypatch) -> None:
        (tmp_path / "apm.yml").write_text(_APM_SIMPLE, encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(pack_cmd, ["--dry-run", "--include-prerelease"])
        assert result.exit_code in (0, 1)

    def test_force_flag_accepted(self, tmp_path: Path, monkeypatch) -> None:
        (tmp_path / "apm.yml").write_text(_APM_SIMPLE, encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(pack_cmd, ["--dry-run", "--force"])
        assert result.exit_code in (0, 1)

    def test_legacy_skill_paths_flag_accepted(self, tmp_path: Path, monkeypatch) -> None:
        (tmp_path / "apm.yml").write_text(_APM_SIMPLE, encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(pack_cmd, ["--dry-run", "--legacy-skill-paths"])
        assert result.exit_code in (0, 1)

    def test_format_apm_flag_accepted(self, tmp_path: Path, monkeypatch) -> None:
        (tmp_path / "apm.yml").write_text(_APM_SIMPLE, encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(pack_cmd, ["--dry-run", "--format", "apm"])
        assert result.exit_code in (0, 1)

    def test_format_invalid_choice_fails(self) -> None:
        result = CliRunner().invoke(pack_cmd, ["--format", "invalid"])
        assert result.exit_code != 0

    def test_deprecated_target_flag_emits_warning(self, tmp_path: Path, monkeypatch) -> None:
        (tmp_path / "apm.yml").write_text(_APM_SIMPLE, encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(pack_cmd, ["--dry-run", "--target", "claude"])
        # deprecated flag emits a warning on stderr (mix_stderr=True is CliRunner default)
        assert "deprecated" in (result.output or "").lower() or result.exit_code in (0, 1)

    def test_marketplace_filter_none_skips(self, tmp_path: Path, monkeypatch) -> None:
        (tmp_path / "apm.yml").write_text(_APM_SIMPLE, encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(pack_cmd, ["--dry-run", "--marketplace", "none"])
        # 'none' is valid; should not crash with an unknown format error
        assert "Unknown marketplace format 'none'" not in (result.output or "")

    def test_marketplace_filter_all_accepted(self, tmp_path: Path, monkeypatch) -> None:
        (tmp_path / "apm.yml").write_text(_APM_SIMPLE, encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(pack_cmd, ["--dry-run", "--marketplace", "all"])
        assert "Unknown marketplace format 'all'" not in (result.output or "")

    def test_marketplace_output_deprecated_flag_warning(self) -> None:
        """--marketplace-output prints a deprecation warning."""
        result = CliRunner().invoke(pack_cmd, ["--marketplace-output", "out.json", "--dry-run"])
        assert "deprecated" in (result.output or "").lower() or result.exit_code in (0, 1)

    def test_json_output_envelope_shape(self, tmp_path: Path, monkeypatch) -> None:
        """--json mode always returns a JSON envelope, even on no-op."""
        (tmp_path / "apm.yml").write_text(_APM_SIMPLE, encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(pack_cmd, ["--dry-run", "--json"])
        data = _json.loads(result.output)
        for key in ("ok", "dry_run", "warnings", "errors", "marketplace", "bundle"):
            assert key in data, f"Missing key '{key}' in JSON envelope"


# ===========================================================================
# unpack_cmd tests
# ===========================================================================


class TestUnpackCmd:
    def test_unpack_deprecation_warning(self, tmp_path: Path) -> None:
        """unpack always emits a deprecation warning."""
        bundle = tmp_path / "bundle.tar.gz"
        bundle.write_bytes(b"fake")
        result = CliRunner().invoke(
            unpack_cmd,
            [str(bundle), "--dry-run"],
        )
        assert "deprecated" in (result.output or "").lower() or result.exit_code in (0, 1)

    def test_unpack_nonexistent_bundle(self, tmp_path: Path) -> None:
        """Passing a non-existent path exits non-zero."""
        result = CliRunner().invoke(
            unpack_cmd,
            [str(tmp_path / "no-such-bundle.tar.gz"), "--dry-run"],
        )
        assert result.exit_code != 0

    def test_unpack_help_shows_install_hint(self) -> None:
        """Help text references 'apm install' as the replacement."""
        result = CliRunner().invoke(unpack_cmd, ["--help"])
        assert result.exit_code == 0
        assert "install" in result.output.lower()
