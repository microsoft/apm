"""``apm install`` refuses a pinned commit no origin branch or tag reaches.

Real CLI against a local origin behind a GitLab-shaped URL (git
``insteadOf``). On ``main``: m1 <- m2. f1 sits only under
``refs/pull/1/head`` (a pull request from a fork). Every refusal must leave
``apm_modules`` empty and the lockfile unchanged, on every download path.
"""

from __future__ import annotations

import functools
import importlib
import os
import shutil
import ssl
import subprocess
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner

from apm_cli.cli import cli

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-git-ref-freshness")]

_REMOTE = "https://gitlab.com/acme/platform/pkg"
# Only the transport credentials are replaced; the downloader runs as shipped.
_DOWNLOADER = "apm_cli.deps.github_downloader.GitHubPackageDownloader"
_UNREACHABLE = "not reachable from any branch or tag"
_ADMISSION = "apm_cli.cache.commit_reachability"


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        [
            "git",
            "-c",
            "user.name=APM Test",
            "-c",
            "user.email=apm@example.com",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "tag.gpgsign=false",
            *args,
        ],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _package(root: Path, name: str, payload: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "apm.yml").write_text(f"name: {name}\nversion: 1.0.0\n", encoding="utf-8")
    instructions = root / ".apm" / "instructions"
    instructions.mkdir(parents=True, exist_ok=True)
    (instructions / f"{name}.instructions.md").write_text(payload, encoding="utf-8")


class _Origin:
    def __init__(self, root: Path) -> None:
        work = root / "work"
        work.mkdir(parents=True)
        _git("init", "-q", "-b", "main", cwd=work)
        for label in ("m1", "m2"):
            _package(work, "pkg", label)
            _package(work / "skills" / "tool", "tool", label)
            _git("add", "-A", cwd=work)
            _git("commit", "-qm", label, cwd=work)
            setattr(self, label, _git("rev-parse", "HEAD", cwd=work))
        _git("checkout", "-qb", "fork", self.m1, cwd=work)
        _package(work, "pkg", "f1")
        _package(work / "skills" / "tool", "tool", "f1")
        _git("add", "-A", cwd=work)
        _git("commit", "-qm", "f1", cwd=work)
        self.f1 = _git("rev-parse", "HEAD", cwd=work)
        _git("checkout", "-q", "main", cwd=work)
        _git("update-ref", "refs/pull/1/head", self.f1, cwd=work)
        _git("branch", "-qD", "fork", cwd=work)
        self.path = root / "origin.git"
        _git("clone", "-q", "--mirror", str(work), str(self.path), cwd=root)

    def env(self) -> dict[str, str]:
        rewrites = [f"{_REMOTE}.git", _REMOTE]
        env = {"GIT_CONFIG_COUNT": str(len(rewrites))}
        for index, remote in enumerate(rewrites):
            env[f"GIT_CONFIG_KEY_{index}"] = f"url.{self.path.as_uri()}.insteadOf"
            env[f"GIT_CONFIG_VALUE_{index}"] = remote
        return env


