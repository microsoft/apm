"""Unit tests for ``apm pack`` command helpers."""

from __future__ import annotations

import json as _json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.bundle.formats import BundleFormat
from apm_cli.commands.pack import (
    _emit_json_error_or_raise,
    _log_bundle_meta,
    _log_unpack_file_list,
    _mapping_summary,
    _parse_marketplace_filter,
    _parse_path_overrides,
    _render_bundle_result,
    _render_marketplace_catalog,
    _render_marketplace_result,
    _warn_empty,
)
from apm_cli.marketplace.builder import BuildReport, MarketplaceOutputReport

pytestmark = pytest.mark.unit


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


# ---------------------------------------------------------------------------
# _parse_path_overrides unit tests
# ---------------------------------------------------------------------------


def _make_ctx(json_output: bool = False):
    """Return a minimal Click context mock for helper tests."""
    ctx = MagicMock()
    exited = []

    def _exit(code=0):
        exited.append(code)

    ctx.exit.side_effect = _exit
    ctx._exited = exited
    return ctx


class TestParsePathOverrides:
    """Unit tests for _parse_path_overrides()."""

    def test_empty_tuple_returns_empty_dict(self) -> None:
        ctx = _make_ctx()
        result = _parse_path_overrides((), ctx, json_output=False)
        assert result == {}

    def test_valid_single_override(self) -> None:
        ctx = _make_ctx()
        result = _parse_path_overrides(("claude=dist/marketplace.json",), ctx, json_output=False)
        assert result == {"claude": "dist/marketplace.json"}

    def test_valid_multiple_overrides(self) -> None:
        ctx = _make_ctx()
        result = _parse_path_overrides(
            ("claude=dist/claude.json", "codex=dist/codex.json"),
            ctx,
            json_output=False,
        )
        assert result == {"claude": "dist/claude.json", "codex": "dist/codex.json"}

    def test_missing_equals_returns_none(self) -> None:
        ctx = _make_ctx(json_output=True)
        result = _parse_path_overrides(("claude-no-equals",), ctx, json_output=True)
        assert result is None

    def test_unknown_format_returns_none(self) -> None:
        ctx = _make_ctx(json_output=True)
        result = _parse_path_overrides(("unknown_format=dist/foo.json",), ctx, json_output=True)
        assert result is None

    def test_path_traversal_returns_none(self) -> None:
        ctx = _make_ctx(json_output=True)
        result = _parse_path_overrides(("claude=../../etc/passwd",), ctx, json_output=True)
        assert result is None

    def test_missing_equals_raises_click_exception_non_json(self) -> None:
        import click as _click

        ctx = _make_ctx()
        with pytest.raises(_click.ClickException):
            _parse_path_overrides(("no-equals",), ctx, json_output=False)

    def test_path_traversal_raises_click_exception_non_json(self) -> None:
        import click as _click

        ctx = _make_ctx()
        with pytest.raises(_click.ClickException):
            _parse_path_overrides(("claude=../../etc/passwd",), ctx, json_output=False)

    def test_strips_whitespace_around_name_and_path(self) -> None:
        ctx = _make_ctx()
        result = _parse_path_overrides(
            (" claude = dist/marketplace.json ",), ctx, json_output=False
        )
        assert result == {"claude": "dist/marketplace.json"}


# ---------------------------------------------------------------------------
# _parse_marketplace_filter unit tests
# ---------------------------------------------------------------------------


class TestParseMarketplaceFilter:
    """Unit tests for _parse_marketplace_filter()."""

    def test_none_returns_none(self) -> None:
        ctx = _make_ctx()
        result = _parse_marketplace_filter(None, ctx, json_output=False)
        assert result is None

    def test_none_string_returns_empty_tuple(self) -> None:
        ctx = _make_ctx()
        result = _parse_marketplace_filter("none", ctx, json_output=False)
        assert result == ()

    def test_none_string_case_insensitive(self) -> None:
        ctx = _make_ctx()
        result = _parse_marketplace_filter("NONE", ctx, json_output=False)
        assert result == ()

    def test_all_string_returns_none(self) -> None:
        ctx = _make_ctx()
        result = _parse_marketplace_filter("all", ctx, json_output=False)
        assert result is None

    def test_all_string_case_insensitive(self) -> None:
        ctx = _make_ctx()
        result = _parse_marketplace_filter("ALL", ctx, json_output=False)
        assert result is None

    def test_single_known_format(self) -> None:
        ctx = _make_ctx()
        result = _parse_marketplace_filter("claude", ctx, json_output=False)
        assert result == ("claude",)

    def test_multiple_known_formats(self) -> None:
        ctx = _make_ctx()
        result = _parse_marketplace_filter("claude,codex", ctx, json_output=False)
        assert result == ("claude", "codex")

    def test_formats_with_whitespace(self) -> None:
        ctx = _make_ctx()
        result = _parse_marketplace_filter(" claude , codex ", ctx, json_output=False)
        assert result == ("claude", "codex")

    def test_unknown_format_exits_with_error(self) -> None:
        ctx = _make_ctx(json_output=True)
        result = _parse_marketplace_filter("unknown_format", ctx, json_output=True)
        # With json_output=True, ctx.exit(1) is called and function returns None
        assert result is None
        assert ctx._exited == [1]


