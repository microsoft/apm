"""Regression tests for marketplace metadata enrichment outcomes."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from apm_cli.commands.pack import pack_cmd
from apm_cli.marketplace.builder import (
    BuildOptions,
    MetadataEnrichmentOutcome,
    MetadataEnrichmentResult,
    MarketplaceBuilder,
    ResolvedPackage,
)
from apm_cli.marketplace.drift_check import check_marketplace_drift
from apm_cli.marketplace.migration import load_marketplace_config


def _write_config(project_root: Path) -> None:
    """Create a marketplace with one pinned remote package."""
    (project_root / "apm.yml").write_text(
        """\
name: metadata-outcome
description: Metadata outcome regression fixture
version: 1.0.0
marketplace:
  owner:
    name: APM Tests
  packages:
    - name: remote-tool
      source: acme/remote-tool
      ref: 0123456789abcdef0123456789abcdef01234567
""",
        encoding="utf-8",
    )


def _resolved_package() -> ResolvedPackage:
    """Return the pinned package used by the test marketplace."""
    return ResolvedPackage(
        name="remote-tool",
        source_repo="acme/remote-tool",
        subdir=None,
        ref="0123456789abcdef0123456789abcdef01234567",
        sha="0123456789abcdef0123456789abcdef01234567",
        requested_version=None,
        tags=(),
        is_prerelease=False,
    )


def _builder(project_root: Path) -> MarketplaceBuilder:
    """Build a dry-run builder from the fixture configuration."""
    return MarketplaceBuilder.from_config(
        load_marketplace_config(project_root),
        project_root,
        options=BuildOptions(dry_run=True),
    )


def test_metadata_outcomes_preserve_success_empty_failed_offline_and_local(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Only failed and intentionally offline remote enrichment is uncertifiable."""
    _write_config(tmp_path)
    builder = _builder(tmp_path)
    remote = _resolved_package()
    local = ResolvedPackage(
        name="local-tool",
        source_repo="",
        subdir="./packages/local-tool",
        ref="",
        sha="",
        requested_version=None,
        tags=(),
        is_prerelease=False,
    )
    monkeypatch.setattr(
        builder,
        "_fetch_remote_metadata",
        lambda _pkg: None,
    )
    monkeypatch.setattr(
        builder,
        "_fetch_local_metadata_outcome",
        lambda _pkg: MetadataEnrichmentOutcome(local.name, "local"),
    )

    result = builder._prefetch_metadata((remote, local))

    assert result.certifiable
    assert result.warnings == ()
    assert [outcome.status for outcome in result.outcomes] == ["empty", "local"]


def test_drift_refuses_uncertifiable_metadata_before_comparing_output(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A failed fetch must not let --check-clean certify equal degraded JSON."""
    _write_config(tmp_path)
    builder = _builder(tmp_path)
    remote = _resolved_package()
    monkeypatch.setattr(builder, "resolve", lambda: type("Resolved", (), {"entries": (remote,)})())
    monkeypatch.setattr(
        builder,
        "_prefetch_metadata",
        lambda _resolved: MetadataEnrichmentResult(
            (MetadataEnrichmentOutcome("remote-tool", "failed", cause="timeout"),)
        ),
    )
    monkeypatch.setattr(
        builder,
        "compose_output",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not compare")),
    )

    report = check_marketplace_drift(builder, load_marketplace_config(tmp_path), tmp_path)

    assert not report.ok
    assert report.outputs[0].status == "uncertifiable"
    assert len(report.outputs[0].metadata_warnings) == 1
    assert "metadata enrichment failed (timeout)" in report.outputs[0].metadata_warnings[0]


def test_pack_json_warns_and_strict_metadata_prevents_writes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Default mode reports degraded metadata; strict mode refuses the artifact."""
    _write_config(tmp_path)

    monkeypatch.setattr(MarketplaceBuilder, "_ensure_auth", lambda _self: None)
    monkeypatch.setattr(
        MarketplaceBuilder,
        "_fetch_remote_metadata_outcome",
        lambda _self, pkg: MetadataEnrichmentOutcome(pkg.name, "failed", cause="transport closed"),
    )
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    warned = runner.invoke(pack_cmd, ["--json"])

    assert warned.exit_code == 0, warned.output
    payload = json.loads(warned.output)
    assert payload["metadata_enrichment"]["certifiable"] is False
    assert payload["metadata_enrichment"]["outcomes"] == [
        {"package": "remote-tool", "status": "failed", "cause": "transport closed"}
    ]
    assert len(payload["warnings"]) == 1
    artifact = tmp_path / ".claude-plugin" / "marketplace.json"
    assert artifact.is_file()

    uncertifiable = runner.invoke(pack_cmd, ["--check-clean", "--dry-run", "--json"])

    assert uncertifiable.exit_code == 4, uncertifiable.output
    uncertifiable_payload = json.loads(uncertifiable.output)
    assert uncertifiable_payload["drift"]["outputs"][0]["status"] == "uncertifiable"
    assert uncertifiable_payload["drift"]["outputs"][0]["metadata_warnings"] == payload["warnings"]
    assert uncertifiable_payload["errors"][0]["code"] == "marketplace_metadata_uncertifiable"
    artifact.unlink()

    strict = runner.invoke(pack_cmd, ["--strict-metadata", "--json"])

    assert strict.exit_code == 5, strict.output
    strict_payload = json.loads(strict.output)
    assert strict_payload["errors"][0]["code"] == "metadata_incomplete"
    assert strict_payload["metadata_enrichment"]["certifiable"] is False
    assert strict_payload["metadata_enrichment"]["outcomes"] == payload["metadata_enrichment"][
        "outcomes"
    ]
    assert strict_payload["warnings"] == payload["warnings"]
    assert not artifact.exists()
