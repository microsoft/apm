"""The in-process pinned fetch installs exactly what git installs, and it engages.

F12: a cold ``apm install`` of the tree-10 offline mirror (TLS through the
world's proxy) with the in-process fetch engaged, and again with git forced
(APM_NO_INPROCESS_FETCH=1), must produce identical lockfiles,
deployed bytes and deploy-store receipts. The hit-rate cell installs under a
realistic user git config and reports how many packages were fetched in
process. The rate-limit cells prove the fetch never moves a user to another
request bucket: an authenticated attempt never uses it, and a rate-limit answer
sends the rest of the command to git with the same lockfile.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from apm_cli.cache.inprocess_fetch import fetch_worktree
from apm_cli.core.auth import AuthResolver
from apm_cli.utils.git_env import reset_git_cache
from apm_cli.utils.yaml_io import load_yaml
from tests.fixtures.perf import TREE_RECIPES, build_tree, serve_offline

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.windows_compat,
    pytest.mark.promise("P-OWN-git-transport-selection"),
]


@pytest.fixture(autouse=True)
def _fresh_ambient_git_config():
    """Each offline world has its own CA: drop the process-life ambient TLS probe around it."""
    reset_git_cache()
    yield
    reset_git_cache()


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

        world.clear_cache()
        slow = tmp_path / "git"
        shutil.copytree(tree.project, slow)
        forced = _assert_installed(apm_binary_path, slow, {**env, "APM_NO_INPROCESS_FETCH": "1"})
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


_TOKEN = "ghp_" + "a" * 36
_REVOKED = "ghp_" + "r" * 36


def _basic(token: str) -> str:
    import base64

    return "Basic " + base64.b64encode(f"x-access-token:{token}".encode()).decode()


def _auth_env(world, token: str = _TOKEN) -> dict[str, str]:
    return {**world.env, "GITHUB_APM_PAT": token, "APM_GITHUB_AUTH_FIRST": "1"}


def _private(world, token: str = _TOKEN) -> None:
    """Every github.com git request without this token's header is a 401 challenge."""
    expected = _basic(token)
    world.services.script(
        "github.com",
        "/",
        401,
        {"WWW-Authenticate": 'Basic realm="GitHub"'},
        b"",
        when=lambda request: (request.header("Authorization") or "") != expected,
    )


def _run(apm: Path, project: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(apm), "install"],
        cwd=project,
        env={**env, "APM_LOG_LEVEL": "DEBUG"},
        capture_output=True,
        text=True,
        timeout=600,
    )


def _engaged_or_why(apm: Path, project: Path, env: dict[str, str]) -> int:
    """Packages fetched in process; on a miss the assertion shows each fallback reason."""
    result = _run(apm, project, env)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
    engaged = result.stderr.count(_ENGAGED)
    reasons = sorted(
        {line.strip()[-200:] for line in result.stderr.splitlines() if "in-process fetch" in line}
    )
    assert engaged == 10, reasons[:20]
    return engaged


def test_an_authenticated_attempt_fetches_in_process_with_only_apms_header(
    apm_binary_path: Path, tmp_path: Path
) -> None:
    """A': the managed github-basic header goes per request; the lockfile equals git's."""
    tree = build_tree(TREE_RECIPES["tree-10"], tmp_path / "fixture")
    with serve_offline(tree, tmp_path / "world") as world:
        world.services.proxy.clear()
        fast = tmp_path / "in-process"
        shutil.copytree(tree.project, fast)
        assert _engaged_or_why(apm_binary_path, fast, _auth_env(world)) == 10
        fetches = _fetches(world)
        assert len(fetches) == 10
        assert {request.header("Authorization") for request in fetches} == {_basic(_TOKEN)}

        world.clear_cache()
        slow = tmp_path / "git"
        shutil.copytree(tree.project, slow)
        forced = {**_auth_env(world), "APM_NO_INPROCESS_FETCH": "1"}
        assert _assert_installed(apm_binary_path, slow, forced) == 0
        assert (fast / "apm.lock.yaml").read_bytes() == (slow / "apm.lock.yaml").read_bytes()
        assert _tree(fast) == _tree(slow)


