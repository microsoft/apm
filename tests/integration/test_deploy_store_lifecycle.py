"""A full install stores every deployed file's bytes under its lockfile hash.

After ``apm install`` on the tree-10 offline mirror, each ``sha256:`` value in
the lockfile's ``deployed_file_hashes`` has a deploy-store entry whose bytes
re-hash to it, so a later command can rebuild any deployed file from the
lockfile alone. A tampered entry is a miss, and the next full install heals it.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from apm_cli.utils.content_hash import canonical_digest
from apm_cli.utils.yaml_io import load_yaml
from tests.fixtures.perf import TREE_RECIPES, build_tree, serve_offline

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-LAW-source.ref_cache_coherent"),
]

_BUCKET = "deploy_v1"


def _deployed_digests(lockfile: Path) -> set[str]:
    digests: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if str(key).endswith("deployed_file_hashes") and isinstance(value, dict):
                    digests.update(v.removeprefix("sha256:") for v in value.values())
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(load_yaml(lockfile))
    return digests


def _install(apm: Path, project: Path, env: dict[str, str]) -> None:
    result = subprocess.run(
        [str(apm), "install"], cwd=project, env=env, capture_output=True, text=True, timeout=600
    )
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]


def test_every_deployed_file_hash_has_a_verified_store_entry(
    apm_binary_path: Path, tmp_path: Path
) -> None:
    tree = build_tree(TREE_RECIPES["tree-10"], tmp_path / "fixture")
    with serve_offline(tree, tmp_path / "world") as world:
        project = tmp_path / "project"
        shutil.copytree(tree.project, project)
        _install(apm_binary_path, project, world.env)
        store = world.cache_dir / _BUCKET

        digests = _deployed_digests(project / "apm.lock.yaml")

        assert digests, "the fixture deploys files"
        for digest in sorted(digests):
            entry = store / digest[:2] / digest
            assert canonical_digest(entry.read_bytes()) == digest, entry

        victim = sorted(digests)[0]
        entry = store / victim[:2] / victim
        entry.write_bytes(entry.read_bytes() + b"\ntampered\n")
        fresh = tmp_path / "fresh"
        shutil.copytree(tree.project, fresh)
        _install(apm_binary_path, fresh, world.env)

        assert canonical_digest(entry.read_bytes()) == victim, "a full install heals the entry"