def _project(root: Path, ref: str | None, path: str | None = None) -> Path:
    project = root / "app"
    project.mkdir(exist_ok=True)
    dep = {"git": f"{_REMOTE}.git"}
    if ref is not None:
        dep["ref"] = ref
    if path:
        dep["path"] = path
    manifest = {
        "name": "app",
        "version": "1.0.0",
        "targets": ["copilot"],
        "dependencies": {"apm": [dep]},
    }
    (project / "apm.yml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    return project


def _install(project: Path, origin: _Origin, monkeypatch: pytest.MonkeyPatch, **env: str):
    def _fixture_git_env(downloader, **_kwargs) -> dict[str, str]:
        return dict(downloader.git_env)

    monkeypatch.chdir(project)
    with (
        patch.dict(os.environ, {**origin.env(), **env}, clear=False),
        patch("apm_cli.commands._helpers.check_for_updates", return_value=None),
        patch(f"{_DOWNLOADER}._resolve_dep_token", return_value=None),
        patch(f"{_DOWNLOADER}._resolve_dep_auth_ctx", return_value=None),
        patch(
            f"{_DOWNLOADER}._build_noninteractive_git_env",
            autospec=True,
            side_effect=_fixture_git_env,
        ),
    ):
        return CliRunner().invoke(cli, ["install", "--no-policy", "--target", "copilot"])


def _installed(project: Path) -> list[str]:
    modules = project / "apm_modules"
    return sorted(p.name for p in modules.rglob("apm.yml")) if modules.is_dir() else []


@pytest.fixture()
def origin(tmp_path: Path) -> _Origin:
    return _Origin(tmp_path / "remote")


@pytest.mark.parametrize(
    ("path", "cache_env"),
    [
        (None, {"APM_CACHE_DIR": "cache"}),
        (None, {"APM_NO_CACHE": "1"}),
        ("skills/tool", {"APM_CACHE_DIR": "cache"}),
        ("skills/tool", {"APM_NO_CACHE": "1"}),
    ],
    ids=["repo-cached", "repo-no-cache", "subdir-cached", "subdir-no-cache"],
)
def test_install_refuses_a_commit_only_a_pull_ref_reaches(
    tmp_path: Path, origin: _Origin, monkeypatch: pytest.MonkeyPatch, path, cache_env
) -> None:
    project = _project(tmp_path, origin.f1, path)
    env = {
        key: str(tmp_path / value) if key == "APM_CACHE_DIR" else value
        for key, value in cache_env.items()
    }

    result = _install(project, origin, monkeypatch, **env)

    assert result.exit_code == 1, result.output
    assert _UNREACHABLE in " ".join(result.output.split())
    assert _installed(project) == []
    assert not (project / "apm.lock.yaml").exists()


def test_install_admits_a_non_tip_commit_on_main(
    tmp_path: Path, origin: _Origin, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path, origin.m1)

    result = _install(project, origin, monkeypatch, APM_CACHE_DIR=str(tmp_path / "cache"))

    assert result.exit_code == 0, result.output
    assert _installed(project) != []


@pytest.mark.parametrize(
    ("path", "no_cache"),
    [(None, False), (None, True), ("skills/tool", False), ("skills/tool", True)],
    ids=["repo-cached", "repo-no-cache", "subdir-cached", "subdir-no-cache"],
)
def test_a_locked_commit_only_a_pull_ref_reaches_is_refused_on_replay(
    tmp_path: Path, origin: _Origin, monkeypatch: pytest.MonkeyPatch, path, no_cache
) -> None:
    project = _project(tmp_path, "main", path)
    first = _install(project, origin, monkeypatch, APM_CACHE_DIR=str(tmp_path / "cache"))
    assert first.exit_code == 0, first.output
    lock_path = project / "apm.lock.yaml"
    lock = lock_path.read_text(encoding="utf-8").replace(origin.m2, origin.f1)
    assert origin.f1 in lock
    lock_path.write_text(lock, encoding="utf-8")
    shutil.rmtree(project / "apm_modules")

    replay_env = {"APM_NO_CACHE": "1"} if no_cache else {"APM_CACHE_DIR": str(tmp_path / "cache2")}
    result = _install(project, origin, monkeypatch, **replay_env)

    output = " ".join(result.output.split())
    assert result.exit_code == 1, result.output
    # The refusal names the entry, the commit and the reachability rule.
    assert _UNREACHABLE in output
    assert origin.f1 in output
    assert "acme/platform/pkg" in output
    assert _installed(project) == []
    assert lock_path.read_text(encoding="utf-8") == lock


_VIRTUAL_FILE = "skills/tool/.apm/instructions/tool.instructions.md"


@pytest.mark.parametrize("no_cache", [False, True], ids=["cached", "no-cache"])
@pytest.mark.parametrize("ref", ["f1", "pull/1/head"], ids=["pull-only-sha", "pull-ref-name"])
def test_a_single_file_dependency_is_admitted_before_its_bytes_are_fetched(
    tmp_path: Path, origin: _Origin, monkeypatch: pytest.MonkeyPatch, ref, no_cache
) -> None:
    project = _project(tmp_path, origin.f1 if ref == "f1" else ref, _VIRTUAL_FILE)
    env = {"APM_NO_CACHE": "1"} if no_cache else {"APM_CACHE_DIR": str(tmp_path / "cache")}

    result = _install(project, origin, monkeypatch, **env)

    output = " ".join(result.output.split())
    assert result.exit_code == 1, result.output
    assert _UNREACHABLE in output or "not a branch or tag of the origin" in output
    assert not (project / "apm_modules").exists() or not any(
        (project / "apm_modules").rglob("*.md")
    )
    assert not (project / "apm.lock.yaml").exists()


def test_a_previewed_install_of_a_non_tip_commit_leaves_no_reachability_state(
    tmp_path: Path, origin: _Origin, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path, origin.m1)
    home = tmp_path / "home"
    home.mkdir()
    cache = home / ".cache" / "apm"
    for name in ("APM_CACHE_DIR", "XDG_CACHE_HOME", "APM_NO_CACHE"):
        monkeypatch.delenv(name, raising=False)

    def _dry_run(project: Path) -> object:
        monkeypatch.chdir(project)
        with (
            patch.dict(os.environ, {**origin.env(), "HOME": str(home)}, clear=False),
            patch("apm_cli.commands._helpers.check_for_updates", return_value=None),
            patch(f"{_DOWNLOADER}._resolve_dep_token", return_value=None),
            patch(f"{_DOWNLOADER}._resolve_dep_auth_ctx", return_value=None),
            patch(
                f"{_DOWNLOADER}._build_noninteractive_git_env",
                autospec=True,
                side_effect=lambda downloader, **_k: dict(downloader.git_env),
            ),
        ):
            return CliRunner().invoke(
                cli, ["install", "--dry-run", "--no-policy", "--target", "copilot"]
            )

    result = _dry_run(project)

    assert result.exit_code == 0, result.output
    # Not even an empty default cache root is created by the preview.
    assert not cache.exists()
    assert not (project / "apm.lock.yaml").exists()


def test_a_locked_commit_is_verified_once_then_replays_without_contact(
    tmp_path: Path, origin: _Origin, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    project = _project(tmp_path, origin.m1)
    cache = str(tmp_path / "cache")
    listed: list[str] = []
    real = sys.modules["apm_cli.utils.git_env"].git_remote_refs

    def counting(url, *args, **kwargs):
        listed.append(url)
        return real(url, *args, **kwargs)

    monkeypatch.setattr("apm_cli.utils.git_env.git_remote_refs", counting)
    first = _install(project, origin, monkeypatch, APM_CACHE_DIR=cache)
    assert first.exit_code == 0, first.output
    assert len(listed) == 1
    shutil.rmtree(project / "apm_modules")
    listed.clear()

    replay = _install(project, origin, monkeypatch, APM_CACHE_DIR=cache)

    assert replay.exit_code == 0, replay.output
    assert listed == []
    assert _installed(project) != []


def test_a_policy_denied_install_of_a_verified_commit_leaves_no_reachability_state(
    tmp_path: Path, origin: _Origin, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path, origin.m1)
    cache = tmp_path / "cache"
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    policy = tmp_path / "apm-policy.yml"
    policy.write_text(
        "name: unmanaged-deny\nversion: '1.0'\nenforcement: block\n"
        "unmanaged_files:\n  action: deny\n",
        encoding="utf-8",
    )
    hand_written = project / ".github" / "instructions" / "hand-written.instructions.md"
    hand_written.parent.mkdir(parents=True)
    hand_written.write_text("# hand written\n", encoding="utf-8")
    monkeypatch.chdir(project)

    with (
        patch.dict(
            os.environ,
            {**origin.env(), "APM_CACHE_DIR": str(cache), "TMPDIR": str(scratch)},
            clear=False,
        ),
        patch("tempfile.tempdir", None),
        patch("apm_cli.commands._helpers.check_for_updates", return_value=None),
        patch(f"{_DOWNLOADER}._resolve_dep_token", return_value=None),
        patch(f"{_DOWNLOADER}._resolve_dep_auth_ctx", return_value=None),
        patch(
            f"{_DOWNLOADER}._build_noninteractive_git_env",
            autospec=True,
            side_effect=lambda downloader, **_k: dict(downloader.git_env),
        ),
    ):
        result = CliRunner().invoke(
            cli, ["install", "--policy", str(policy), "--target", "copilot"]
        )

    output = " ".join(result.output.split())
    assert result.exit_code == 1, result.output
    assert "unmanaged_files.action: deny" in output
    assert not (cache / "reachability").exists()
    assert not list(scratch.glob("apm-reach-*"))
    assert not (project / "apm.lock.yaml").exists()


@pytest.mark.parametrize(
    ("ref", "path", "no_cache", "tiered"),
    [
        ("main", None, False, True),
        ("main", None, True, True),
        ("main", "skills/tool", False, True),
        ("main", "skills/tool", True, True),
        ("main", "skills/tool", False, False),
        (None, None, False, True),
        (None, None, True, True),
        (None, "skills/tool", False, True),
        (None, "skills/tool", True, True),
    ],
    ids=[
        "repo-cached",
        "repo-no-cache",
        "subdir-cached",
        "subdir-no-cache",
        "subdir-untiered",
        "default-branch-repo-cached",
        "default-branch-repo-no-cache",
        "default-branch-subdir-cached",
        "default-branch-subdir-no-cache",
    ],
)
def test_a_branch_that_moves_after_admission_installs_the_admitted_commit(
    tmp_path: Path, origin: _Origin, monkeypatch: pytest.MonkeyPatch, ref, path, no_cache, tiered
) -> None:
    """A named ref (#5138) or the default branch (#5199) is fetched by its admitted commit."""
    # Admission is the hook between resolution and fetch, for any ref.
    admit = importlib.import_module(_ADMISSION).admit_pinned_commit
    work = tmp_path / "remote" / "work"
    moved: list[str] = []

    def _admit_then_move(*args, **kwargs):
        admit(*args, **kwargs)
        if not moved:
            _package(work, "pkg", "m3")
            _package(work / "skills" / "tool", "tool", "m3")
            _git("add", "-A", cwd=work)
            _git("commit", "-qm", "m3", cwd=work)
            moved.append(_git("rev-parse", "HEAD", cwd=work))
            _git("push", "-q", str(origin.path), "main", cwd=work)

    project = _project(tmp_path, ref, path)
    env = {"APM_NO_CACHE": "1"} if no_cache else {"APM_CACHE_DIR": str(tmp_path / "cache")}
    if not tiered:
        env["APM_TIERED_RESOLVER"] = "0"
    monkeypatch.setattr(f"{_ADMISSION}.admit_pinned_commit", _admit_then_move)

    result = _install(project, origin, monkeypatch, **env)

    assert result.exit_code == 0, result.output
    assert moved, "the branch never moved after admission"
    lock = (project / "apm.lock.yaml").read_text(encoding="utf-8")
    assert origin.m2 in lock and moved[0] not in lock
    deployed = sorted((project / "apm_modules").rglob("*.instructions.md"))
    assert deployed and all(p.read_text(encoding="utf-8") == "m2" for p in deployed)


@pytest.mark.parametrize(
    ("path", "no_cache"),
    [(None, False), (None, True), ("skills/tool", False), ("skills/tool", True)],
    ids=["repo-cached", "repo-no-cache", "subdir-cached", "subdir-no-cache"],
)
def test_an_admitted_commit_force_pushed_away_fails_closed(
    tmp_path: Path, origin: _Origin, monkeypatch: pytest.MonkeyPatch, path, no_cache
) -> None:
    """#5138: if the admitted commit is gone, the install fails; it never fetches by name."""
    # The verdict recorder is the hook between admission and fetch.
    record_tip = importlib.import_module(_ADMISSION)._record_tip
    work = tmp_path / "remote" / "work"
    rewritten: list[str] = []

    def _admit_then_force_push(*args, **kwargs):
        record_tip(*args, **kwargs)
        if not rewritten:
            _git("reset", "-q", "--hard", origin.m1, cwd=work)
            _package(work, "pkg", "r1")
            _package(work / "skills" / "tool", "tool", "r1")
            _git("add", "-A", cwd=work)
            _git("commit", "-qm", "r1", cwd=work)
            rewritten.append(_git("rev-parse", "HEAD", cwd=work))
            _git("push", "-qf", str(origin.path), "main", cwd=work)
            _git("reflog", "expire", "--expire=now", "--all", cwd=origin.path)
            _git("gc", "-q", "--prune=now", cwd=origin.path)

    project = _project(tmp_path, "main", path)
    env = {"APM_NO_CACHE": "1"} if no_cache else {"APM_CACHE_DIR": str(tmp_path / "cache")}
    monkeypatch.setattr(f"{_ADMISSION}._record_tip", _admit_then_force_push)

    result = _install(project, origin, monkeypatch, **env)

    assert rewritten, "the branch was never rewritten after admission"
    assert result.exit_code == 1, result.output
    output = " ".join(result.output.split())
    assert f"'main' was admitted at commit {origin.m2[:12]}" in output
    assert "can no longer be fetched from the origin" in output
    assert f"now points to {rewritten[0][:12]}" in output
    assert _installed(project) == []
    assert not (project / "apm.lock.yaml").exists()


def test_a_commit_the_server_no_longer_has_fails_without_a_full_clone(
    tmp_path: Path, origin: _Origin
) -> None:
    """#5138: a gone commit fails at depth-1 cost; only a refused want clones in full."""
    from apm_cli.deps.bare_cache import fetch_commit_worktree

    notes: list[str] = []
    target = tmp_path / "checkout"
    gone = "1" * 40

    def _plan(_base, path, *, dep_ref, clone_action, **_kwargs) -> None:
        clone_action(origin.path.as_uri(), {}, path)

    with pytest.raises(subprocess.CalledProcessError) as failed:
        fetch_commit_worktree(
            _plan,
            "acme/platform/pkg",
            target,
            dep_ref=None,
            sha=gone,
            verbose_callback=notes.append,
        )

    assert "not our ref" in failed.value.stderr.decode()
    assert notes == []
    assert not (target / ".apm").exists()


@pytest.mark.parametrize("protocol", ["2", "0"], ids=["fetch-by-commit", "server-refuses-want"])
def test_a_named_ref_is_checked_out_at_its_commit_even_when_a_want_by_commit_is_refused(
    tmp_path: Path, origin: _Origin, protocol: str
) -> None:
    """#5138: the shallow fetch by commit, or the full-clone fallback, checks out that commit."""
    from apm_cli.deps.bare_cache import fetch_commit_worktree

    notes: list[str] = []
    target = tmp_path / "checkout"
    env = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "protocol.version"}
    env["GIT_CONFIG_VALUE_0"] = protocol

    def _plan(_base, path, *, dep_ref, clone_action, **_kwargs) -> None:
        clone_action(origin.path.as_uri(), env, path)

    fetch_commit_worktree(
        _plan,
        "acme/platform/pkg",
        target,
        dep_ref=None,
        sha=origin.m1,
        verbose_callback=notes.append,
    )

    assert (target / ".apm" / "instructions" / "pkg.instructions.md").read_text() == "m1"
    assert _git("rev-parse", "HEAD", cwd=target) == origin.m1
    assert bool(notes) is (protocol == "0")


def _wrapped_git(root: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    """Run every git call through a script that may rewrite what the server says."""
    script = root / "git-wrapper"
    script.write_text(
        f"#!{sys.executable}\n"
        "import os, re, subprocess, sys\n"
        f"REAL = {shutil.which('git')!r}\n"
        "args = sys.argv[1:]\n"
        f"{body}\n"
        "sys.exit(subprocess.run([REAL, *args]).returncode)\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setattr("apm_cli.utils.git_env._git_executable", str(script))


# gettext translates git's messages in any locale but C.
_TRANSLATING = """
if os.environ.get("LC_ALL") != "C":
    done = subprocess.run([REAL, *args], stderr=subprocess.PIPE)
    sys.stderr.buffer.write(done.stderr.replace(
        b"does not allow request for unadvertised object",
        b"erlaubt keine Anfrage nach einem nicht angebotenen Objekt"))
    sys.exit(done.returncode)
"""

# A host that answers "not our ref" for every commit it did not advertise.
_NOT_OUR_REF = """
if "fetch" in args and re.fullmatch("[0-9a-f]{40}", args[-1]):
    sys.stderr.write("fatal: remote error: upload-pack: not our ref " + args[-1] + "\\n")
    sys.exit(128)
"""


def test_a_refused_want_by_commit_falls_back_under_a_non_english_locale(
    tmp_path: Path, origin: _Origin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#5138: the refusal is recognized whatever language git speaks."""
    from apm_cli.deps.bare_cache import fetch_commit_worktree

    _wrapped_git(tmp_path, monkeypatch, _TRANSLATING)
    for name, value in {"LANG": "de_DE.UTF-8", "LANGUAGE": "de", "LC_ALL": "de_DE.UTF-8"}.items():
        monkeypatch.setenv(name, value)
    notes: list[str] = []
    target = tmp_path / "checkout"
    env = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "protocol.version"}
    env["GIT_CONFIG_VALUE_0"] = "0"

    def _plan(_base, path, *, dep_ref, clone_action, **_kwargs) -> None:
        clone_action(origin.path.as_uri(), env, path)

    fetch_commit_worktree(
        _plan,
        "acme/platform/pkg",
        target,
        dep_ref=None,
        sha=origin.m1,
        verbose_callback=notes.append,
    )

    assert (target / ".apm" / "instructions" / "pkg.instructions.md").read_text() == "m1"
    assert _git("rev-parse", "HEAD", cwd=target) == origin.m1
    assert len(notes) == 1


@pytest.mark.parametrize("ref", ["main", "refs/heads/main"])
def test_a_host_that_will_not_serve_a_commit_by_id_gets_the_admitted_tip_by_name(
    tmp_path: Path, origin: _Origin, monkeypatch: pytest.MonkeyPatch, ref: str
) -> None:
    """#5138: "not our ref" for a still-admitted tip fetches it by name, shallow."""
    from apm_cli.deps.bare_cache import fetch_commit_worktree

    _wrapped_git(tmp_path, monkeypatch, _NOT_OUR_REF)
    notes: list[str] = []
    target = tmp_path / "checkout"

    def _plan(_base, path, *, dep_ref, clone_action, **_kwargs) -> None:
        clone_action(origin.path.as_uri(), {}, path)

    fetch_commit_worktree(
        _plan,
        "acme/platform/pkg",
        target,
        dep_ref=SimpleNamespace(reference=ref),
        sha=origin.m2,
        verbose_callback=notes.append,
    )

    assert (target / ".apm" / "instructions" / "pkg.instructions.md").read_text() == "m2"
    assert _git("rev-parse", "HEAD", cwd=target) == origin.m2
    assert _git("rev-parse", "--is-shallow-repository", cwd=target) == "true"
    assert notes == []


def test_a_host_that_will_not_serve_a_moved_admitted_commit_names_it(
    tmp_path: Path, origin: _Origin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#5138: when the ref moved off the admitted commit, nothing is fetched by name."""
    from apm_cli.cache.commit_reachability import AdmittedCommitGoneError
    from apm_cli.deps.bare_cache import fetch_commit_worktree

    _wrapped_git(tmp_path, monkeypatch, _NOT_OUR_REF)
    target = tmp_path / "checkout"

    def _plan(_base, path, *, dep_ref, clone_action, **_kwargs) -> None:
        clone_action(origin.path.as_uri(), {}, path)

    with pytest.raises(AdmittedCommitGoneError) as failed:
        fetch_commit_worktree(
            _plan,
            "acme/platform/pkg",
            target,
            dep_ref=SimpleNamespace(reference="main"),
            sha=origin.m1,
        )

    assert f"'main' was admitted at commit {origin.m1[:12]}" in str(failed.value)
    assert f"now points to {origin.m2[:12]}" in str(failed.value)
    assert not (target / ".apm").exists()


def test_a_fetch_by_commit_applies_a_remote_activated_mirror_rewrite(
    tmp_path: Path, origin: _Origin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#5138: config a clone activates by its remote URL also reaches the fetch by commit."""
    from apm_cli.deps.bare_cache import fetch_commit_worktree

    mirror = tmp_path / "mirror.gitconfig"
    mirror.write_text(
        f'[url "{origin.path.as_uri()}"]\n\tinsteadOf = {_REMOTE}.git\n', encoding="ascii"
    )
    config = tmp_path / "global.gitconfig"
    config.write_text(
        f'[includeIf "hasconfig:remote.*.url:{_REMOTE}.git"]\n\tpath = {mirror.as_posix()}\n',
        encoding="ascii",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    target = tmp_path / "checkout"

    def _plan(_base, path, *, dep_ref, clone_action, **_kwargs) -> None:
        clone_action(f"{_REMOTE}.git", {"GIT_TERMINAL_PROMPT": "0"}, path)

    fetch_commit_worktree(_plan, "acme/platform/pkg", target, dep_ref=None, sha=origin.m1)

    assert (target / ".apm" / "instructions" / "pkg.instructions.md").read_text() == "m1"
    assert _git("rev-parse", "HEAD", cwd=target) == origin.m1


def test_a_package_with_a_link_installs_from_a_host_that_will_not_serve_a_commit_by_id(
    tmp_path: Path, origin: _Origin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#5138: the by-name fallback is verified after links are materialized."""
    work = tmp_path / "remote" / "work"
    instructions = work / ".apm" / "instructions"
    (instructions / "linked.instructions.md").symlink_to("pkg.instructions.md")
    _git("add", "-A", cwd=work)
    _git("commit", "-qm", "link", cwd=work)
    linked = _git("rev-parse", "HEAD", cwd=work)
    _git("push", "-q", str(origin.path), "main", cwd=work)
    _wrapped_git(tmp_path, monkeypatch, _NOT_OUR_REF)
    project = _project(tmp_path, "main")

    result = _install(project, origin, monkeypatch, APM_NO_CACHE="1")

    assert result.exit_code == 0, result.output
    assert linked in (project / "apm.lock.yaml").read_text(encoding="utf-8")
    [deployed] = (project / "apm_modules").rglob("linked.instructions.md")
    assert not deployed.is_symlink()
    assert deployed.read_text(encoding="utf-8") == "m2"


def test_a_mirror_alone_serves_the_by_name_fallback_of_a_remote_activated_rewrite(
    tmp_path: Path, origin: _Origin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#5138: the ref listing of the fallback goes where the clone would, never the origin."""
    from apm_cli.deps.bare_cache import fetch_commit_worktree

    remote = "https://origin.invalid/acme/pkg.git"
    mirror = tmp_path / "mirror.gitconfig"
    mirror.write_text(f'[url "{origin.path.as_uri()}"]\n\tinsteadOf = {remote}\n', encoding="ascii")
    config = tmp_path / "global.gitconfig"
    config.write_text(
        f'[includeIf "hasconfig:remote.*.url:{remote}"]\n\tpath = {mirror.as_posix()}\n',
        encoding="ascii",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    listed = tmp_path / "listed"
    _wrapped_git(
        tmp_path,
        monkeypatch,
        _NOT_OUR_REF
        + f"""
if "ls-remote" in args:
    url = next(a for a in args[args.index("ls-remote") + 1 :] if not a.startswith("-"))
    where = subprocess.run([REAL, "ls-remote", "--get-url", url], capture_output=True, text=True)
    with open({str(listed)!r}, "a") as log:
        log.write(where.stdout)
""",
    )
    target = tmp_path / "checkout"

    def _plan(_base, path, *, dep_ref, clone_action, **_kwargs) -> None:
        clone_action(remote, {"GIT_TERMINAL_PROMPT": "0"}, path)

    fetch_commit_worktree(
        _plan, "acme/pkg", target, dep_ref=SimpleNamespace(reference="main"), sha=origin.m2
    )

    assert listed.read_text(encoding="utf-8").split() == [origin.path.as_uri()]
    assert (target / ".apm" / "instructions" / "pkg.instructions.md").read_text() == "m2"


@pytest.fixture()
def dumb_http(origin: _Origin, tmp_path: Path):
    """Serve the origin over Git's dumb HTTPS protocol, which has no shallow fetch.

    Yields the repository URL; the loopback certificate is self-signed.
    """
    from tests.integration.test_marketplace_generic_https_credential_lifecycle import (
        _write_tls_certificate,
    )
    from tests.utils.local_git_http_server import _stdlib_server_tls

    (tmp_path / "tls").mkdir()
    leaf, key = _write_tls_certificate(tmp_path / "tls")
    _git("--git-dir", str(origin.path), "update-server-info", cwd=origin.path.parent)
    served = tmp_path / "served"
    (served / "acme").mkdir(parents=True)
    (served / "acme" / "pkg.git").symlink_to(origin.path)
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(served))
    handler.log_message = lambda *_args: None
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    with _stdlib_server_tls():  # a truststore-injected ssl cannot serve TLS
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(certfile=leaf, keyfile=key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"https://127.0.0.1:{server.server_port}/acme/pkg.git"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


@pytest.mark.parametrize("no_cache", [False, True], ids=["cached", "no-cache"])
def test_a_subdir_named_ref_installs_from_a_dumb_http_host(
    tmp_path: Path,
    origin: _Origin,
    dumb_http: str,
    monkeypatch: pytest.MonkeyPatch,
    no_cache,
) -> None:
    """#5138: a host with no shallow fetch still admits and installs a named ref's commit."""
    url = dumb_http
    project = _project(tmp_path, "main", "skills/tool")
    manifest = yaml.safe_load((project / "apm.yml").read_text(encoding="utf-8"))
    manifest["dependencies"]["apm"][0]["git"] = url
    (project / "apm.yml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    config = tmp_path / "global.gitconfig"
    base = url.rsplit("/acme/", 1)[0] + "/"
    config.write_text(f'[http "{base}"]\n\tsslVerify = false\n', encoding="ascii")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    env = {"APM_NO_CACHE": "1"} if no_cache else {"APM_CACHE_DIR": str(tmp_path / "cache")}

    result = _install(project, origin, monkeypatch, **env)

    assert result.exit_code == 0, result.output
    assert origin.m2 in (project / "apm.lock.yaml").read_text(encoding="utf-8")
    [deployed] = (project / "apm_modules").rglob("tool.instructions.md")
    assert deployed.read_text(encoding="utf-8") == "m2"


@pytest.mark.parametrize("no_cache", [False, True], ids=["cached", "no-cache"])
def test_a_locked_subdir_ref_on_a_dumb_http_host_replays_its_locked_commit(
    tmp_path: Path, origin: _Origin, dumb_http: str, monkeypatch: pytest.MonkeyPatch, no_cache
) -> None:
    """#5138: a lockfile pins the commit after the branch moves; no ref listing runs.

    The locked fetch goes to the host by commit: no ls-remote, no clone by name.
    """
    url = dumb_http
    project = _project(tmp_path, "main", "skills/tool")
    manifest = yaml.safe_load((project / "apm.yml").read_text(encoding="utf-8"))
    manifest["dependencies"]["apm"][0]["git"] = url
    (project / "apm.yml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    config = tmp_path / "global.gitconfig"
    base = url.rsplit("/acme/", 1)[0] + "/"
    config.write_text(f'[http "{base}"]\n\tsslVerify = false\n', encoding="ascii")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    env = {"APM_NO_CACHE": "1"} if no_cache else {"APM_CACHE_DIR": str(tmp_path / "cache")}
    assert _install(project, origin, monkeypatch, **env).exit_code == 0
    work = tmp_path / "remote" / "work"
    _package(work / "skills" / "tool", "tool", "m3")
    _git("add", "-A", cwd=work)
    _git("commit", "-qm", "m3", cwd=work)
    moved = _git("rev-parse", "HEAD", cwd=work)
    _git("push", "-q", str(origin.path), "main", cwd=work)
    _git("--git-dir", str(origin.path), "update-server-info", cwd=origin.path.parent)
    shutil.rmtree(project / "apm_modules")
    calls_log = tmp_path / "git-calls"
    _wrapped_git(
        tmp_path,
        monkeypatch,
        f"""
with open({str(calls_log)!r}, "a") as log:
    log.write(" ".join(args) + "\\n")
""",
    )

    result = _install(project, origin, monkeypatch, **env)

    assert result.exit_code == 0, result.output
    lock = (project / "apm.lock.yaml").read_text(encoding="utf-8")
    assert origin.m2 in lock and moved not in lock
    [deployed] = (project / "apm_modules").rglob("tool.instructions.md")
    assert deployed.read_text(encoding="utf-8") == "m2"
    calls = calls_log.read_text(encoding="utf-8").splitlines()
    # Fetched by the locked commit; the ref is neither listed nor cloned by name.
    assert [call for call in calls if " fetch " in call and call.endswith(origin.m2)]
    listed = [call for call in calls if {"ls-remote", "--branch"} & set(call.split())]
    assert not listed, listed


@pytest.mark.parametrize("include_if", [False, True], ids=["none", "one"])
def test_a_warm_pinned_install_probes_clone_config_only_when_config_has_an_include_if(
    tmp_path: Path, origin: _Origin, monkeypatch: pytest.MonkeyPatch, include_if
) -> None:
    """#5138: without an includeIf, config cannot depend on the remote: nothing is probed."""
    config = tmp_path / "global.gitconfig"
    config.write_text(
        '[includeIf "gitdir:/nonexistent/"]\n\tpath = /nonexistent.gitconfig\n'
        if include_if
        else "",
        encoding="ascii",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    project = _project(tmp_path, origin.m1)
    env = {"APM_CACHE_DIR": str(tmp_path / "cache")}
    assert _install(project, origin, monkeypatch, **env).exit_code == 0
    shutil.rmtree(project / "apm_modules")
    calls_log = tmp_path / "git-calls"
    _wrapped_git(
        tmp_path,
        monkeypatch,
        f"""
with open({str(calls_log)!r}, "a") as log:
    log.write(" ".join(args) + "\\n")
""",
    )

    result = _install(project, origin, monkeypatch, **env)

    assert result.exit_code == 0, result.output
    assert origin.m1 in (project / "apm.lock.yaml").read_text(encoding="utf-8")
    assert _installed(project)
    calls = calls_log.read_text(encoding="utf-8").splitlines() if calls_log.exists() else []
    # A probe adds the origin remote to a scratch repository named "origin".
    probed = [
        c
        for c in map(str.split, calls)
        if c[:1] == ["-C"] and c[1].endswith("/origin") and c[2:4] == ["remote", "add"]
    ]
    # Once per URL the attempt contacts (a hasconfig pattern matches the URL
    # as spelled, so the admission's and the cache's spellings each count).
    urls = [call[-1] for call in probed]
    assert sorted(set(urls)) == sorted(urls), probed
    assert bool(urls) is include_if, probed