def test_a_private_repository_installs_in_process_like_git(
    apm_binary_path: Path, tmp_path: Path
) -> None:
    tree = build_tree(TREE_RECIPES["tree-10"], tmp_path / "fixture")
    with serve_offline(tree, tmp_path / "world") as world:
        _private(world)
        fast = tmp_path / "in-process"
        shutil.copytree(tree.project, fast)
        assert _engaged_or_why(apm_binary_path, fast, _auth_env(world)) == 10

        world.clear_cache()
        slow = tmp_path / "git"
        shutil.copytree(tree.project, slow)
        forced = {**_auth_env(world), "APM_NO_INPROCESS_FETCH": "1"}
        assert _assert_installed(apm_binary_path, slow, forced) == 0
        assert (fast / "apm.lock.yaml").read_bytes() == (slow / "apm.lock.yaml").read_bytes()
        assert _tree(fast) == _tree(slow)


def test_a_revoked_token_fails_exactly_like_git_and_never_leaks(
    apm_binary_path: Path, tmp_path: Path
) -> None:
    tree = build_tree(TREE_RECIPES["tree-10"], tmp_path / "fixture")
    with serve_offline(tree, tmp_path / "world") as world:
        _private(world)
        fast = tmp_path / "in-process"
        shutil.copytree(tree.project, fast)
        b1 = _run(apm_binary_path, fast, _auth_env(world, _REVOKED))
        world.clear_cache()
        slow = tmp_path / "git"
        shutil.copytree(tree.project, slow)
        git = _run(
            apm_binary_path, slow, {**_auth_env(world, _REVOKED), "APM_NO_INPROCESS_FETCH": "1"}
        )

    assert b1.returncode != 0
    assert b1.returncode == git.returncode
    assert not (fast / "apm.lock.yaml").exists() and not (slow / "apm.lock.yaml").exists()
    for output in (b1.stdout + b1.stderr, git.stdout + git.stderr):
        assert _REVOKED not in output
        assert _basic(_REVOKED).split(" ", 1)[1] not in output


def test_a_user_extra_header_sends_the_authenticated_fetch_to_git(
    apm_binary_path: Path, tmp_path: Path
) -> None:
    tree = build_tree(TREE_RECIPES["tree-10"], tmp_path / "fixture")
    with serve_offline(tree, tmp_path / "world") as world:
        with open(world.env["GIT_CONFIG_GLOBAL"], "a", encoding="utf-8", newline="\n") as config:
            config.write('[http "https://github.com/"]\n\textraHeader = X-Corp-Proxy: 1\n')
        world.services.proxy.clear()
        project = tmp_path / "project"
        shutil.copytree(tree.project, project)

        assert _assert_installed(apm_binary_path, project, _auth_env(world)) == 0
        assert _fetches(world) == []


def test_the_token_never_reaches_debug_output(apm_binary_path: Path, tmp_path: Path) -> None:
    tree = build_tree(TREE_RECIPES["tree-10"], tmp_path / "fixture")
    with serve_offline(tree, tmp_path / "world") as world:
        project = tmp_path / "project"
        shutil.copytree(tree.project, project)
        result = _run(apm_binary_path, project, _auth_env(world))

    assert result.returncode == 0, result.stderr[-2000:]
    output = result.stdout + result.stderr
    assert _TOKEN not in output
    assert _basic(_TOKEN).split(" ", 1)[1] not in output


def test_the_retry_env_fetches_in_process_and_an_anonymous_env_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = build_tree(TREE_RECIPES["tree-10"], tmp_path / "fixture")
    with serve_offline(tree, tmp_path / "world") as world:
        # The env builders read the ambient TLS config from this process: make it the world's.
        for variable, value in world.env.items():
            monkeypatch.setenv(variable, value)
        reset_git_cache()
        name = sorted(tree.packages)[0]
        url = f"https://github.com/{tree.slug(name)}"
        anonymous = AuthResolver.build_public_github_anonymous_git_env(base_env=dict(world.env))
        listed = subprocess.run(
            ["git", "ls-remote", "--tags", url],
            env=anonymous,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        refs = dict(line.split("\t")[::-1] for line in listed.splitlines() if line)
        tag = f"refs/tags/{tree.recipe.tag}"
        sha = refs.get(f"{tag}^{{}}") or refs[tag]
        retry = AuthResolver.build_public_github_authenticated_git_env(
            _TOKEN, base_env=dict(world.env)
        )
        world.services.proxy.clear()
        (tmp_path / "retry").mkdir()
        assert fetch_worktree(url, sha, tmp_path / "retry", retry, ()) is not None
        (tmp_path / "anonymous").mkdir()
        assert fetch_worktree(url, sha, tmp_path / "anonymous", anonymous, ()) is not None
        assert [request.header("Authorization") for request in _fetches(world)] == [
            _basic(_TOKEN),
            None,
        ]
