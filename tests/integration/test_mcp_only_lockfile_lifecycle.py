"""Installed-binary lifecycle contracts for MCP-only lock state."""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.artifact_snapshot import ArtifactSnapshot, assert_unchanged
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.lifecycle_state import LifecycleStateSnapshot
from tests.utils.local_package import LocalPackage, LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]

_CONFIG_PATH = PurePosixPath(".cursor/mcp.json")
_AUDIT_ARGS = ("audit", "--ci", "--no-policy", "--format", "json")
_MCP_DEPENDENCY = {
    "name": "fixture-mcp",
    "registry": False,
    "transport": "stdio",
    "command": "printf",
    "args": ["fixture"],
}


def _scenario(
    root: Path,
    apm_binary_path: Path,
    *,
    with_mcp: bool = True,
) -> tuple[
    IsolatedApmEnvironment,
    dict[str, str],
    LocalPackage,
    ApmLifecycleRunner,
]:
    isolated = IsolatedApmEnvironment.create(root, base_env=dict(os.environ))
    project = LocalPackageFactory(isolated.work_root).create(
        "mcp-only-consumer",
        mcp_dependencies=(_MCP_DEPENDENCY,) if with_mcp else (),
        targets=("cursor",),
    )
    (project.root / ".cursor").mkdir()
    runner = ApmLifecycleRunner(
        (str(apm_binary_path),),
        timeout_seconds=60,
        scenario_timeout_seconds=180,
    )
    return isolated, isolated.subprocess_env(), project, runner


def _snapshot(project: LocalPackage) -> LifecycleStateSnapshot:
    return LifecycleStateSnapshot.capture(
        project.root,
        config_paths=(_CONFIG_PATH,),
    )


def _assert_same_state(
    expected: LifecycleStateSnapshot,
    actual: LifecycleStateSnapshot,
) -> None:
    assert actual.manifest_bytes == expected.manifest_bytes
    assert actual.lockfile_bytes == expected.lockfile_bytes
    assert actual.deployment_records == expected.deployment_records
    assert actual.mcp_state_bytes == expected.mcp_state_bytes
    assert actual.lsp_state_bytes == expected.lsp_state_bytes
    assert actual.files == expected.files
    assert actual.semantic_bytes == expected.semantic_bytes


def _assert_success(result: CommandResult) -> None:
    assert result.returncode == 0, (
        f"command={result.command!r}\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


def _audit(
    runner: ApmLifecycleRunner,
    project: LocalPackage,
    environment: dict[str, str],
    *,
    scenario_id: str,
) -> dict[str, object]:
    result = runner.run(
        _AUDIT_ARGS,
        scenario_id=scenario_id,
        cwd=project.root,
        env=environment,
    )
    _assert_success(result)
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict)
    assert payload["passed"] is True
    return payload


