"""Integration guardrails for neutral IR and explicit schema contracts."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_neutral_hook_intent_translates_at_native_edges() -> None:
    """One portable timeout must render in each target's native unit."""
    from apm_cli.integration.hook_native_formats import (
        _to_antigravity_hook_entries,
        _to_gemini_hook_entries,
    )

    source = [{"command": "echo ok", "timeoutSec": 3}]

    gemini = _to_gemini_hook_entries(source)
    antigravity = _to_antigravity_hook_entries(source, "PreInvocation")

    assert gemini[0]["hooks"][0]["timeout"] == 3000
    assert antigravity[0]["timeout"] == 3


def test_manifest_schema_negotiates_normative_v01_registry_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit v0.1 identity must select its normative registry parser."""
    from apm_cli.models.apm_package import APMPackage
    from apm_cli.models.manifest_contract import OPENAPM_V01_SCHEMA_URI

    monkeypatch.setattr(
        "apm_cli.deps.registry.feature_gate.require_package_registry_enabled",
        lambda _feature: None,
    )
    manifest = tmp_path / "apm.yml"
    manifest.write_text(
        "\n".join(
            (
                f"$schema: {OPENAPM_V01_SCHEMA_URI}",
                "name: demo",
                "version: 1.0.0",
                "registries:",
                "  corp: https://registry.example.test/apm",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    package = APMPackage.from_apm_yml(manifest)

    assert package.manifest_contract == "openapm-v0.1"
    assert package.registries == {"corp": "https://registry.example.test/apm"}
    assert package.default_registry is None


def test_unknown_manifest_schema_identity_fails_closed(tmp_path: Path) -> None:
    """A future schema cannot be silently interpreted as the working draft."""
    from apm_cli.models.apm_package import APMPackage
    from apm_cli.models.manifest_contract import UnsupportedManifestContractError

    manifest = tmp_path / "apm.yml"
    manifest.write_text(
        "$schema: https://example.test/openapm-v9.json\nname: demo\nversion: 1.0.0\n",
        encoding="utf-8",
    )

    with pytest.raises(UnsupportedManifestContractError):
        APMPackage.from_apm_yml(manifest)


def test_lifecycle_docs_match_explicit_compilation_contract() -> None:
    """The lifecycle page must state the same install/compile ownership."""
    lifecycle = (
        Path(__file__).parents[2]
        / "docs"
        / "src"
        / "content"
        / "docs"
        / "concepts"
        / "lifecycle.md"
    ).read_text(encoding="utf-8")

    assert "does not run aggregate\ncompilation" in lifecycle
