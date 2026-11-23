"""A full install stores every deployed file's bytes under its lockfile hash.

After ``apm install`` on the tree-10 offline mirror, each ``sha256:`` value in
the lockfile's ``deployed_file_hashes`` has a deploy-store entry whose bytes
re-hash to it, so a later command can rebuild any deployed file from the
lockfile alone. A tampered entry is a miss, and the next full install heals it.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

from apm_cli.utils.content_hash import canonical_digest
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
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


# A per-install identifier that differs on every run by design.
_PER_RUN = {".apm-local/installation"}


def _tree(root: Path) -> dict[str, bytes]:
    files = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and ".git" not in path.parts
    }
    return {rel: data for rel, data in files.items() if rel not in _PER_RUN}


def test_an_empty_or_deleted_store_changes_nothing(apm_binary_path: Path, tmp_path: Path) -> None:
    tree = build_tree(TREE_RECIPES["tree-10"], tmp_path / "fixture")
    with serve_offline(tree, tmp_path / "world") as world:
        with_store = tmp_path / "with-store"
        shutil.copytree(tree.project, with_store)
        _install(apm_binary_path, with_store, world.env)
        assert any((world.cache_dir / _BUCKET).rglob("*"))

        shutil.rmtree(world.cache_dir / _BUCKET)
        without_store = tmp_path / "without-store"
        shutil.copytree(tree.project, without_store)
        _install(apm_binary_path, without_store, world.env)

        assert _tree(without_store) == _tree(with_store)


def test_every_installed_package_gets_a_record_binding_its_lockfile_hashes(
    apm_binary_path: Path, tmp_path: Path
) -> None:
    tree = build_tree(TREE_RECIPES["tree-10"], tmp_path / "fixture")
    with serve_offline(tree, tmp_path / "world") as world:
        project = tmp_path / "project"
        shutil.copytree(tree.project, project)
        _install(apm_binary_path, project, world.env)

        lock = load_yaml(project / "apm.lock.yaml")
        locked = [
            {
                rel: value.removeprefix("sha256:")
                for rel, value in dep["deployed_file_hashes"].items()
            }
            for dep in lock["dependencies"]
            if dep.get("deployed_file_hashes")
        ]
        records = [
            load_yaml(path)
            for path in sorted((world.cache_dir / _BUCKET / "records").rglob("*.json"))
        ]

        assert locked and len(records) == len(locked)
        recorded = [{rel: pair[0] for rel, pair in rec["files"].items()} for rec in records]
        assert sorted(map(sorted, map(dict.items, recorded))) == sorted(
            map(sorted, map(dict.items, locked))
        )
        for rec in records:
            for rel, (_digest, raw) in rec["files"].items():
                assert hashlib.sha256((project / rel).read_bytes()).hexdigest() == raw


def test_cache_clean_after_an_install_empties_the_store(
    apm_binary_path: Path, tmp_path: Path
) -> None:
    tree = build_tree(TREE_RECIPES["tree-10"], tmp_path / "fixture")
    with serve_offline(tree, tmp_path / "world") as world:
        project = tmp_path / "project"
        shutil.copytree(tree.project, project)
        _install(apm_binary_path, project, world.env)
        store = world.cache_dir / _BUCKET
        stored = [path for path in store.rglob("*") if path.is_file()]
        assert stored

        info = subprocess.run(
            [str(apm_binary_path), "cache", "info"],
            cwd=project,
            env=world.env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert info.returncode == 0, info.stdout + info.stderr
        assert "Deployed file entries:" in info.stdout
        cleaned = subprocess.run(
            [str(apm_binary_path), "cache", "clean", "--force"],
            cwd=project,
            env=world.env,
            capture_output=True,
            text=True,
            timeout=120,
        )

        assert cleaned.returncode == 0, cleaned.stdout + cleaned.stderr
        assert not [path for path in store.rglob("*") if path.is_file()]


def _cache(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_a_policy_refused_install_stores_nothing(apm_binary_path: Path, tmp_path: Path) -> None:
    """QL binding property: a refused install never reaches the deploy store."""
    tree = build_tree(TREE_RECIPES["tree-10"], tmp_path / "fixture")
    denied = sorted(tree.packages)[0]
    policy = {
        "name": "deny-one",
        "version": "1.0.0",
        "enforcement": "block",
        "dependencies": {"deny": [f"{tree.recipe.owner}/{denied}"]},
    }
    with serve_offline(tree, tmp_path / "world") as world:
        project = tmp_path / "project"
        shutil.copytree(tree.project, project)
        dump_yaml(policy, project / "org-policy.yml")
        store = world.cache_dir / _BUCKET

        def refused() -> None:
            result = subprocess.run(
                [str(apm_binary_path), "install", "--policy", "./org-policy.yml"],
                cwd=project,
                env=world.env,
                capture_output=True,
                text=True,
                timeout=600,
            )
            assert result.returncode != 0, result.stdout + result.stderr
            assert denied in result.stdout + result.stderr

        refused()
        assert not [path for path in store.rglob("*") if path.is_file()], "cold refusal stored"

        warm = tmp_path / "warm"
        shutil.copytree(tree.project, warm)
        _install(apm_binary_path, warm, world.env)
        before = _cache(world.cache_dir)
        refused()

        assert _cache(world.cache_dir) == before, "a refused install changed the cache"
