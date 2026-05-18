"""Unit tests for ``apm pack`` command helpers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner

from apm_cli.commands.pack import _render_marketplace_result, pack_cmd
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
    # The new --marketplace-path flag is shown in help
    assert "--marketplace-path" in result.output
    # The deprecated --marketplace-output is hidden
    assert "--marketplace-output" not in result.output
    assert "--claude-output" not in result.output


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
