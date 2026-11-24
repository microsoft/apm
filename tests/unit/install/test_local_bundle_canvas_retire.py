"""Retiring a local bundle's canvas touches only that bundle's rows and unedited files.

Removal stays inside the project: a symlinked extension directory never lets
retirement delete a matching file it points at, and a file another bundle's
record still lists stays.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.core.deployment_ledger import DeploymentLedgerCodec, record_bundle_receipts
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
    record_bundle_receipts(
        project, scope=InstallScope.PROJECT, targets=_TARGETS, written=rows, lockfile=lockfile
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


def test_retire_never_follows_a_symlinked_extension_directory(tmp_path: Path) -> None:
    project = tmp_path / "p"
    project.mkdir()
    _deploy(project, "first", ["extension.mjs"])
    outside = tmp_path / "outside"
    outside.mkdir()
    content = (project / _ROOT / "extension.mjs").read_bytes()
    (outside / "extension.mjs").write_bytes(content)
    (project / _ROOT / "extension.mjs").unlink()
    (project / _ROOT).rmdir()
    (project / _ROOT.rstrip("/")).symlink_to(outside, target_is_directory=True)

    retire_recorded_bundle_canvas(project, _TARGETS, "first", user_scope=False)

    assert (outside / "extension.mjs").read_bytes() == content


def test_retire_keeps_a_file_another_bundle_still_records(tmp_path: Path) -> None:
    project = tmp_path / "p"
    project.mkdir()
    _deploy(project, "first", ["extension.mjs"])
    _deploy(project, "second", ["extension.mjs"])

    retire_recorded_bundle_canvas(project, _TARGETS, "second", user_scope=False)

    assert (project / _ROOT / "extension.mjs").is_file()
    assert f"{_ROOT}extension.mjs" in LockFile.read(get_lockfile_path(project)).local_deployed_files


def test_recording_the_canvas_classifies_an_unrecorded_hook_record_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recording a bundle's canvas is an install write: a hook record file the config
    file records never saw is classified as the user's, not left unrecorded."""
    import json

    home = tmp_path / "apm-home"
    monkeypatch.setenv("APM_HOME", str(home))
    project = tmp_path / "p"
    sidecar = project / ".github" / "apm-hooks.json"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text("{}\n", encoding="utf-8")
    row = f"{_ROOT}extension.mjs"
    (project / row).parent.mkdir(parents=True)
    (project / row).write_text("export default 'first';\n", encoding="utf-8")

    record_bundle_canvas(project, _TARGETS, "first", None, [row])

    store = home / "deployment-receipts" / "config-files" / "records.json"
    rows = json.loads(store.read_text(encoding="utf-8"))["files"]
    origins = {entry["path"]: entry["origin"] for entry in rows}
    assert origins.get(sidecar.resolve().as_posix()) == "preexisting"


def test_recording_the_canvas_at_user_scope_classifies_against_the_user_installation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At user scope the hook record file is classified against ``$APM_HOME``'s lockfile."""
    import json

    home = tmp_path / "apm-home"
    home.mkdir()
    (home / "apm.lock.yaml").write_text(
        "lockfile_version: '1'\ndependencies: []\n", encoding="utf-8"
    )
    monkeypatch.setenv("APM_HOME", str(home))
    project = tmp_path / "p"
    sidecar = project / ".github" / "apm-hooks.json"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text(json.dumps({"hooks": {"Stop": [{"command": "x"}]}}) + "\n", encoding="utf-8")
    row = f"{_ROOT}extension.mjs"
    (project / row).parent.mkdir(parents=True)
    (project / row).write_text("export default 'first';\n", encoding="utf-8")

    record_bundle_canvas(project, _TARGETS, "first", None, [row], user_scope=True)

    store = home / "deployment-receipts" / "config-files" / "records.json"
    rows = json.loads(store.read_text(encoding="utf-8"))["files"]
    origins = {entry["path"]: entry["origin"] for entry in rows}
    assert origins.get(sidecar.resolve().as_posix()) == "legacy"
