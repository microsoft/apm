"""Regression tests for marketplace metadata enrichment outcomes."""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.parse
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from apm_cli.commands.pack import pack_cmd
from apm_cli.core.build_orchestrator import (
    BuildOptions as OrchestratorBuildOptions,
)
from apm_cli.core.build_orchestrator import (
    BuildOrchestrator,
    MarketplaceProducer,
    MetadataEnrichmentError,
    OutputKind,
    ProducerResult,
)
from apm_cli.marketplace.builder import (
    BuildOptions,
    MarketplaceBuilder,
    MetadataEnrichmentOutcome,
    MetadataEnrichmentResult,
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
      category: Productivity
""",
        encoding="utf-8",
    )


def _write_manifestless_range_config(project_root: Path) -> None:
    """Create the manifestless marketplace fixture from issue #3055."""
    (project_root / "apm.yml").write_text(
        """\
name: generic-marketplace
description: Manifestless metadata reproduction
version: 0.1.0
marketplace:
  owner:
    name: public-repro
    url: https://github.com/public-repro
  build:
    tagPattern: "v{version}"
  outputs:
    claude: {}
  packages:
    - name: brainstorming
      description: Public brainstorming skill
      source: obra/superpowers
      subdir: skills/brainstorming
      version: "^4.0.0"
""",
        encoding="utf-8",
    )


def _resolved_range_refs(
    *_args: object,
    **_kwargs: object,
) -> subprocess.CompletedProcess[str]:
    """Return one compatible remote tag without invoking Git."""
    resolved_sha = "a" * 40
    return subprocess.CompletedProcess(
        args=["git", "ls-remote"],
        returncode=0,
        stdout=f"{resolved_sha}\trefs/tags/v4.3.1\n",
        stderr="",
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


def test_metadata_outcome_mapping_uses_the_precomputed_package_index() -> None:
    """Mapper lookups do not rescan every preserved metadata outcome."""
    result = MetadataEnrichmentResult(
        (
            MetadataEnrichmentOutcome("first", "fetched", (("version", "1.0.0"),)),
            MetadataEnrichmentOutcome("second", "empty"),
        )
    )

    assert dict(result) == {"first": {"version": "1.0.0"}}
    assert result["first"] == {"version": "1.0.0"}


def test_metadata_outcomes_reject_unknown_statuses() -> None:
    """Unknown future states cannot silently become certifiable."""
    with pytest.raises(ValueError, match="unknown metadata enrichment status"):
        MetadataEnrichmentOutcome("remote-tool", "future")  # type: ignore[arg-type]


def test_explicit_metadata_certifies_non_github_package(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Fixed curator fields certify hosts without metadata fetch support."""
    _write_config(tmp_path)
    builder = _builder(tmp_path)
    remote = ResolvedPackage(
        name="remote-tool",
        source_repo="acme/remote-tool",
        subdir=None,
        ref="v1.0.0",
        sha="0123456789abcdef0123456789abcdef01234567",
        requested_version="1.0.0",
        tags=(),
        is_prerelease=False,
        host="gitlab.example.com",
        curator_metadata=(
            ("description", "Curated description"),
            ("version", "1.0.0"),
        ),
    )
    fetch = MagicMock(side_effect=AssertionError("must not fetch"))
    monkeypatch.setattr(builder, "_fetch_remote_metadata", fetch)

    result = builder._prefetch_metadata((remote,))

    fetch.assert_not_called()
    assert result.certifiable
    assert result.outcomes[0].status == "explicit"
    assert result["remote-tool"] == {
        "description": "Curated description",
        "version": "1.0.0",
    }


def test_metadata_failure_cause_never_exposes_exception_secrets(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Raw exception text must not enter warnings or JSON."""
    _write_config(tmp_path)
    builder = _builder(tmp_path)
    secret = "github_pat_secret_material_1234567890"
    auth_resolver = MagicMock()
    auth_resolver.uses_public_github_anonymous_first.return_value = True
    auth_resolver.try_with_fallback.side_effect = RuntimeError(f"Authorization: Bearer {secret}")
    builder._auth_resolver = auth_resolver

    outcome = builder._fetch_remote_metadata_outcome(_resolved_package())
    result = MetadataEnrichmentResult((outcome,))

    assert outcome.cause == "RuntimeError"
    assert secret not in json.dumps(result.to_json_dict())
    assert secret not in " ".join(result.warnings)


def test_marketplace_none_skips_resolution(tmp_path: Path, monkeypatch) -> None:
    """An explicit marketplace opt-out performs no resolution or fetch."""
    _write_config(tmp_path)
    monkeypatch.setattr(
        MarketplaceBuilder,
        "resolve",
        lambda _self: (_ for _ in ()).throw(AssertionError("must not resolve")),
    )
    options = OrchestratorBuildOptions(
        project_root=tmp_path,
        apm_yml_path=tmp_path / "apm.yml",
        marketplace_formats=(),
        dry_run=True,
    )

    result = BuildOrchestrator(producers=[MarketplaceProducer()]).run(options)

    assert result.outputs == []
    assert result.producer_results[0].payload.outputs == ()


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
    assert (
        strict_payload["metadata_enrichment"]["outcomes"]
        == payload["metadata_enrichment"]["outcomes"]
    )
    assert strict_payload["warnings"] == payload["warnings"]
    assert not artifact.exists()


def test_pack_check_clean_certifies_manifestless_skill_with_version_range(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A verified remote SKILL.md makes an absent apm.yml certifiable."""
    _write_manifestless_range_config(tmp_path)
    resolved_sha = "a" * 40

    requested_urls: list[tuple[str | None, str, str | None]] = []

    def fake_urlopen(
        request: urllib.request.Request,
        *,
        timeout: int,
    ) -> BytesIO:
        """Return the issue fixture's remote package documents."""
        assert timeout == 5
        parsed = urllib.parse.urlparse(request.full_url)
        requested_urls.append((parsed.hostname, parsed.path, request.get_header("Authorization")))
        if parsed.path.endswith("/apm.yml"):
            raise urllib.error.HTTPError(
                url=request.full_url,
                code=404,
                msg="Not Found",
                hdrs=None,
                fp=None,
            )
        if parsed.path.endswith("/SKILL.md"):
            return BytesIO(
                b"---\nname: brainstorming\ndescription: Public brainstorming skill\n---\n"
            )
        raise AssertionError(f"unexpected metadata URL path: {parsed.path}")

    monkeypatch.setenv("GITHUB_APM_PAT", "test-token")
    monkeypatch.setattr("apm_cli.utils.git_env.git_remote_refs", _resolved_range_refs)
    monkeypatch.setattr("apm_cli.marketplace.builder.urllib.request.urlopen", fake_urlopen)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    packed = runner.invoke(pack_cmd, [])
    checked = runner.invoke(pack_cmd, ["--check-clean", "--dry-run", "--json"])

    assert packed.exit_code == 0, packed.output
    assert checked.exit_code == 0, checked.output
    assert "metadata enrichment failed" not in (packed.output + checked.output)
    payload = json.loads(checked.output)
    assert payload["metadata_enrichment"] == {
        "certifiable": True,
        "outcomes": [{"package": "brainstorming", "status": "manifestless"}],
    }
    expected_base = f"/obra/superpowers/{resolved_sha}/skills/brainstorming"
    assert requested_urls == [
        ("raw.githubusercontent.com", f"{expected_base}/apm.yml", None),
        ("raw.githubusercontent.com", f"{expected_base}/SKILL.md", None),
        ("raw.githubusercontent.com", f"{expected_base}/apm.yml", None),
        ("raw.githubusercontent.com", f"{expected_base}/SKILL.md", None),
    ]


def test_pack_check_clean_rejects_missing_manifestless_skill_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing apm.yml remains uncertifiable when SKILL.md is also absent."""
    _write_manifestless_range_config(tmp_path)
    requested_urls: list[tuple[str | None, str]] = []

    def fake_urlopen(
        request: urllib.request.Request,
        *,
        timeout: int,
    ) -> BytesIO:
        """Return 404 for every manifest and skill marker probe."""
        assert timeout == 5
        parsed = urllib.parse.urlparse(request.full_url)
        requested_urls.append((parsed.hostname, parsed.path))
        raise urllib.error.HTTPError(
            url=request.full_url,
            code=404,
            msg="Not Found",
            hdrs=None,
            fp=None,
        )

    monkeypatch.setenv("GITHUB_APM_PAT", "test-token")
    monkeypatch.setattr("apm_cli.utils.git_env.git_remote_refs", _resolved_range_refs)
    monkeypatch.setattr("apm_cli.marketplace.builder.urllib.request.urlopen", fake_urlopen)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    packed = runner.invoke(pack_cmd, [])
    checked = runner.invoke(pack_cmd, ["--check-clean", "--dry-run", "--json"])

    assert packed.exit_code == 0, packed.output
    assert checked.exit_code == 4, checked.output
    payload = json.loads(checked.output)
    assert payload["metadata_enrichment"]["certifiable"] is False
    assert payload["metadata_enrichment"]["outcomes"] == [
        {"package": "brainstorming", "status": "failed", "cause": "HTTP 404"}
    ]
    assert {hostname for hostname, _path in requested_urls} == {
        "api.github.com",
        "raw.githubusercontent.com",
    }
    assert {Path(path).name for _hostname, path in requested_urls} == {
        "SKILL.md",
        "apm.yml",
    }


@pytest.mark.parametrize(
    ("metadata_error", "expected_cause"),
    [
        (PermissionError("authentication required"), "authentication required"),
        (urllib.error.URLError("connection refused"), "network request failed"),
    ],
)
def test_pack_check_clean_rejects_metadata_access_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    metadata_error: Exception,
    expected_cause: str,
) -> None:
    """Authentication and network failures remain uncertifiable."""
    _write_manifestless_range_config(tmp_path)

    def fake_urlopen(
        request: urllib.request.Request,
        *,
        timeout: int,
    ) -> BytesIO:
        """Raise the selected external metadata-access failure."""
        del request
        assert timeout == 5
        raise metadata_error

    monkeypatch.setattr("apm_cli.utils.git_env.git_remote_refs", _resolved_range_refs)
    monkeypatch.setattr("apm_cli.marketplace.builder.urllib.request.urlopen", fake_urlopen)
    monkeypatch.chdir(tmp_path)

    checked = CliRunner().invoke(pack_cmd, ["--check-clean", "--dry-run", "--json"])

    assert checked.exit_code == 4, checked.output
    payload = json.loads(checked.output)
    assert payload["metadata_enrichment"]["outcomes"] == [
        {"package": "brainstorming", "status": "failed", "cause": expected_cause}
    ]


@pytest.mark.parametrize(
    ("remote_result", "expected_error"),
    [
        (
            subprocess.CompletedProcess(
                args=["git", "ls-remote"],
                returncode=128,
                stdout="",
                stderr="fatal: repository not found",
            ),
            "repository not found during ls-remote",
        ),
        (
            subprocess.CompletedProcess(
                args=["git", "ls-remote"],
                returncode=0,
                stdout=("b" * 40) + "\trefs/tags/v5.0.0\n",
                stderr="",
            ),
            "No tag matching version '^4.0.0'",
        ),
    ],
)
def test_pack_check_clean_rejects_invalid_remote_or_ref(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    remote_result: subprocess.CompletedProcess[str],
    expected_error: str,
) -> None:
    """Missing repositories and incompatible refs fail before certification."""
    _write_manifestless_range_config(tmp_path)

    def fake_remote_refs(
        *_args: object,
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        """Return the selected repository or ref resolution failure."""
        return remote_result

    monkeypatch.setattr("apm_cli.utils.git_env.git_remote_refs", fake_remote_refs)
    monkeypatch.chdir(tmp_path)

    checked = CliRunner().invoke(pack_cmd, ["--check-clean", "--dry-run"])

    assert checked.exit_code == 1, checked.output
    assert expected_error in checked.output


def test_check_clean_never_writes_before_reporting_uncertifiable_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The clean gate must not overwrite an artifact before reporting failure."""
    _write_config(tmp_path)
    monkeypatch.setattr(MarketplaceBuilder, "_ensure_auth", lambda _self: None)
    monkeypatch.setattr(
        MarketplaceBuilder,
        "_fetch_remote_metadata_outcome",
        lambda _self, pkg: MetadataEnrichmentOutcome(pkg.name, "failed", cause="transport closed"),
    )
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    initial = runner.invoke(pack_cmd, [])

    assert initial.exit_code == 0, initial.output
    artifact = tmp_path / ".claude-plugin" / "marketplace.json"
    artifact.write_text('{"sentinel": "unchanged"}\n', encoding="utf-8")
    before = artifact.read_bytes()

    clean_check = runner.invoke(pack_cmd, ["--check-clean"])

    assert clean_check.exit_code == 4, clean_check.output
    assert artifact.read_bytes() == before


def test_explicit_clean_check_dry_run_reports_uncertifiable_metadata_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """An uncertifiable preview must not repeat warnings or promise a write."""
    _write_config(tmp_path)
    monkeypatch.setattr(MarketplaceBuilder, "_ensure_auth", lambda _self: None)
    monkeypatch.setattr(
        MarketplaceBuilder,
        "_fetch_remote_metadata_outcome",
        lambda _self, pkg: MetadataEnrichmentOutcome(pkg.name, "failed", cause="transport closed"),
    )
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(pack_cmd, ["--check-clean", "--dry-run"])

    assert result.exit_code == 4, result.output
    assert result.output.count("metadata enrichment failed") == 1
    assert "[dry-run] Would write marketplace.json" not in result.output


def test_pack_json_reuses_build_metadata_for_clean_check(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The clean gate must compare the exact metadata snapshot built once."""
    _write_config(tmp_path)
    fetched = MetadataEnrichmentOutcome(
        "remote-tool",
        "fetched",
        values=(("description", "available"), ("version", "1.0.0")),
    )
    monkeypatch.setattr(MarketplaceBuilder, "_ensure_auth", lambda _self: None)
    fetch = MagicMock(return_value=fetched)
    monkeypatch.setattr(
        MarketplaceBuilder,
        "_fetch_remote_metadata_outcome",
        fetch,
    )
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(pack_cmd, ["--check-clean", "--dry-run", "--json"])

    assert result.exit_code == 4, result.output
    payload = json.loads(result.output)
    assert fetch.call_count == 1
    assert payload["metadata_enrichment"]["certifiable"] is True
    assert payload["metadata_enrichment"]["outcomes"] == [
        {"package": "remote-tool", "status": "fetched"}
    ]
    assert payload["errors"][0]["code"] == "marketplace_drift"


def test_strict_metadata_preflight_prevents_bundle_and_marketplace_mutations(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Strict metadata failure must precede every requested artifact producer."""
    _write_config(tmp_path)
    config_path = tmp_path / "apm.yml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "  packages:", "  outputs: [codex, claude]\n  packages:"
        )
        + "dependencies: {}\n",
        encoding="utf-8",
    )
    bundle = MagicMock(spec=["kind", "produce"])
    bundle.kind = OutputKind.BUNDLE
    bundle.produce.return_value = ProducerResult(kind=OutputKind.BUNDLE)
    marketplace = MarketplaceProducer()
    monkeypatch.setattr(MarketplaceBuilder, "_ensure_auth", lambda _self: None)
    monkeypatch.setattr(
        MarketplaceBuilder,
        "_fetch_remote_metadata_outcome",
        lambda _self, pkg: MetadataEnrichmentOutcome(pkg.name, "failed", cause="transport closed"),
    )
    options = OrchestratorBuildOptions(
        project_root=tmp_path,
        apm_yml_path=tmp_path / "apm.yml",
        marketplace_strict_metadata=True,
    )

    for _ in range(2):
        with pytest.raises(MetadataEnrichmentError, match="metadata enrichment failed"):
            BuildOrchestrator(producers=[bundle, marketplace]).run(options)
        assert not (tmp_path / ".claude-plugin" / "marketplace.json").exists()
        assert not (tmp_path / ".agents" / "plugins" / "marketplace.json").exists()

    bundle.produce.assert_not_called()