def test_marketplace_fallback_renders_warnings_and_package_count() -> None:
    logger = _RecordingLogger()
    fallback_report = SimpleNamespace(
        outputs=(),
        resolved=(object(), object()),
        warnings=("duplicate package warning",),
    )

    _render_marketplace_result(
        logger,
        fallback_report,
        dry_run=False,
        extra_warnings=("duplicate package warning",),
        outputs=[Path("marketplace.json")],
    )

    assert logger.warnings == ["duplicate package warning"]
    assert logger.successes == ["Built marketplace.json (2 package(s)) -> marketplace.json"]


def test_marketplace_fallback_renders_report_warnings_without_extra_warnings() -> None:
    logger = _RecordingLogger()

    _render_marketplace_result(
        logger,
        BuildReport(
            outputs=(
                MarketplaceOutputReport(
                    profile="claude",
                    resolved=(),
                    errors=(),
                    warnings=("report warning",),
                    output_path=Path("unused"),
                ),
            )
        ),
        dry_run=False,
        outputs=[],
    )

    assert logger.warnings == ["report warning"]


# ---------------------------------------------------------------------------
# G3+B: vendor-neutral post-pack catalog
# ---------------------------------------------------------------------------


def _build_report_with_outputs(
    profiles_paths: list[tuple[str, str]],
) -> BuildReport:
    """Helper: build a BuildReport with N MarketplaceOutputReport entries."""
    return BuildReport(
        outputs=tuple(
            MarketplaceOutputReport(
                profile=p,
                resolved=(object(),),
                errors=(),
                warnings=(),
                output_path=Path(path),
            )
            for p, path in profiles_paths
        )
    )


def test_post_pack_catalog_emits_artifacts_and_docs_url() -> None:
    """G3+B: after per-output success lines, render a catalog and a
    single docs pointer that consumers can follow."""
    from urllib.parse import urlparse

    logger = _RecordingLogger()
    report = _build_report_with_outputs(
        [
            ("claude", ".claude-plugin/marketplace.json"),
            ("codex", ".agents/plugins/marketplace.json"),
        ]
    )

    _render_marketplace_result(logger, report, dry_run=False, outputs=[])

    # Per-output success lines still fire
    assert len(logger.successes) == 2
    # Catalog header is an [i] info line (the things were already
    # successfully built; this block is recap + guidance).
    assert any(line.startswith("Marketplace artifacts ready:") for line in logger.infos)
    # Two indented artifact rows
    assert sum(1 for line in logger.infos if line.startswith("  ")) == 2
    # Single docs pointer URL line -- parsed, not substring-matched.
    docs_urls: list[str] = []
    for line in logger.infos:
        for token in line.split():
            cleaned = token.strip("(),.;'\"")
            if "://" not in cleaned:
                continue
            if "installing-from-marketplaces" in urlparse(cleaned).path:
                docs_urls.append(cleaned)
    assert len(docs_urls) == 1, f"expected exactly one docs URL, got {docs_urls!r}"
    parsed = urlparse(docs_urls[0])
    assert parsed.scheme == "https"
    assert parsed.hostname == "microsoft.github.io"
    assert parsed.path == "/apm/consumer/installing-from-marketplaces/"
    assert parsed.fragment == "install-patterns"


def test_post_pack_catalog_suppressed_in_dry_run() -> None:
    """Dry-run mode never writes the files, so suppress the 'ready' chatter."""
    logger = _RecordingLogger()
    report = _build_report_with_outputs([("claude", ".claude-plugin/marketplace.json")])

    _render_marketplace_result(logger, report, dry_run=True, outputs=[])

    assert logger.successes == []  # dry_run path uses dry_run_notice
    assert logger.dry_runs  # dry-run lines fire
    assert logger.infos == []  # no catalog/guidance in dry-run


def test_post_pack_catalog_vendor_neutral() -> None:
    """The catalog and docs pointer must not name any specific vendor
    CLI command (Copilot, Claude, Codex, Cursor, ...). APM is vendor-
    agnostic; per-assistant install instructions live in the docs page."""
    logger = _RecordingLogger()
    report = _build_report_with_outputs(
        [
            ("claude", ".claude-plugin/marketplace.json"),
            ("codex", ".agents/plugins/marketplace.json"),
        ]
    )

    _render_marketplace_result(logger, report, dry_run=False, outputs=[])

    forbidden_phrases = (
        "copilot plugin install",
        "claude plugin install",
        "codex plugin install",
        "cursor plugin install",
    )
    for needle in forbidden_phrases:
        for line in logger.infos:
            assert needle.lower() not in line.lower(), (
                f"Vendor-neutral catalog must not mention '{needle}': {line!r}"
            )


