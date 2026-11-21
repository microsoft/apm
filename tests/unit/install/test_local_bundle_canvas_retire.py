"""Retiring a local bundle's canvas touches only that bundle's rows and unedited files."""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
from apm_cli.core.scope import InstallScope
from apm_cli.deps.lockfile import LockFile, get_lockfile_path
from apm_cli.install.local_bundle_canvas import (
    record_bundle_canvas,
    retire_recorded_bundle_canvas,
)
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.utils.content_hash import compute_file_hash

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-local-bundle-hook-trust")]

_ROOT = ".github/extensions/board/"
_TARGETS = [KNOWN_TARGETS["copilot"]]


def _deploy(project: Path, slug: str, names: list[str]) -> list[str]:
    rows = [f"{_ROOT}{name}" for name in names]
    for row in rows:
        (project / row).parent.mkdir(parents=True, exist_ok=True)
        (project / row).write_text(f"export default '{slug}';\n", encoding="utf-8")
    lockfile = LockFile.read(get_lockfile_path(project)) or LockFile()
    existing = list(lockfile.local_deployed_files)
    hashes = dict(lockfile.local_deployed_file_hashes)
    hashes.update({row: compute_file_hash(project / row) for row in rows})
    DeploymentLedgerCodec.record_local_bundle_files(
        lockfile, existing + rows, hashes, scope=InstallScope.PROJECT
    )
    lockfile.write(get_lockfile_path(project))
    record_bundle_canvas(project, _TARGETS, slug, None, rows)
    return rows


def test_retire_keeps_other_bundles_and_edited_files(tmp_path: Path) -> None:
    project = tmp_path / "p"
    project.mkdir()
    _deploy(project, "first", ["extension.mjs"])
    _deploy(project, "second", ["second.mjs", "edited.mjs"])
    (project / _ROOT / "edited.mjs").write_text("// local edit\n", encoding="utf-8")

    assert retire_recorded_bundle_canvas(project, _TARGETS, "second", user_scope=False)

    assert not (project / _ROOT / "second.mjs").exists()
    assert (project / _ROOT / "edited.mjs").read_text(encoding="utf-8") == "// local edit\n"
    assert (project / _ROOT / "extension.mjs").is_file()
    rows = LockFile.read(get_lockfile_path(project)).local_deployed_files
    assert rows == [f"{_ROOT}extension.mjs"]
    # The second bundle's record is gone, so a repeat retires nothing.
    assert not retire_recorded_bundle_canvas(project, _TARGETS, "second", user_scope=False)
    assert (project / _ROOT / "extension.mjs").is_file()
