"""C11: the git path with the pack checkout installs exactly what ``git checkout`` installs.

A cold ``apm install`` of the tree-10 offline mirror on the git CLI path
(APM_NO_INPROCESS_FETCH=1), once with the pack checkout engaged and once with
git forced (APM_NO_INPROCESS_CHECKOUT=1): identical lockfiles, deployed bytes and
deploy-store receipts, and every package checked out in process.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from apm_cli.utils.git_env import reset_git_cache
from tests.fixtures.perf import TREE_RECIPES, build_tree, serve_offline
from tests.integration.test_inprocess_fetch_world import _receipts, _tree

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.windows_compat,
    pytest.mark.promise("P-OWN-git-transport-selection"),
]

_ENGAGED = "In-process checkout: "


@pytest.fixture(autouse=True)
def _fresh_ambient_git_config():
    reset_git_cache()
    yield
    reset_git_cache()


def _install(apm: Path, project: Path, env: dict[str, str]) -> int:
    result = subprocess.run(
        [str(apm), "install"],
        cwd=project,
        env={**env, "APM_LOG_LEVEL": "DEBUG", "APM_NO_INPROCESS_FETCH": "1"},
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
    return result.stderr.count(_ENGAGED)


def test_the_pack_checkout_and_git_checkout_install_identically(
    apm_binary_path: Path, tmp_path: Path
) -> None:
    tree = build_tree(TREE_RECIPES["tree-10"], tmp_path / "fixture")
    with serve_offline(tree, tmp_path / "world") as world:
        env = dict(world.env)
        fast = tmp_path / "pack"
        shutil.copytree(tree.project, fast)
        engaged = _install(apm_binary_path, fast, env)
        fast_receipts = _receipts(world.cache_dir)

        world.clear_cache()
        slow = tmp_path / "git"
        shutil.copytree(tree.project, slow)
        forced = _install(apm_binary_path, slow, {**env, "APM_NO_INPROCESS_CHECKOUT": "1"})
        git_receipts = _receipts(world.cache_dir)

        assert engaged == len(tree.packages) == 10
        assert forced == 0
        assert (fast / "apm.lock.yaml").read_bytes() == (slow / "apm.lock.yaml").read_bytes()
        assert _tree(fast) == _tree(slow)
        assert fast_receipts and fast_receipts == git_receipts


def test_a_warm_reinstall_verifies_the_pack_checkout_through_git_readers(
    apm_binary_path: Path, tmp_path: Path
) -> None:
    """C9: the one-shot skip covers the fresh landing only; a second command re-verifies."""
    tree = build_tree(TREE_RECIPES["tree-10"], tmp_path / "fixture")
    with serve_offline(tree, tmp_path / "world") as world:
        env = dict(world.env)
        first = tmp_path / "first"
        shutil.copytree(tree.project, first)
        assert _install(apm_binary_path, first, env) == 10
        second = tmp_path / "second"
        shutil.copytree(tree.project, second)
        assert _install(apm_binary_path, second, env) == 0
        assert (first / "apm.lock.yaml").read_bytes() == (second / "apm.lock.yaml").read_bytes()
        assert _tree(first) == _tree(second)
