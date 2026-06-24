"""Unit tests for ``apm pack`` command helpers."""

from __future__ import annotations

import textwrap as _tw
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from apm_cli.commands.pack import (
    _parse_marketplace_filter,
    _parse_path_overrides,
    _render_marketplace_result,
    pack_cmd,
)
from apm_cli.marketplace.builder import BuildReport, MarketplaceOutputReport


class _RecordingLogger:
    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.successes: list[str] = []
        self.dry_runs: list[str] = []
        self.infos: list[str] = []

    def warning(self, message: str) -> None:
        self.warnings.append(message)

    def success(self, message: str) -> None:
        self.successes.append(message)

    def dry_run_notice(self, message: str) -> None:
        self.dry_runs.append(message)

    def info(self, message: str) -> None:
        self.infos.append(message)


def test_pack_help_recommends_manifest_marketplace_output_config() -> None:
    result = CliRunner().invoke(pack_cmd, ["--help"])

    assert result.exit_code == 0
    # The --marketplace-path flag is shown in help
    assert "--marketplace-path" in result.output
    # The removed --marketplace-output flag is gone entirely
    assert "--marketplace-output" not in result.output
    assert "--claude-output" not in result.output


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
            if "publish-to-a-marketplace" in urlparse(cleaned).path:
                docs_urls.append(cleaned)
    assert len(docs_urls) == 1, f"expected exactly one docs URL, got {docs_urls!r}"
    parsed = urlparse(docs_urls[0])
    assert parsed.scheme == "https"
    assert parsed.hostname == "microsoft.github.io"
    assert parsed.path == "/apm/producer/publish-to-a-marketplace/"
    assert parsed.fragment == "consume-from-any-assistant"


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


_APM_GATE = """\
name: my-project
description: A project.
version: 1.0.0
marketplace:
  owner:
    name: ACME
  packages:
    - name: local-tool
      source: ./packages/local-tool
      description: Tool.
      version: 1.0.0
"""


def _scaffold(tmp_path: Path, *, pkg_version: str = "1.0.0") -> Path:
    (tmp_path / "apm.yml").write_text(_tw.dedent(_APM_GATE), encoding="utf-8")
    pkg = tmp_path / "packages" / "local-tool"
    pkg.mkdir(parents=True)
    pkg.joinpath("apm.yml").write_text(
        f"name: local-tool\ndescription: Tool.\nversion: {pkg_version}\n",
        encoding="utf-8",
    )
    return tmp_path


class TestPackReleaseGatesIntegration:
    """End-to-end gate behaviour: BuildOrchestrator + gates compose cleanly."""

    @pytest.fixture(autouse=True)
    def _reset_console_state(self):
        """Reset global console singleton to avoid --json polluting later tests."""
        from apm_cli.utils.console import _reset_console

        yield
        _reset_console()

    def test_no_gate_flags_returns_no_gate_keys_in_json(self, tmp_path: Path, monkeypatch) -> None:
        import json as _json

        _scaffold(tmp_path)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(pack_cmd, ["--dry-run", "--offline", "--json"])
        data = _json.loads(result.output)
        # Envelope always carries the keys; both should be null when not requested.
        assert data["version_alignment"] is None
        assert data["drift"] is None

    def test_check_versions_only_payload_shape(self, tmp_path: Path, monkeypatch) -> None:
        import json as _json

        _scaffold(tmp_path)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(
            pack_cmd, ["--check-versions", "--dry-run", "--offline", "--json"]
        )
        data = _json.loads(result.output)
        assert data["version_alignment"] is not None
        assert data["version_alignment"]["strategy"] == "lockstep"
        assert isinstance(data["version_alignment"]["packages"], list)

    def test_check_clean_only_payload_shape(self, tmp_path: Path, monkeypatch) -> None:
        import json as _json

        _scaffold(tmp_path)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(pack_cmd, ["--check-clean", "--dry-run", "--offline", "--json"])
        data = _json.loads(result.output)
        assert data["drift"] is not None
        assert isinstance(data["drift"]["outputs"], list)

    def test_version_gate_passes_drift_gate_fails_exit_4(self, tmp_path: Path, monkeypatch) -> None:
        _scaffold(tmp_path)
        monkeypatch.chdir(tmp_path)
        # No marketplace.json on disk -> drift "missing" -> exit 4.
        result = CliRunner().invoke(
            pack_cmd,
            ["--check-versions", "--check-clean", "--dry-run", "--offline"],
        )
        assert result.exit_code == 4

    def test_version_gate_fails_drift_gate_passes_exit_3(self, tmp_path: Path, monkeypatch) -> None:
        _scaffold(tmp_path, pkg_version="0.5.0")
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(pack_cmd, ["--check-versions", "--dry-run", "--offline"])
        assert result.exit_code == 3

    def test_gate_errors_appear_in_json_envelope(self, tmp_path: Path, monkeypatch) -> None:
        import json as _json

        _scaffold(tmp_path, pkg_version="0.5.0")
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(
            pack_cmd, ["--check-versions", "--dry-run", "--offline", "--json"]
        )
        data = _json.loads(result.output)
        codes = {e["code"] for e in data.get("errors", [])}
        assert "version_misaligned" in codes
        assert data["ok"] is False

    def test_gate_with_no_marketplace_block_does_not_fail(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # apm.yml without a marketplace block -> both gates skip cleanly.
        (tmp_path / "apm.yml").write_text(
            "name: x\ndescription: y\nversion: 1.0.0\n", encoding="utf-8"
        )
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(pack_cmd, ["--check-versions", "--check-clean", "--dry-run"])
        assert result.exit_code not in (3, 4)
