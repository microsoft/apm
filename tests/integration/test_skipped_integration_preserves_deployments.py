"""Skipped-integration deployment preservation contract."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from apm_cli.core.scope import InstallScope
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.install.phases import cleanup
from apm_cli.install.phases.lockfile import LockfileBuilder
from apm_cli.install.sources import Materialization
from apm_cli.install.template import run_integration_template
from apm_cli.utils.content_hash import compute_file_hash
from apm_cli.utils.diagnostics import DiagnosticCollector

pytestmark = [pytest.mark.integration, pytest.mark.component]


def test_skipped_integration_preserves_prior_files_and_lock_claims(
    tmp_path: Path,
) -> None:
    """Keep real deployed skills and their lock claims when integration skips."""
    dep_key = "owner/package"
    prior_commit = "a" * 40
    current_commit = "b" * 40
    current_content_hash = "sha256:new-package"
    deployed_files = [f".agents/skills/skill-{index:02d}/SKILL.md" for index in range(24)]
    deployed_hashes = {}
    for relative in deployed_files:
        deployed_path = tmp_path / relative
        deployed_path.parent.mkdir(parents=True)
        deployed_path.write_text(f"# {relative}\n", encoding="ascii")
        deployed_hashes[relative] = compute_file_hash(deployed_path)

    previous = LockFile()
    previous.add_dependency(
        LockedDependency(
            repo_url=dep_key,
            resolved_commit=prior_commit,
            content_hash="sha256:prior-package",
            deployed_files=deployed_files,
            deployed_file_hashes=deployed_hashes,
        )
    )
    current = LockFile()
    current.add_dependency(LockedDependency(repo_url=dep_key, resolved_commit=current_commit))
    diagnostics = DiagnosticCollector()
    ctx = SimpleNamespace(
        existing_lockfile=previous,
        only_packages=False,
        intended_dep_keys={dep_key},
        package_deployed_files={},
        orphan_cleanup_retained={},
        package_cleanup_retained={},
        package_hashes={dep_key: current_content_hash},
        project_root=tmp_path,
        targets=[],
        diagnostics=diagnostics,
        logger=None,
        scope=InstallScope.PROJECT,
        skill_subset_from_cli=False,
        skill_subset=None,
        update_refs=True,
    )
    source = SimpleNamespace(
        ctx=ctx,
        dep_ref=SimpleNamespace(is_local=False, local_path=None),
        INTEGRATE_ERROR_PREFIX="Failed to integrate primitives",
    )

    run_integration_template(
        source,
        materialization=Materialization(
            package_info=None,
            install_path=tmp_path / "unused",
            dep_key=dep_key,
            deltas={"installed": 0},
        ),
    )
    cleanup.run(ctx)
    builder = LockfileBuilder(ctx)
    builder._attach_deployed_files(current)
    builder._attach_content_hashes(current)

    assert ctx.package_deployed_files == {}
    assert all((tmp_path / relative).is_file() for relative in deployed_files)
    locked = current.get_dependency(dep_key)
    assert locked.resolved_commit == current_commit
    assert locked.content_hash == current_content_hash
    assert locked.deployed_files == deployed_files
    assert locked.deployed_file_hashes == deployed_hashes
