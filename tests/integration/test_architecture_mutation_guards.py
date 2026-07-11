"""Integration guardrails for validate-before-mutate architecture."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def test_compiled_output_batch_scans_once_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A complete compile batch must cross one blocking scan chokepoint."""
    from apm_cli.compilation.output_writer import CompiledOutputWriter
    from apm_cli.security.gate import SecurityGate

    calls = 0
    real_scan = SecurityGate.scan_texts

    def counting_scan(contents, *, policy):
        nonlocal calls
        calls += 1
        return real_scan(contents, policy=policy)

    monkeypatch.setattr(SecurityGate, "scan_texts", counting_scan)
    outputs = {
        tmp_path / "AGENTS.md": "# agents\n",
        tmp_path / "nested" / "CLAUDE.md": "# claude\n",
    }

    CompiledOutputWriter().write_many(outputs)

    assert calls == 1
    assert all(path.read_text(encoding="utf-8") == content for path, content in outputs.items())


def test_invalid_hook_payload_writes_no_files(tmp_path: Path) -> None:
    """Native hook validation must fail before payload or script mutation."""
    from apm_cli.core.deployment_state import MaterializationStatus
    from apm_cli.integration.hook_integrator import HookIntegrator
    from apm_cli.models.apm_package import APMPackage, PackageInfo

    package_root = tmp_path / "apm_modules" / "owner" / "hooks"
    source_dir = package_root / "hooks"
    source_dir.mkdir(parents=True)
    (source_dir / "hooks.json").write_text(
        json.dumps({"version": 2, "hooks": {}}),
        encoding="utf-8",
    )
    (tmp_path / ".github").mkdir()
    package = APMPackage(name="invalid-hooks", version="1.0.0", package_path=package_root)

    result = HookIntegrator().integrate_package_hooks(
        PackageInfo(package=package, install_path=package_root),
        tmp_path,
    )

    hooks_dir = tmp_path / ".github" / "hooks"
    assert not list(hooks_dir.rglob("*"))
    assert result.files_integrated == 0
    assert len(result.materializations) == 1
    assert result.materializations[0].status is MaterializationStatus.FAILED


@pytest.mark.parametrize(
    "payload",
    [
        "lockfile_version: '99'\ndependencies: []\n",
        "lockfile_version: '2'\ndependencies: {}\n",
        "lockfile_version: '2'\ndependencies:\n  - not-a-mapping\n",
    ],
)
def test_lockfile_loader_fails_closed_on_unsupported_or_malformed_shape(
    payload: str,
) -> None:
    """Unknown versions and malformed containers must never downgrade."""
    from apm_cli.deps.lockfile import LockFile, LockfileFormatError

    with pytest.raises(LockfileFormatError):
        LockFile.from_yaml(payload)
