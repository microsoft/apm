"""A symlinked apm.lock.yaml must refuse before any deployment write (#3532, #3649).

#3532 makes lockfile writers fail closed on a symlinked ``apm.lock.yaml``.
Commands that will rewrite the lockfile (``apm update``, ``apm install
--update``, ``apm uninstall``) must refuse before they deploy, delete or edit
anything else, so a failed run leaves the project exactly as it was. Commands
that leave the lockfile unchanged keep working through the link.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.integration._lockfile_chain_kit import (
    Kit,
    build_kit,
    output,
    publish_v110,
    tree,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
]

_LINK_TARGET = "../shared/apm.lock.yaml"


def _symlink_lock(kit: Kit) -> tuple[Path, bytes]:
    project = kit.project
    shared = project.parent / "shared"
    shared.mkdir()
    target = shared / "apm.lock.yaml"
    (project / "apm.lock.yaml").rename(target)
    os.symlink(_LINK_TARGET, project / "apm.lock.yaml")
    return target, target.read_bytes()


def _assert_refused_without_writes(
    kit: Kit, args: tuple[str, ...], target: Path, original: bytes
) -> None:
    project = kit.project
    before = tree(project)
    result = kit.run(*args)
    text = output(result)
    assert result.returncode != 0, text
    assert "symbolic link" in text, text
    link = project / "apm.lock.yaml"
    assert link.is_symlink() and os.readlink(link) == _LINK_TARGET
    assert target.read_bytes() == original
    assert tree(project) == before, "a refused lockfile write must not touch the project"


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows")
@pytest.mark.parametrize(
    "args",
    [("update", "--yes"), ("install", "--update", "--no-policy")],
    ids=["update", "install-update"],
)
def test_update_with_symlinked_lock_refuses_before_deploying(
    tmp_path, apm_binary_path, args
) -> None:
    kit = build_kit(tmp_path, apm_binary_path)
    target, original = _symlink_lock(kit)
    publish_v110(kit)
    _assert_refused_without_writes(kit, args, target, original)


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows")
def test_uninstall_with_symlinked_lock_refuses_before_deleting(tmp_path, apm_binary_path) -> None:
    kit = build_kit(tmp_path, apm_binary_path)
    target, original = _symlink_lock(kit)
    _assert_refused_without_writes(kit, ("uninstall", "test/kit"), target, original)
    assert (kit.project / "apm_modules/test/kit").is_dir()


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows")
def test_unchanged_installs_still_read_through_symlinked_lock(tmp_path, apm_binary_path) -> None:
    kit = build_kit(tmp_path, apm_binary_path)
    target, original = _symlink_lock(kit)
    before = tree(kit.project)
    for args in (("install", "--no-policy"), ("install", "--no-policy", "--frozen")):
        result = kit.run(*args)
        assert result.returncode == 0, output(result)
        assert tree(kit.project) == before
        assert (kit.project / "apm.lock.yaml").is_symlink()
        assert target.read_bytes() == original
    publish_v110(kit)
    noop = kit.run("update", "--yes", "--dry-run")
    assert noop.returncode == 0, output(noop)
    assert tree(kit.project) == before