def test_mcp_only_install_audit_and_repeat_are_byte_identical(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    isolated, environment, project, runner = _scenario(tmp_path / "converge", apm_binary_path)
    install_args = ("install", "--target", "cursor", "--no-policy")

    first_install = runner.run(
        install_args,
        scenario_id="mcp-only-initial-install",
        cwd=project.root,
        env=environment,
    )
    _assert_success(first_install)
    first = _snapshot(project)
    first_cache = ArtifactSnapshot.capture(isolated.cache_root)
    assert first.lockfile_bytes is not None
    assert b"fixture-mcp" in first.mcp_state_bytes
    assert first.file(_CONFIG_PATH.as_posix()).kind == "file"
    _audit(
        runner,
        project,
        environment,
        scenario_id="mcp-only-initial-audit",
    )

    repeated_install = runner.run(
        install_args,
        scenario_id="mcp-only-repeated-install",
        cwd=project.root,
        env=environment,
    )
    _assert_success(repeated_install)
    repeated = _snapshot(project)
    _assert_same_state(first, repeated)
    assert_unchanged(first_cache, ArtifactSnapshot.capture(isolated.cache_root))


@pytest.mark.parametrize(
    "install_suffix",
    (
        (),
        ("--only", "mcp"),
    ),
    ids=("default", "only-mcp"),
)
def test_frozen_missing_lock_fails_without_durable_writes(
    tmp_path: Path,
    apm_binary_path: Path,
    install_suffix: tuple[str, ...],
) -> None:
    isolated, environment, project, runner = _scenario(
        tmp_path / f"missing-{'-'.join(install_suffix) or 'default'}",
        apm_binary_path,
    )
    before = _snapshot(project)
    cache_before = ArtifactSnapshot.capture(isolated.cache_root)

    result = runner.run(
        ("install", "--frozen", "--target", "cursor", "--no-policy", *install_suffix),
        scenario_id=f"mcp-only-frozen-missing-{'-'.join(install_suffix) or 'default'}",
        cwd=project.root,
        env=environment,
    )

    assert result.returncode != 0
    assert "--frozen requires apm.lock.yaml to exist" in result.stdout
    _assert_same_state(before, _snapshot(project))
    assert_unchanged(cache_before, ArtifactSnapshot.capture(isolated.cache_root))


def test_frozen_dedicated_mcp_add_fails_before_manifest_or_config_write(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    isolated, environment, project, runner = _scenario(
        tmp_path / "dedicated-add",
        apm_binary_path,
        with_mcp=False,
    )
    before = _snapshot(project)
    cache_before = ArtifactSnapshot.capture(isolated.cache_root)

    result = runner.run(
        (
            "install",
            "--frozen",
            "--mcp",
            "fixture-added",
            "--target",
            "cursor",
            "--",
            "printf",
            "fixture",
        ),
        scenario_id="mcp-only-frozen-dedicated-add",
        cwd=project.root,
        env=environment,
    )

    assert result.returncode != 0
    assert "--frozen cannot be combined with --mcp" in result.stdout + result.stderr
    _assert_same_state(before, _snapshot(project))
    assert_unchanged(cache_before, ArtifactSnapshot.capture(isolated.cache_root))


def test_frozen_local_bundle_fails_before_deployment_or_lock_write(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    isolated, environment, project, runner = _scenario(
        tmp_path / "local-bundle",
        apm_binary_path,
        with_mcp=False,
    )
    bundle = isolated.package_root / "fixture-bundle"
    bundle.mkdir()
    (bundle / "plugin.json").write_text(
        json.dumps({"id": "fixture-bundle", "name": "Fixture Bundle"}),
        encoding="ascii",
    )
    dump_yaml(
        {
            "pack": {
                "format": "plugin",
                "target": "cursor",
                "bundle_files": {},
            },
            "dependencies": [],
        },
        bundle / "apm.lock.yaml",
    )
    before = _snapshot(project)
    cache_before = ArtifactSnapshot.capture(isolated.cache_root)

    result = runner.run(
        (
            "install",
            str(bundle),
            "--frozen",
            "--target",
            "cursor",
            "--no-policy",
        ),
        scenario_id="frozen-local-bundle",
        cwd=project.root,
        env=environment,
    )

    assert result.returncode != 0
    assert "--frozen cannot be combined with positional packages" in (result.stdout + result.stderr)
    _assert_same_state(before, _snapshot(project))
    assert_unchanged(cache_before, ArtifactSnapshot.capture(isolated.cache_root))


def test_frozen_missing_redirect_root_does_not_create_directory(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    isolated, environment, project, runner = _scenario(
        tmp_path / "redirect-root",
        apm_binary_path,
    )
    redirect_root = isolated.work_root / "missing-deploy-root"
    before = _snapshot(project)
    cache_before = ArtifactSnapshot.capture(isolated.cache_root)

    result = runner.run(
        (
            "install",
            "--frozen",
            "--root",
            str(redirect_root),
            "--target",
            "cursor",
            "--no-policy",
        ),
        scenario_id="mcp-only-frozen-missing-redirect-root",
        cwd=project.root,
        env=environment,
    )

    assert result.returncode != 0
    assert "--frozen requires --root directory" in result.stdout + result.stderr
    assert not redirect_root.exists()
    _assert_same_state(before, _snapshot(project))
    assert_unchanged(cache_before, ArtifactSnapshot.capture(isolated.cache_root))


@pytest.mark.parametrize(
    "install_suffix",
    (
        (),
        ("--only", "mcp"),
    ),
    ids=("default", "only-mcp"),
)
def test_frozen_stale_mcp_lock_is_read_only_and_normal_install_repairs(
    tmp_path: Path,
    apm_binary_path: Path,
    install_suffix: tuple[str, ...],
) -> None:
    isolated, environment, project, runner = _scenario(
        tmp_path / f"stale-{'-'.join(install_suffix) or 'default'}",
        apm_binary_path,
    )
    install_args = ("install", "--target", "cursor", "--no-policy", *install_suffix)
    initial = runner.run(
        install_args,
        scenario_id=f"mcp-only-stale-seed-{'-'.join(install_suffix) or 'default'}",
        cwd=project.root,
        env=environment,
    )
    _assert_success(initial)

    manifest = load_yaml(project.manifest_path)
    manifest["dependencies"]["mcp"][0]["args"] = ["repaired"]
    dump_yaml(manifest, project.manifest_path)
    stale = _snapshot(project)
    cache_before = ArtifactSnapshot.capture(isolated.cache_root)

    frozen = runner.run(
        ("install", "--frozen", "--target", "cursor", "--no-policy", *install_suffix),
        scenario_id=f"mcp-only-frozen-stale-{'-'.join(install_suffix) or 'default'}",
        cwd=project.root,
        env=environment,
    )
    assert frozen.returncode != 0
    assert "MCP server 'fixture-mcp' config differs" in frozen.stdout
    _assert_same_state(stale, _snapshot(project))
    assert_unchanged(cache_before, ArtifactSnapshot.capture(isolated.cache_root))

    repaired = runner.run(
        install_args,
        scenario_id=f"mcp-only-normal-repair-{'-'.join(install_suffix) or 'default'}",
        cwd=project.root,
        env=environment,
    )
    _assert_success(repaired)
    repaired_state = _snapshot(project)
    assert repaired_state.manifest_bytes == stale.manifest_bytes
    assert repaired_state.lockfile_bytes != stale.lockfile_bytes
    assert b"repaired" in repaired_state.mcp_state_bytes
    _audit(
        runner,
        project,
        environment,
        scenario_id=f"mcp-only-repaired-audit-{'-'.join(install_suffix) or 'default'}",
    )
