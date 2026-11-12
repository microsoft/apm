"""An SSH-declared non-GitHub dependency stays on SSH (#3640, #3649).

The first install fills the persistent Git cache over SSH, never trying
``https://<host>``; a cold project restored with ``--frozen`` and the audit
replay reuse the same transport.  A PATH ``git`` shim records every URL.
"""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

import pytest

from tests.integration._dependency_graph_world import NO_POLICY, World, write_package

pytestmark = [pytest.mark.integration, pytest.mark.requires_apm_binary]

_SSH = "git@gitlab.example.invalid:org/sshpkg.git"


@pytest.mark.skipif(os.name == "nt", reason="POSIX git shim")
def test_ssh_non_github_dependency_never_tries_https(tmp_path: Path, apm_binary_path: Path) -> None:
    world = World.create(tmp_path, apm_binary_path)
    source = write_package(world.isolated.package_root / "sshpkg", "sshpkg")
    repository = world.factory.create("sshpkg", source_tree=source)
    world.factory.commit(repository, message="publish")

    log = world.isolated.root / "git-urls.log"
    shim_dir = world.isolated.root / "shim"
    shim_dir.mkdir()
    shim = shim_dir / "git"
    real_git = shutil.which("git")
    assert real_git
    shim.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\nexec "{real_git}" "$@"\n', encoding="ascii")
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    env = world.isolated.subprocess_env()
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = f"url.{repository.file_url}.insteadOf"
    env["GIT_CONFIG_VALUE_0"] = _SSH
    env["PATH"] = f"{shim_dir}{os.pathsep}{env['PATH']}"
    world.env = env

    def https_attempts() -> list[str]:
        text = log.read_text(encoding="utf-8") if log.exists() else ""
        return [line for line in text.splitlines() if "https://gitlab.example.invalid" in line]

    world.manifest([_SSH])
    world.apm("install", NO_POLICY)
    (entry,) = world.locked().values()
    assert entry["host"] == "gitlab.example.invalid"
    assert world.deployed() == {"sshpkg.instructions.md"}
    assert _SSH in log.read_text(encoding="utf-8")
    assert https_attempts() == []

    lock_before = world.lock_bytes()
    shutil.rmtree(world.project / "apm_modules")
    world.apm("install", NO_POLICY, "--frozen")
    assert world.lock_bytes() == lock_before
    world.apm("audit", "--ci", NO_POLICY)
    assert https_attempts() == []
