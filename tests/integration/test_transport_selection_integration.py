"""End-to-end integration tests for Transport Selection v1 (issue #778).

Validates the strict-by-default transport selection contract against a real
public GitHub repository (`github/awesome-copilot`). Wraps gitpython's
``Repo.clone_from`` so we can record which URLs were actually attempted by the
production downloader code path.

Cases:
  1. Public shorthand path -- runs unconditionally with network.
  2. Explicit ``https://`` strict-by-default -- runs unconditionally with network.
  3. Explicit ``ssh://`` strict-by-default -- requires SSH key, gated.
  4. ``insteadOf`` rewrite honored for shorthand -- requires SSH key, gated.
  5. ``APM_GIT_PROTOCOL=ssh`` env -- requires SSH key, gated.
  6. ``APM_ALLOW_PROTOCOL_FALLBACK=1`` escape hatch -- requires SSH key, gated.

Network-dependent. Skipped unless ``APM_RUN_INTEGRATION_TESTS=1``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import List, Tuple  # noqa: F401, UP035
from unittest.mock import patch

import pytest

from apm_cli.deps.github_downloader import GitHubPackageDownloader
from apm_cli.deps.transport_selection import (
    ENV_ALLOW_FALLBACK,
    ENV_PROTOCOL,
    ProtocolPreference,
)
from apm_cli.models.apm_package import DependencyReference

pytestmark = pytest.mark.skipif(
    os.environ.get("APM_RUN_INTEGRATION_TESTS") != "1",
    reason="Set APM_RUN_INTEGRATION_TESTS=1 to run network-dependent tests",
)


_OWNER = "github"
_REPO = "awesome-copilot"


def _ssh_available() -> bool:
    try:
        result = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=no",
                "-T",
                "git@github.com",
            ],
            capture_output=True,
            timeout=10,
        )
        return b"successfully authenticated" in (result.stderr or b"")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


_REQUIRES_SSH = pytest.mark.skipif(
    not _ssh_available(),
    reason="No usable SSH key for git@github.com",
)


@pytest.fixture
def tmp_clone_dir():
    d = Path(tempfile.mkdtemp(prefix="apm-transport-it-"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def isolated_env(monkeypatch):
    for var in (ENV_PROTOCOL, ENV_ALLOW_FALLBACK):
        monkeypatch.delenv(var, raising=False)
    yield monkeypatch


def _attempt_clone(
    spec: str,
    *,
    target_dir: Path,
    protocol_pref: ProtocolPreference = ProtocolPreference.NONE,
    allow_fallback: bool = False,
) -> tuple[bool, list[str]]:
    """Drive the production clone path and capture the URLs attempted."""
    dep = DependencyReference.parse(spec)
    captured: list[str] = []

    from git import Repo as _Repo

    real_clone_from = _Repo.clone_from

    def _record(url, to_path, *args, **kwargs):
        captured.append(url)
        return real_clone_from(url, to_path, *args, **kwargs)

    dl = GitHubPackageDownloader(
        protocol_pref=protocol_pref,
        allow_fallback=allow_fallback,
    )

    with patch("apm_cli.deps.github_downloader.Repo.clone_from", side_effect=_record):
        try:
            dl._clone_with_fallback(
                repo_url_base=dep.repo_url,
                target_path=target_dir,
                dep_ref=dep,
            )
            return True, captured
        except Exception:
            return False, captured


# ---------------------------------------------------------------------------
# Always-on cases (no SSH key required)
# ---------------------------------------------------------------------------


class TestPublicShorthandPath:
    def test_https_clone_succeeds_for_shorthand(self, tmp_clone_dir, isolated_env):
        ok, urls = _attempt_clone(f"{_OWNER}/{_REPO}", target_dir=tmp_clone_dir / "c")
        assert ok, f"clone failed; URLs tried: {urls}"
        assert any(u.startswith("https://") for u in urls)
        assert not any(u.startswith("git@") or u.startswith("ssh://") for u in urls), (
            "shorthand-default must not silently try SSH; URLs tried: %s" % urls  # noqa: UP031
        )


class TestExplicitHttpsStrict:
    def test_explicit_https_clones_no_ssh_attempt(self, tmp_clone_dir, isolated_env):
        ok, urls = _attempt_clone(
            f"https://github.com/{_OWNER}/{_REPO}.git",
            target_dir=tmp_clone_dir / "c",
        )
        assert ok
        assert all(u.startswith("https://") for u in urls), urls


# ---------------------------------------------------------------------------
# SSH-required cases
# ---------------------------------------------------------------------------


@_REQUIRES_SSH
class TestExplicitSshStrict:
    def test_explicit_ssh_clones_no_https_fallback(self, tmp_clone_dir, isolated_env):
        ok, urls = _attempt_clone(
            f"ssh://git@github.com/{_OWNER}/{_REPO}.git",
            target_dir=tmp_clone_dir / "c",
        )
        assert ok
        assert all((u.startswith("git@") or u.startswith("ssh://")) for u in urls), urls

    def test_explicit_ssh_bad_host_does_not_fall_back(self, tmp_clone_dir, isolated_env):
        ok, urls = _attempt_clone(
            "ssh://git@nonexistent-host-apm-test.invalid/foo/bar.git",
            target_dir=tmp_clone_dir / "c",
        )
        assert not ok
        assert all(not u.startswith("https://") for u in urls), urls


@_REQUIRES_SSH
class TestInsteadOfHonored:
    def test_gitconfig_insteadof_makes_shorthand_use_ssh(
        self, tmp_clone_dir, isolated_env, monkeypatch
    ):
        fixture_home = Path(tempfile.mkdtemp(prefix="apm-it-home-"))
        try:
            shutil.copy(
                Path(__file__).parent.parent / "fixtures" / "gitconfig_insteadof_to_ssh",
                fixture_home / ".gitconfig",
            )
            monkeypatch.setenv("HOME", str(fixture_home))
            ok, urls = _attempt_clone(f"{_OWNER}/{_REPO}", target_dir=tmp_clone_dir / "c")
            assert ok
            assert urls and (urls[0].startswith("git@") or urls[0].startswith("ssh://")), urls
        finally:
            shutil.rmtree(fixture_home, ignore_errors=True)


@_REQUIRES_SSH
class TestEnvProtocolOverride:
    def test_env_apm_git_protocol_ssh_picks_ssh_for_shorthand(self, tmp_clone_dir, isolated_env):
        isolated_env.setenv(ENV_PROTOCOL, "ssh")
        ok, urls = _attempt_clone(
            f"{_OWNER}/{_REPO}",
            target_dir=tmp_clone_dir / "c",
            protocol_pref=ProtocolPreference.SSH,
        )
        assert ok
        assert urls and (urls[0].startswith("git@") or urls[0].startswith("ssh://")), urls


@_REQUIRES_SSH
class TestAllowFallbackEscapeHatch:
    def test_explicit_ssh_with_allow_fallback_can_reach_https(self, tmp_clone_dir, isolated_env):
        ok, urls = _attempt_clone(  # noqa: RUF059
            "ssh://git@nonexistent-host-apm-test.invalid/foo/bar.git",
            target_dir=tmp_clone_dir / "c",
            allow_fallback=True,
        )
        assert any(u.startswith("https://") for u in urls), (
            "allow_fallback must permit cross-protocol retry; URLs tried: %s" % urls  # noqa: UP031
        )
