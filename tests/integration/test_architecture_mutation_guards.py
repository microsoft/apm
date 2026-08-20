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


@pytest.mark.parametrize(
    "payload",
    (
        {"version": 2, "hooks": {}},
        {"version": 1, "hooks": {"PreToolUse": "not-a-list"}},
    ),
)
def test_invalid_hook_payload_writes_no_files(
    tmp_path: Path,
    payload: dict,
) -> None:
    """Native hook validation must fail before payload or script mutation."""
    from apm_cli.core.deployment_state import MaterializationStatus
    from apm_cli.integration.hook_integrator import HookIntegrator
    from apm_cli.models.apm_package import APMPackage, PackageInfo

    package_root = tmp_path / "apm_modules" / "owner" / "hooks"
    source_dir = package_root / "hooks"
    source_dir.mkdir(parents=True)
    (source_dir / "hooks.json").write_text(
        json.dumps(payload),
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
        (
            "lockfile_version: '1'\n"
            "dependencies: []\n"
            "deployments:\n"
            "  - kind: project-relative\n"
            "    target: copilot\n"
            "    value: .github/demo.md\n"
            "    scope: project\n"
            "    owners: []\n"
            "    active_owner: ''\n"
        ),
    ],
)
def test_lockfile_loader_fails_closed_on_unsupported_or_malformed_shape(
    payload: str,
) -> None:
    """Unknown versions and malformed containers must never downgrade."""
    from apm_cli.deps.lockfile import LockFile, LockfileFormatError

    with pytest.raises(LockfileFormatError):
        LockFile.from_yaml(payload)


def test_optional_null_lockfile_mappings_remain_backward_compatible() -> None:
    """Strict validation must normalize legacy null optional mappings."""
    from apm_cli.deps.lockfile import LockFile

    lockfile = LockFile.from_yaml(
        "lockfile_version: '1'\ndependencies: []\nmcp_configs: null\nlsp_configs: null\n"
    )

    assert lockfile.mcp_configs == {}
    assert lockfile.lsp_configs == {}


def test_transitive_local_identity_round_trips_through_lockfile(tmp_path: Path) -> None:
    """Parent and anchored path provenance must survive persistence."""
    from apm_cli.deps.lockfile import LockedDependency, LockFile

    dependency = LockedDependency(
        repo_url="_local/shared",
        source="local",
        local_path="../shared",
        declaring_parent="owner/parent#main",
        anchored_local_path=str(tmp_path / "workspace" / "shared"),
    )
    lock_path = tmp_path / "apm.lock.yaml"
    LockFile(dependencies={dependency.get_unique_key(): dependency}).write(lock_path)

    loaded = LockFile.read(lock_path)

    assert loaded is not None
    restored = loaded.get_all_dependencies()[0]
    assert restored.declaring_parent == "owner/parent#main"
    assert restored.anchored_local_path == str(tmp_path / "workspace" / "shared")
