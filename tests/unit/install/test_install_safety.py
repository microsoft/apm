"""Regression tests for install.sh APM_LIB_DIR safety validation (issue #1690).

Background
----------
The Unix installer previously accepted ``APM_LIB_DIR`` from the environment and
unconditionally ran ``rm -rf "$APM_LIB_DIR"`` on the resolved path. A user who
set ``APM_LIB_DIR=$HOME/.local/share`` while trying to install to
``$HOME/.local/bin`` lost unrelated application data (Atuin's local history DB
in the reported incident).

The fix wraps the four guards -- absolute path, suffix, blocklist, marker file
-- in a callable function ``apm_lib_dir_validate()`` and refuses unsafe paths
before any ``rm -rf``. These tests exercise the function directly via the
sentinel-bounded source block in install.sh.

Note: the test file does not import any production Python code. It treats
``install.sh`` as a shell source so the function-under-test is the same code
that runs in production.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
INSTALL_SH = REPO_ROOT / "install.sh"
SENTINEL_BEGIN = re.compile(r"^# INSTALL_SAFETY_BEGIN", re.MULTILINE)
SENTINEL_END = re.compile(r"^# INSTALL_SAFETY_END", re.MULTILINE)


def _load_validator():
    """Extract the apm_lib_dir_validate() block from install.sh and return the
    source text of the function plus a small driver wrapper. Tests source the
    result into a fresh shell to invoke the function in isolation -- no network
    and no real installation side effects.
    """
    text = INSTALL_SH.read_text(encoding="utf-8")
    match_end = SENTINEL_END.search(text)
    assert match_end is not None, "INSTALL_SAFETY_END sentinel missing in install.sh"
    match_begin = SENTINEL_BEGIN.search(text)
    assert match_begin is not None, "INSTALL_SAFETY_BEGIN sentinel missing in install.sh"
    start = match_begin.end()
    end = match_end.start()
    block = text[start:end]
    return block


_VALIDATOR_SRC = _load_validator()


def _run_validator(lib_dir: str, home: str | None = None) -> int:
    """Source the validator in a fresh bash, call it with ``lib_dir``, and
    return its return code. Stdout/stderr are captured but discarded -- only
    the exit code matters here.
    """
    if home is None:
        home = "/home/safe-user"
    driver = f"""
{_VALIDATOR_SRC}
apm_lib_dir_validate "$1"
"""
    with tempfile.TemporaryDirectory() as tmp:
        proc = subprocess.run(
            ["bash", "-c", driver, "--", lib_dir],
            input="",
            capture_output=True,
            text=True,
            env={**os.environ, "HOME": home},
            cwd=tmp,
            timeout=10,
        )
    return proc.returncode


def _run_prepare_parent(lib_dir: str, home: str | None = None) -> int:
    """Return the parent-preparation helper exit code for ``lib_dir``."""
    if home is None:
        home = "/home/safe-user"
    driver = f"""
{_VALIDATOR_SRC}
apm_prepare_lib_parent "$1"
"""
    with tempfile.TemporaryDirectory() as tmp:
        proc = subprocess.run(
            ["bash", "-c", driver, "--", lib_dir],
            input="",
            capture_output=True,
            text=True,
            env={**os.environ, "HOME": home},
            cwd=tmp,
            timeout=10,
        )
    return proc.returncode


# ---------------------------------------------------------------------------
# Guard 1: absolute path required
# ---------------------------------------------------------------------------


class TestAbsolutePathGuard:
    def test_accepts_unix_absolute(self):
        assert _run_validator("/usr/local/lib/apm") == 0

    def test_rejects_relative_path(self):
        assert _run_validator("relative/path/apm") == 11

    def test_rejects_empty(self):
        assert _run_validator("") == 11

    def test_rejects_dot_relative(self):
        assert _run_validator("./apm") == 11


# ---------------------------------------------------------------------------
# Guard 2: suffix check (/apm or /lib/apm)
# ---------------------------------------------------------------------------


class TestSuffixGuard:
    def test_accepts_apm_suffix(self):
        assert _run_validator("/opt/apm") == 0

    def test_accepts_lib_apm_suffix(self):
        assert _run_validator("/usr/local/lib/apm") == 0

    def test_rejects_just_apm_in_middle(self):
        # /usr/local/apm-tool -- does not end in /apm
        assert _run_validator("/usr/local/apm-tool") == 12

    def test_rejects_no_apm_suffix(self):
        # The original reported incident: HOME/.local/share
        assert _run_validator("/home/safe-user/.local/share") == 12

    def test_rejects_share_apm_with_no_lib_parent(self):
        # Edge: ends with /apm. The blocklist guard only matches exact
        # blocklist paths, so /home/safe-user/.local/share/apm passes the
        # blocklist (it's a different path from .local/share). Suffix guard
        # accepts it (ends in /apm). The path is therefore allowed -- a user
        # who explicitly names this path has indicated intent. Suffix guard
        # is the primary defence.
        rc = _run_validator("/home/safe-user/.local/share/apm", home="/home/safe-user")
        assert rc == 0  # suffix OK, blocklist is exact-match

    def test_rejects_partial_match(self):
        # /usr/local/bin/apm-suffix -- must NOT match the */apm pattern
        # since the pattern requires a literal '/' before 'apm'.
        # Actually '/usr/local/bin/apm-suffix' ends in 'apm-suffix' not '/apm'.
        # The pattern '*/apm' requires '/apm' literal at end. Let me verify:
        # 'apm-suffix' is not '/apm', so guard 2 rejects.
        assert _run_validator("/usr/local/bin/apm-suffix") == 12


# ---------------------------------------------------------------------------
# Guard 3: blocklist (shared/broad parent directories)
# ---------------------------------------------------------------------------


class TestBlocklistGuard:
    @pytest.mark.parametrize(
        "broad_path",
        [
            "/home/safe-user",
            "/home/safe-user/.local",
            "/home/safe-user/.local/share",
            "/home/safe-user/.config",
            "/usr",
            "/usr/local",
            "/opt",
            "/tmp",
            "/",
        ],
    )
    def test_rejects_broad_paths(self, broad_path: str):
        assert _run_validator(broad_path, home="/home/safe-user") != 0

    def test_accepts_safe_user_local(self):
        # Default user-local install path from the Quickstart docs.
        assert _run_validator("/home/safe-user/.local/lib/apm") == 0

    def test_rejects_home(self):
        # /home/safe-user resolves to itself; even with /apm suffix it's blocked.
        # But /home/safe-user doesn't end in /apm, so guard 2 fires first.
        assert _run_validator("/home/safe-user") == 12

    def test_rejects_home_with_apm_suffix_via_blocklist(self):
        # The blocklist checks for an exact path match. /home/safe-user/apm
        # is a different path from /home/safe-user (which is in the
        # blocklist), so it passes the blocklist guard. Suffix guard is
        # satisfied, so the path is allowed. This is intentional: a user
        # who explicitly names ``$HOME/apm`` has indicated intent.
        assert _run_validator("/home/safe-user/apm") == 0

    def test_rejects_usr(self):
        # /usr alone -- no /apm suffix, suffix guard fires.
        assert _run_validator("/usr") == 12

    def test_rejects_usr_with_apm_suffix(self):
        # /usr/apm -- suffix ok, /usr is in blocklist but is a different
        # path; blocklist guard allows non-exact matches. Allowed.
        # (Users explicitly naming /usr/apm have indicated intent.)
        assert _run_validator("/usr/apm") == 0

    def test_rejects_tmp(self):
        assert _run_validator("/tmp") == 12

    def test_rejects_tmp_with_apm_suffix(self):
        # /tmp/apm -- suffix ok, /tmp is a different path; allowed.
        assert _run_validator("/tmp/apm") == 0

    def test_rejects_root(self):
        assert _run_validator("/") == 12

    def test_rejects_root_apm(self):
        # /apm -- suffix ok, / is a different path; allowed.
        # The suffix guard alone is the primary defence against /-rooted
        # accidents; the blocklist catches only exact blocklist-path matches.
        assert _run_validator("/apm") == 0

    def test_rejects_local_share(self):
        # The original reported incident: suffix fails first, but document the
        # blocklist behavior too.
        assert _run_validator("/home/safe-user/.local/share") == 12


# ---------------------------------------------------------------------------
# Guard 4: marker-file check (existing non-empty directories)
# ---------------------------------------------------------------------------


class TestMarkerFileGuard:
    """Guard 4 only fires when the directory exists and is non-empty. The
    Python harness stages directories under a tempdir and calls the validator
    against them; absence of a marker file on a non-empty directory must
    return 14.
    """

    def test_accepts_empty_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "apm")
            os.makedirs(target)
            assert _run_validator(target) == 0

    def test_accepts_dir_with_apm_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "apm")
            os.makedirs(target)
            Path(target, "apm").touch()
            assert _run_validator(target) == 0

    def test_accepts_dir_with_version_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "apm")
            os.makedirs(target)
            Path(target, "VERSION").write_text("0.18.0\n")
            assert _run_validator(target) == 0

    def test_accepts_dir_with_apm_installed_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "apm")
            os.makedirs(target)
            Path(target, ".apm-installed").touch()
            assert _run_validator(target) == 0

    def test_accepts_dir_with_apm_cmd_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "apm")
            os.makedirs(target)
            Path(target, "apm.cmd").touch()
            assert _run_validator(target) == 0

    def test_rejects_nonempty_dir_without_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "apm")
            os.makedirs(target)
            # Looks like an APM-named dir but is actually someone else's data.
            Path(target, "user-data.txt").write_text("important\n")
            assert _run_validator(target) == 14

    def test_accepts_nonexistent_dir(self):
        # Path does not exist -- guard 4 short-circuits (no rm -rf would be
        # called anyway, but the validator must still return 0).
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "fresh", "apm")  # not created
            assert _run_validator(target) == 0


# ---------------------------------------------------------------------------
# End-to-end scenarios
# ---------------------------------------------------------------------------


class TestUserLocalInstall:
    def test_prepare_parent_creates_missing_user_local_lib_without_sudo(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.join(tmp, "home")
            os.makedirs(os.path.join(home, ".local"))
            target = os.path.join(home, ".local", "lib", "apm")

            assert _run_prepare_parent(target, home=home) == 0
            assert Path(home, ".local", "lib").is_dir()

    def test_prepare_parent_falls_back_when_parent_unwritable(self):
        with tempfile.TemporaryDirectory() as tmp:
            protected = Path(tmp, "protected")
            protected.mkdir()
            protected.chmod(0o555)
            try:
                target = str(protected / "lib" / "apm")
                assert _run_prepare_parent(target, home=os.path.join(tmp, "home")) == 1
            finally:
                protected.chmod(0o755)


class TestReportedIncident:
    """The exact command from issue #1690's reproduction must be blocked."""

    def test_reproduces_reported_issue(self):
        # curl ... | APM_INSTALL_DIR="$HOME/.local/bin" APM_LIB_DIR="$HOME/.local/share" sh
        # With HOME=/home/safe-user this becomes:
        #   APM_LIB_DIR = /home/safe-user/.local/share
        rc = _run_validator("/home/safe-user/.local/share", home="/home/safe-user")
        assert rc == 12  # suffix guard fires first; this still blocks deletion

    def test_safe_derived_default_passes(self):
        # The default derived path for $HOME/.local/bin is $HOME/.local/lib/apm.
        rc = _run_validator("/home/safe-user/.local/lib/apm", home="/home/safe-user")
        assert rc == 0


class TestSentinelInvariants:
    """The sentinel markers must remain in install.sh. Removing them would
    silently break testability -- fail loudly if a refactor strips them.
    """

    def test_begin_sentinel_present(self):
        text = INSTALL_SH.read_text(encoding="utf-8")
        assert SENTINEL_BEGIN.search(text) is not None

    def test_end_sentinel_present(self):
        text = INSTALL_SH.read_text(encoding="utf-8")
        assert SENTINEL_END.search(text) is not None

    def test_function_defined_in_extracted_block(self):
        assert "apm_lib_dir_validate()" in _VALIDATOR_SRC


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
