"""Integration guardrails for neutral IR and explicit schema contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def _write_portable_hook_package(tmp_path: Path) -> object:
    """Create one package carrying the flat portable hook shape."""
    from apm_cli.models.apm_package import APMPackage, PackageInfo

    package_root = tmp_path / "apm_modules" / "owner" / "portable-hooks"
    hooks_dir = package_root / ".apm" / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "check.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "type": "command",
                            "command": "echo check",
                            "timeout": 10,
                            "matcher": "fs_write|str_replace",
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    return PackageInfo(
        package=APMPackage(name="portable-hooks", version="1.0.0"),
        install_path=package_root,
    )


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


def test_claude_edge_nests_flat_portable_hook_entries(tmp_path: Path) -> None:
    """Claude output must wrap portable handlers in matcher/hooks entries."""
    from apm_cli.integration.hook_integrator import HookIntegrator
    from apm_cli.integration.targets import KNOWN_TARGETS

    (tmp_path / ".claude").mkdir()
    package_info = _write_portable_hook_package(tmp_path)

    HookIntegrator().integrate_hooks_for_target(
        KNOWN_TARGETS["claude"],
        package_info,
        tmp_path,
    )

    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert settings["hooks"]["PreToolUse"] == [
        {
            "matcher": "fs_write|str_replace",
            "hooks": [
                {
                    "type": "command",
                    "command": "echo check",
                    "timeout": 10,
                }
            ],
        }
    ]


def test_kiro_edge_emits_current_v1_hook_schema(tmp_path: Path) -> None:
    """Kiro output must use v1 trigger/matcher/action documents."""
    from apm_cli.integration.hook_integrator import HookIntegrator
    from apm_cli.integration.targets import KNOWN_TARGETS

    (tmp_path / ".kiro").mkdir()
    package_info = _write_portable_hook_package(tmp_path)

    result = HookIntegrator().integrate_hooks_for_target(
        KNOWN_TARGETS["kiro"],
        package_info,
        tmp_path,
    )

    assert len(result.target_paths) == 1
    payload = json.loads(result.target_paths[0].read_text(encoding="utf-8"))
    assert payload == {
        "version": "v1",
        "hooks": [
            {
                "name": "portable-hooks PreToolUse 1",
                "trigger": "PreToolUse",
                "matcher": "fs_write|str_replace",
                "action": {
                    "type": "command",
                    "command": "echo check",
                    "timeout": 10,
                },
            }
        ],
    }


def test_neutral_hook_ir_snapshots_metadata() -> None:
    """Frozen portable intent must not alias mutable native dictionaries."""
    from apm_cli.integration.hook_ir import HookHandler

    metadata = {"args": ["one"]}
    handler = HookHandler(command="echo ok", metadata=metadata)
    metadata["args"].append("two")

    assert handler.metadata["args"] == ("one",)
    with pytest.raises(TypeError):
        handler.metadata["new"] = "value"


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
                "  default: internal",
                "  internal: https://registry.example.test/apm",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    package = APMPackage.from_apm_yml(manifest)

    assert package.manifest_contract == "openapm-v0.1"
    assert package.registries == {"internal": "https://registry.example.test/apm"}
    assert package.default_registry == "internal"


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
