"""The in-process pinned fetch installs exactly what git installs, and it engages.

F12: a cold ``apm install`` of the tree-10 offline mirror (TLS through the
world's proxy) with the in-process fetch engaged, and again with git forced
by an inert global attributes file, must produce identical lockfiles,
deployed bytes and deploy-store receipts. The hit-rate cell installs under a
realistic user git config and reports how many packages were fetched in
process. The rate-limit cells prove the fetch never moves a user to another
request bucket: an authenticated attempt never uses it, and a rate-limit answer
sends the rest of the command to git with the same lockfile.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from apm_cli.cache.inprocess_fetch import fetch_worktree
from apm_cli.core.auth import AuthResolver
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


def _assert_installed(apm: Path, project: Path, env: dict[str, str], *args: str) -> int:
    """Run a cold install; return how many packages the in-process fetch landed."""
    result = subprocess.run(
        [str(apm), "install", *args],
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
        engaged = _assert_installed(apm_binary_path, fast, env)
        fast_receipts = _receipts(world.cache_dir)

        # An empty global attributes file changes no bytes but makes the fetch refuse.
        attributes = _global_attributes(env)
        attributes.parent.mkdir(parents=True, exist_ok=True)
        attributes.write_bytes(b"")
        try:
            world.clear_cache()
            slow = tmp_path / "git"
            shutil.copytree(tree.project, slow)
            forced = _assert_installed(apm_binary_path, slow, env)
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

        engaged = _assert_installed(apm_binary_path, project, dict(world.env))

        print(f"in-process hit rate: {engaged}/{len(tree.packages)}")
        assert engaged == len(tree.packages) == 10


def _in_process_fetch(request) -> bool:
    """A fetch request of the in-process client (git's own carry a ``git/`` agent)."""
    agent = request.header("User-Agent") or ""
    return (
        request.method == "POST"
        and request.path.endswith("/git-upload-pack")
        and not agent.startswith("git/")
        and b"command=fetch" in request.body
    )


def _fetches(world) -> list:
    return [request for request in world.services.proxy.observations if _in_process_fetch(request)]


def test_a_rate_limit_mid_install_sends_the_rest_to_git_with_the_same_lockfile(
    apm_binary_path: Path, tmp_path: Path
) -> None:
    tree = build_tree(TREE_RECIPES["tree-10"], tmp_path / "fixture")
    with serve_offline(tree, tmp_path / "world") as world:
        env = dict(world.env)
        reference = tmp_path / "reference"
        shutil.copytree(tree.project, reference)
        assert _assert_installed(apm_binary_path, reference, env) == 10

        world.clear_cache()
        world.services.proxy.clear()
        limited: list[object] = []

        def first_fetch(request) -> bool:
            if limited or not _in_process_fetch(request):
                return False
            limited.append(request)
            return True

        world.services.script(
            "github.com", "/", 429, {"Retry-After": "60"}, b"rate limited", when=first_fetch
        )
        project = tmp_path / "limited"
        shutil.copytree(tree.project, project)
        engaged = _assert_installed(apm_binary_path, project, env, "--parallel-downloads", "1")

        assert [request.status for request in _fetches(world)] == [429], "no retry after a 429"
        assert engaged == 0
        assert (project / "apm.lock.yaml").read_bytes() == (
            reference / "apm.lock.yaml"
        ).read_bytes()
        assert _tree(project) == _tree(reference)


def test_an_authenticated_attempt_never_uses_the_in_process_fetch(
    apm_binary_path: Path, tmp_path: Path
) -> None:
    tree = build_tree(TREE_RECIPES["tree-10"], tmp_path / "fixture")
    with serve_offline(tree, tmp_path / "world") as world:
        env = {**world.env, "GITHUB_APM_PAT": "ghp_" + "a" * 36, "APM_GITHUB_AUTH_FIRST": "1"}
        project = tmp_path / "project"
        shutil.copytree(tree.project, project)
        world.services.proxy.clear()

        assert _assert_installed(apm_binary_path, project, env) == 0
        assert _fetches(world) == []

        dep = load_yaml(project / "apm.lock.yaml")["dependencies"][0]
        url, sha = f"https://github.com/{dep['repo_url']}", dep["resolved_commit"]
        retry = AuthResolver.build_public_github_authenticated_git_env(
            "ghp_" + "b" * 36, base_env=dict(world.env)
        )
        (tmp_path / "retry").mkdir()
        assert fetch_worktree(url, sha, tmp_path / "retry", retry, ()) is None
        assert _fetches(world) == []
        # Killed control: the anonymous attempt of the same package does fetch in process.
        anonymous = AuthResolver.build_public_github_anonymous_git_env(base_env=dict(world.env))
        (tmp_path / "anonymous").mkdir()
        assert fetch_worktree(url, sha, tmp_path / "anonymous", anonymous, ()) is not None
        assert len(_fetches(world)) == 1
