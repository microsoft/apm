"""``apm install`` refuses a pinned commit no origin branch or tag reaches.

Real CLI against a local origin behind a GitLab-shaped URL (git
``insteadOf``). On ``main``: m1 <- m2. f1 sits only under
``refs/pull/1/head`` (a pull request from a fork). Every refusal must leave
``apm_modules`` empty and the lockfile unchanged, on every download path.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
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


def _project(root: Path, ref: str, path: str | None = None) -> Path:
    project = root / "app"
    project.mkdir(exist_ok=True)
    dep = {"git": f"{_REMOTE}.git", "ref": ref}
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

    assert result.exit_code == 1, result.output
    assert _UNREACHABLE in " ".join(result.output.split())
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
