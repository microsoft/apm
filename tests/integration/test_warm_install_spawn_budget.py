"""Warm-cache installs spawn nothing per dependency and make no avoidable request.

The floor guard for the warm journeys on the tree-10 offline mirror: once the
cache holds every pinned commit, a no-op install and a ``--frozen`` replay need
no network and no per-dependency process. The only process left is org-policy
discovery reading the project's git remote, once per command. A fresh install
without a lockfile also resolves each ref once (a tag may move, so it is always
asked), which builds the git TLS settings once per process. A PATH shim counts
every git/gh process the CLI starts itself (nested git invocations are not
counted); the offline proxy counts every request.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from tests.fixtures.perf import TREE_RECIPES, build_tree, serve_offline

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell shims count spawns"),
    pytest.mark.promise("P-LAW-source.ref_cache_coherent", "P-LAW-lockfile.frozen_preserves_bytes"),
]

_SHIMMED = ("git", "gh")
# The fixture project is not a git repository: no 'origin', then no remote.
_REMOTE_READ = ["git remote get-url origin", "git remote"]
_TLS_PROBE = "git config --null --show-scope --list"
_TIMEOUT_S = 600


def _write_shims(directory: Path, search_path: str, log: Path) -> None:
    directory.mkdir(parents=True)
    for name in _SHIMMED:
        real = shutil.which(name, path=search_path)
        if real is None:
            continue
        shim = directory / name
        shim.write_text(
            "#!/bin/sh\n"
            'if [ -z "$APM_SPAWN_NESTED" ]; then\n'
            f'  printf "%s\\n" "{name} $*" >> "{log}"\n'
            "fi\n"
            "APM_SPAWN_NESTED=1 export APM_SPAWN_NESTED\n"
            f'exec "{real}" "$@"\n',
            encoding="utf-8",
        )
        shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class _Journey:
    def __init__(self, apm: Path, world, env: dict[str, str], log: Path) -> None:
        self.apm, self.world, self.env, self.log = apm, world, env, log

    def run(self, project: Path, *argv: str) -> tuple[list[str], int]:
        """Run ``apm *argv``; return the spawned commands and the request count."""
        self.log.unlink(missing_ok=True)
        self.world.reset_traffic()
        result = subprocess.run(
            [str(self.apm), *argv],
            cwd=project,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
            check=False,
        )
        assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
        spawns = self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []
        return spawns, self.world.traffic().as_dict()["requests"]


@pytest.fixture(scope="module")
def warm(apm_binary_path: Path, tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("warm-budget")
    tree = build_tree(TREE_RECIPES["tree-10"], root / "fixture")
    with serve_offline(tree, root / "world") as world:
        log = root / "spawns.log"
        shims = root / "shims"
        _write_shims(shims, world.env.get("PATH", os.defpath), log)
        env = dict(world.env, PATH=os.pathsep.join([str(shims), world.env.get("PATH", "")]))
        journey = _Journey(apm_binary_path, world, env, log)
        seed = root / "seed"
        shutil.copytree(tree.project, seed)
        journey.run(seed, "install")  # fills the cache; not measured
        yield journey, tree, seed


def _fresh(tree, seed: Path, destination: Path, *, lockfile: bool) -> Path:
    shutil.copytree(tree.project, destination)
    if lockfile:
        shutil.copyfile(seed / "apm.lock.yaml", destination / "apm.lock.yaml")
    return destination


def test_a_no_op_install_reads_the_remote_once_and_requests_nothing(warm) -> None:
    journey, _tree, seed = warm
    before = (seed / "apm.lock.yaml").read_bytes()

    spawns, requests = journey.run(seed, "install")

    assert (spawns, requests) == (_REMOTE_READ, 0)
    assert (seed / "apm.lock.yaml").read_bytes() == before


def test_a_frozen_replay_from_a_warm_cache_spawns_nothing_per_dependency(
    warm, tmp_path: Path
) -> None:
    journey, tree, seed = warm
    project = _fresh(tree, seed, tmp_path / "frozen", lockfile=True)

    spawns, requests = journey.run(project, "install", "--frozen")

    assert (spawns, requests) == (_REMOTE_READ, 0)
    assert (project / "apm.lock.yaml").read_bytes() == (seed / "apm.lock.yaml").read_bytes()
    assert sorted(p.name for p in (project / "apm_modules").rglob("apm.yml")) == sorted(
        p.name for p in (seed / "apm_modules").rglob("apm.yml")
    )


def test_a_warm_install_spawns_nothing_per_dependency_and_resolves_each_ref_once(
    warm, tmp_path: Path
) -> None:
    journey, tree, seed = warm
    project = _fresh(tree, seed, tmp_path / "warm", lockfile=False)

    spawns, requests = journey.run(project, "install")

    assert sorted(spawns) == sorted([*_REMOTE_READ, _TLS_PROBE])
    assert requests <= len(tree.packages)
    assert (project / "apm.lock.yaml").read_bytes() == (seed / "apm.lock.yaml").read_bytes()
