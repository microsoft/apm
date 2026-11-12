"""Functional regression coverage for deterministic generated bundle text."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from apm_cli.bundle.agent_plugin_exporter import export_agent_plugin_bundle
from apm_cli.bundle.packer import pack_bundle
from apm_cli.bundle.plugin_exporter import export_plugin_bundle
from apm_cli.core.plugin_manifest import write_plugin_manifest
from apm_cli.deps.lockfile import LockFile

pytestmark = pytest.mark.component


def _write_project(root: Path) -> Path:
    """Create the smallest project accepted by every bundle producer."""
    root.mkdir(parents=True)
    (root / "apm.yml").write_text(
        yaml.safe_dump(
            {
                "name": "lf-test",
                "version": "1.0.0",
                "description": "LF writer regression fixture",
            }
        ),
        encoding="utf-8",
    )
    LockFile().write(root / "apm.lock.yaml")
    return root


def _emulate_windows_text_translation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make Path.write_text translate newlines unless newline="" is explicit."""
    original = Path.write_text

    def windows_write_text(
        path: Path,
        data: str,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> int:
        if newline is None:
            data = data.replace("\r\n", "\n").replace("\n", "\r\n")
        return original(path, data, encoding=encoding, errors=errors, newline="")

    monkeypatch.setattr(Path, "write_text", windows_write_text)


def _assert_lf(path: Path) -> bytes:
    """Return file bytes after asserting canonical LF line endings."""
    content = path.read_bytes()
    assert b"\r" not in content
    assert b"\n" in content
    return content


def _assert_recorded_hashes(bundle: Path, generated_files: tuple[str, ...]) -> None:
    """Assert lockfile hashes cover the canonical bytes actually emitted."""
    lockfile = yaml.safe_load((bundle / "apm.lock.yaml").read_text(encoding="utf-8"))
    recorded = lockfile["pack"]["bundle_files"]
    for relative_path in generated_files:
        content = _assert_lf(bundle / relative_path)
        assert recorded[relative_path] == hashlib.sha256(content).hexdigest()


@pytest.mark.windows_compat
def test_apm_bundle_lockfile_uses_lf_under_windows_translation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _write_project(tmp_path / "project")
    _emulate_windows_text_translation(monkeypatch)

    result = pack_bundle(project, tmp_path / "build")

    _assert_lf(result.bundle_path / "apm.lock.yaml")


@pytest.mark.windows_compat
def test_claude_plugin_metadata_and_hashes_use_lf_under_windows_translation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _write_project(tmp_path / "project")
    hooks = project / ".apm" / "hooks" / "hooks.json"
    hooks.parent.mkdir(parents=True)
    hooks.write_text(json.dumps({"preCommit": ["lint"]}), encoding="utf-8")
    (project / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"demo": {"command": "demo"}}}),
        encoding="utf-8",
    )
    _emulate_windows_text_translation(monkeypatch)

    result = export_plugin_bundle(project, tmp_path / "build")

    _assert_lf(result.bundle_path / "apm.lock.yaml")
    _assert_recorded_hashes(
        result.bundle_path,
        ("hooks.json", ".mcp.json", "plugin.json"),
    )


@pytest.mark.windows_compat
def test_agent_plugin_metadata_and_hashes_use_lf_under_windows_translation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _write_project(tmp_path / "project")
    _emulate_windows_text_translation(monkeypatch)

    result = export_agent_plugin_bundle(project, tmp_path / "build")

    _assert_lf(result.bundle_path / "apm.lock.yaml")
    _assert_recorded_hashes(result.bundle_path, ("mcp.json", "plugin.json"))


@pytest.mark.windows_compat
def test_generated_plugin_manifest_uses_lf_under_windows_translation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _emulate_windows_text_translation(monkeypatch)

    output = write_plugin_manifest(
        tmp_path,
        {"name": "lf-test", "version": "1.0.0"},
        "claude",
    )

    assert output is not None
    assert _assert_lf(output).endswith(b"}\n")