def test_post_pack_catalog_aligns_profiles_in_columns() -> None:
    """When multiple profiles fire, render in aligned two-column form
    so the eye can scan paths."""
    logger = _RecordingLogger()
    report = _build_report_with_outputs(
        [
            ("claude", ".claude-plugin/marketplace.json"),
            ("codex", ".agents/plugins/marketplace.json"),
        ]
    )

    _render_marketplace_result(logger, report, dry_run=False, outputs=[])

    catalog_rows = [line for line in logger.infos if line.startswith("  [")]
    assert len(catalog_rows) == 2
    # Both rows have an aligned label width (e.g. "[claude]" and "[codex] ")
    label_lengths = {line.split("]")[0] for line in catalog_rows}
    assert len(label_lengths) == 2  # both profiles present


# ---------------------------------------------------------------------------
# Wave 4: release-gate integration tests
# ---------------------------------------------------------------------------


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
        _render_bundle_result(logger, result, BundleFormat.AGENT_PLUGIN, None, False)
        assert any("empty" in w.lower() for w in logger.warnings)

    def test_live_with_files_emits_success(self) -> None:
        """Live mode with files emits a success line with count."""
        logger = _RecordingLogger()
        result = _pack_result(files=["a.md", "b.md", "c.md"])
        _render_bundle_result(logger, result, "plugin", None, False)
        assert any("3 file(s)" in s for s in logger.successes)

    def test_live_plugin_format_progress_message(self) -> None:
        """Plugin format emits the Agent Plugin ready progress message."""
        logger = _RecordingLogger()
        result = _pack_result(files=["plugin.json"])
        _render_bundle_result(logger, result, BundleFormat.AGENT_PLUGIN, None, False)
        assert any("Agent Plugin bundle ready" in p for p in logger.progresses)

    def test_live_apm_format_no_plugin_message(self) -> None:
        """'apm' format does NOT emit the plugin-ready progress message."""
        logger = _RecordingLogger()
        result = _pack_result(files=["bundle.zip"])
        _render_bundle_result(logger, result, "apm", None, False)
        assert not any("Plugin bundle ready" in p for p in logger.progresses)

    def test_live_share_line_emitted(self) -> None:
        """The share line names the consumer that accepts the format (#3994)."""
        logger = _RecordingLogger()
        result = _pack_result(files=["file.md"], bundle_path="build/my-bundle")
        _render_bundle_result(logger, result, BundleFormat.CLAUDE_PLUGIN, None, False)
        # A bare 'build/x' parses as owner/repo, so the line spells a path (#3994).
        assert "Share with: apm install ./build/my-bundle" in logger.infos

    def test_live_count_names_the_embedded_lockfile(self) -> None:
        """Counts say the bundle also embeds apm.lock.yaml (#3994)."""
        logger = _RecordingLogger()
        result = _pack_result(files=["a.md", "b.md"], bundle_path="build/x")
        _render_bundle_result(logger, result, BundleFormat.CLAUDE_PLUGIN, None, False)
        assert any("Packed 2 file(s) plus apm.lock.yaml -> build/x" in m for m in logger.successes)

    def test_live_legacy_format_shares_with_unpack(self) -> None:
        """'apm install' rejects the legacy APM layout; unpack reads it (#3994)."""
        logger = _RecordingLogger()
        result = _pack_result(files=["file.md"], bundle_path="build/my-bundle.zip")
        _render_bundle_result(logger, result, BundleFormat.APM, None, False)
        share = next(i for i in logger.infos if i.startswith("Share with:"))
        assert share.startswith("Share with: apm unpack ./build/my-bundle.zip (deprecated")
        assert not share.startswith("Share with: apm install")

    def test_live_agent_plugin_shares_by_declaration(self) -> None:
        """Agent plugins install declaratively, never via 'apm install <path>' (#3994)."""
        logger = _RecordingLogger()
        result = _pack_result(files=["plugin.json"], bundle_path="build/x")
        _render_bundle_result(logger, result, BundleFormat.AGENT_PLUGIN, None, False)
        share = next(i for i in logger.infos if i.startswith("Share"))
        assert "under dependencies.apm" in share
        assert "'apm install --target copilot'" in share
        assert not share.startswith("Share with: apm install")

    def test_live_zip_archive_emits_migration_tip_when_requested(self) -> None:
        logger = _RecordingLogger()
        result = _pack_result(files=["file.md"], bundle_path="build/my-bundle.zip")
        _render_bundle_result(logger, result, "plugin", None, False, show_zip_migration_notice=True)
        assert any("--archive now produces .zip" in i for i in logger.infos)
        assert any("--archive-format tar.gz" in i for i in logger.infos)

    def test_live_zip_archive_suppresses_migration_tip_when_not_requested(self) -> None:
        logger = _RecordingLogger()
        result = _pack_result(files=["file.md"], bundle_path="build/my-bundle.zip")
        _render_bundle_result(
            logger, result, "plugin", None, False, show_zip_migration_notice=False
        )
        assert not any("--archive now produces .zip" in i for i in logger.infos)

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
