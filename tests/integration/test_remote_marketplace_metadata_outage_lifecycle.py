"""Installed-binary lifecycle proof for remote marketplace metadata outages."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
    pytest.mark.requires_network_integration,
]

_ARTIFACT = ".claude-plugin/marketplace.json"
_SHA = "09c4bf708d787eba45046904ab5b40b9ac597c6b"


def _evidence(result: CommandResult) -> str:
    """Return process evidence for a failed lifecycle assertion."""
    return (
        f"cwd={result.cwd!s}\n"
        f"command={result.command!r}\n"
        f"returncode={result.returncode}\n"
        f"stdout={result.stdout!r}\n"
        f"stderr={result.stderr!r}"
    )


def _write_project(project_root: Path) -> None:
    """Create the pinned remote marketplace fixture from issue #2524."""
    (project_root / "apm.yml").write_text(
        f"""\
name: remote-metadata-outage
description: Remote metadata outage lifecycle fixture
version: 1.0.0
dependencies: {{}}
marketplace:
  owner:
    name: APM Lifecycle Tests
  outputs: [codex, claude]
  packages:
    - name: adapt-nifi-flows-to-2-x
      source: Netcracker/qubership-nifi
      subdir: agent-packages/adapt-nifi-flows-to-2-x
      ref: {_SHA}
      category: Productivity
""",
        encoding="utf-8",
    )


def _blocked_transport_environment() -> dict[str, str]:
    """Return an environment that deterministically rejects HTTPS metadata fetches."""
    environment = dict(os.environ)
    for name in ("https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY", "no_proxy", "NO_PROXY"):
        environment.pop(name, None)
    environment.update(
        {
            "https_proxy": "http://127.0.0.1:9",
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "ALL_PROXY": "http://127.0.0.1:9",
        }
    )
    return environment


def _bundle_snapshot(bundle_path: Path) -> dict[Path, bytes]:
    """Capture a bundle's file content so failure paths cannot hide mutation."""
    return {
        path.relative_to(bundle_path): path.read_bytes()
        for path in bundle_path.rglob("*")
        if path.is_file()
    }


def test_remote_metadata_outage_never_certifies_degraded_marketplace(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """An outage warns in dry-run and makes --check-clean exit 4 before equality."""
    project_root = tmp_path / "remote-metadata-outage"
    project_root.mkdir()
    _write_project(project_root)
    (project_root / "apm.lock.yaml").write_text("dependencies: []\n", encoding="utf-8")
    runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=300)
    available_environment = dict(os.environ)

    seeded = runner.run(
        ("pack",),
        scenario_id="remote-metadata-outage-seed",
        cwd=project_root,
        env=available_environment,
    )
    assert seeded.returncode == 0, _evidence(seeded)
    artifact = project_root / _ARTIFACT
    on_disk = json.loads(artifact.read_text(encoding="utf-8"))
    plugin = on_disk["plugins"][0]
    assert plugin["description"]
    assert plugin["version"]
    plugin.pop("description")
    plugin.pop("version")
    artifact.write_text(json.dumps(on_disk, indent=2) + "\n", encoding="utf-8")
    truncated_bytes = artifact.read_bytes()
    codex_artifact = project_root / ".agents" / "plugins" / "marketplace.json"
    codex_bytes = codex_artifact.read_bytes()
    bundle_path = project_root / "build" / "remote-metadata-outage-1.0.0"
    bundle_snapshot = _bundle_snapshot(bundle_path)

    dry_run, uncertifiable = runner.run_sequence(
        (
            ("pack", "--dry-run"),
            ("pack", "--check-clean", "--dry-run"),
        ),
        expected_returncodes=(0, 4),
        scenario_id="remote-metadata-outage-gate",
        cwd=project_root,
        env=_blocked_transport_environment(),
    )

    assert "metadata enrichment failed" in (dry_run.stdout + dry_run.stderr)
    assert "adapt-nifi-flows-to-2-x" in (dry_run.stdout + dry_run.stderr)
    assert artifact.read_bytes() == truncated_bytes
    assert codex_artifact.read_bytes() == codex_bytes
    assert _bundle_snapshot(bundle_path) == bundle_snapshot
    assert "cannot certify regenerated metadata" in (uncertifiable.stdout + uncertifiable.stderr)

    strict, strict_retry = runner.run_sequence(
        (
            ("pack", "--strict-metadata"),
            ("pack", "--strict-metadata"),
        ),
        expected_returncodes=(5, 5),
        scenario_id="remote-metadata-outage-strict",
        cwd=project_root,
        env=_blocked_transport_environment(),
    )

    assert "metadata enrichment failed" in (strict.stdout + strict.stderr)
    assert "metadata enrichment failed" in (strict_retry.stdout + strict_retry.stderr)
    assert artifact.read_bytes() == truncated_bytes
    assert codex_artifact.read_bytes() == codex_bytes
    assert _bundle_snapshot(bundle_path) == bundle_snapshot

    restored = runner.run(
        ("pack", "--check-clean", "--dry-run"),
        scenario_id="remote-metadata-outage-restored",
        cwd=project_root,
        env=available_environment,
    )
    assert restored.returncode == 4, _evidence(restored)
    restored_output = restored.stdout + restored.stderr
    assert "plugins[0].description" in restored_output
    assert "plugins[0].version" in restored_output
