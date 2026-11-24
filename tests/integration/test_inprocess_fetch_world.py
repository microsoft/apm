"""The in-process pinned fetch installs exactly what git installs, and it engages.

F12: a cold ``apm install`` of the tree-10 offline mirror (TLS through the
world's proxy) with the in-process fetch engaged, and again with git forced
by an inert global attributes file, must produce identical lockfiles,
deployed bytes and deploy-store receipts. The hit-rate cell installs under a
realistic user git config and reports how many packages were fetched in
process.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import load_yaml
from tests.fixtures.perf import TREE_RECIPES, build_tree, serve_offline

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-OWN-git-transport-selection"),
]

_ENGAGED = "In-process fetch: "
# A per-install identifier that differs on every run by design.
_PER_RUN = {".apm-local/installation"}

# Shape of a developer's global config, as in test_anonymous_ref_listing_world.
_USER_GITCONFIG = """
[user]
\tname = Example Developer
\temail = dev@example.invalid
[init]
\tdefaultBranch = main
[pull]
\trebase = true
[credential "https://github.com"]
\thelper =
\thelper = !gh auth git-credential
[credential "https://dev.azure.com"]
\tuseHttpPath = true
[url "git@gitlab.example.invalid:"]
\tinsteadOf = https://gitlab.example.invalid/
"""


def _install(apm: Path, project: Path, env: dict[str, str]) -> int:
    """Run a cold install; return how many packages the in-process fetch landed."""
    result = subprocess.run(
        [str(apm), "install"],
        cwd=project,
        env={**env, "APM_LOG_LEVEL": "DEBUG"},
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
    return result.stderr.count(_ENGAGED)


def _tree(root: Path) -> dict[str, bytes]:
    files = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and ".git" not in path.parts
    }
    return {rel: data for rel, data in files.items() if rel not in _PER_RUN}


def _receipts(cache_dir: Path) -> list[dict]:
    records = sorted((cache_dir / "deploy_v1" / "records").rglob("*.json"))
    return sorted((load_yaml(path)["files"] for path in records), key=repr)


def _global_attributes(env: dict[str, str]) -> Path:
    xdg = env.get("XDG_CONFIG_HOME")
    home = env.get("HOME") or env.get("USERPROFILE")
    assert xdg or home, "the world names a config home"
    return Path(xdg) / "git" / "attributes" if xdg else Path(home) / ".config/git/attributes"


def test_in_process_and_git_installs_are_byte_identical(
    apm_binary_path: Path, tmp_path: Path
) -> None:
    tree = build_tree(TREE_RECIPES["tree-10"], tmp_path / "fixture")
    with serve_offline(tree, tmp_path / "world") as world:
        env = dict(world.env)
        fast = tmp_path / "in-process"
        shutil.copytree(tree.project, fast)
        engaged = _install(apm_binary_path, fast, env)
        fast_receipts = _receipts(world.cache_dir)

        # An empty global attributes file changes no bytes but makes the fetch refuse.
        attributes = _global_attributes(env)
        attributes.parent.mkdir(parents=True, exist_ok=True)
        attributes.write_bytes(b"")
        try:
            world.clear_cache()
            slow = tmp_path / "git"
            shutil.copytree(tree.project, slow)
            forced = _install(apm_binary_path, slow, env)
        finally:
            os.unlink(attributes)
        git_receipts = _receipts(world.cache_dir)

        assert engaged == len(tree.packages) == 10
        assert forced == 0
        assert (fast / "apm.lock.yaml").read_bytes() == (slow / "apm.lock.yaml").read_bytes()
        assert _tree(fast) == _tree(slow)
        assert fast_receipts and fast_receipts == git_receipts


def test_every_package_is_fetched_in_process_under_a_realistic_user_config(
    apm_binary_path: Path, tmp_path: Path
) -> None:
    tree = build_tree(TREE_RECIPES["tree-10"], tmp_path / "fixture")
    with serve_offline(tree, tmp_path / "world") as world:
        with open(world.env["GIT_CONFIG_GLOBAL"], "a", encoding="utf-8", newline="\n") as config:
            config.write(_USER_GITCONFIG)
        project = tmp_path / "project"
        shutil.copytree(tree.project, project)

        engaged = _install(apm_binary_path, project, dict(world.env))

        print(f"in-process hit rate: {engaged}/{len(tree.packages)}")
        assert engaged == len(tree.packages) == 10
