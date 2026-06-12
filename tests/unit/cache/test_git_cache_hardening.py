"""Supply-chain hardening tests for GitCache subprocess invocations.

GitCache is the single chokepoint through which APM invokes ``git``
against caller-supplied URLs. A malicious upstream could ship hook
scripts (``.git/hooks/post-checkout``) or attacker-controlled
submodule URLs that would trigger arbitrary code execution on clone
or checkout. These tests assert that every git subprocess invoked
by GitCache carries:

- ``-c core.hooksPath=/dev/null`` -- disables hook execution
- ``-c submodule.recurse=false`` -- prevents submodule recursion

and that every ``git clone`` call also carries
``--no-recurse-submodules`` so the flag is explicit even if a future
git release flips its default.

These guards are on-path for the generic-git marketplace registration
path (``apm marketplace add <untrusted-url>``) -- without them, any
user who registers a malicious marketplace would execute attacker code
on the next ``apm marketplace update``.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from unittest.mock import MagicMock, patch

from apm_cli.cache.git_cache import GitCache, _safe_git_args


def _all_cmd_argvs(mock_run: MagicMock) -> list[list[str]]:
    """Extract every argv passed to ``subprocess.run`` across all calls."""
    argvs = []
    for call in mock_run.call_args_list:
        # subprocess.run can be called positionally or with `args=`
        argv = call.args[0] if call.args else call.kwargs.get("args")
        if isinstance(argv, list):
            argvs.append(argv)
    return argvs


class TestSafeGitArgs:
    def test_includes_hooks_path_dev_null(self) -> None:
        args = _safe_git_args()
        assert "-c" in args
        assert "core.hooksPath=/dev/null" in args

    def test_includes_submodule_recurse_false(self) -> None:
        args = _safe_git_args()
        assert "submodule.recurse=false" in args


class TestLsRemoteHardening:
    @patch("subprocess.run")
    def test_ls_remote_carries_safe_args(self, mock_run: MagicMock, tmp_path: Path) -> None:
        cache = GitCache(tmp_path)
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=f"{'c' * 40}\trefs/heads/main\n",
            stderr="",
        )
        cache._resolve_sha("https://evil.example.com/o/r", "main")

        argvs = _all_cmd_argvs(mock_run)
        assert argvs, "ls-remote subprocess.run was not invoked"
        argv = argvs[0]
        assert "ls-remote" in argv
        assert "core.hooksPath=/dev/null" in argv
        assert "submodule.recurse=false" in argv


class TestCloneHardening:
    """Every clone subprocess invocation must disable hooks and submodules."""

    @patch("apm_cli.cache.git_cache.atomic_land", return_value=True)
    @patch("apm_cli.cache.git_cache.verify_checkout_sha", return_value=True)
    @patch("subprocess.run")
    def test_bare_clone_carries_safe_args_and_no_recurse(
        self,
        mock_run: MagicMock,
        _verify: MagicMock,
        _land: MagicMock,
        tmp_path: Path,
    ) -> None:
        cache = GitCache(tmp_path)
        sha = "a" * 40
        # ls-remote -> bare clone -> local clone -> checkout
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=f"{sha}\trefs/heads/main\n",
            stderr="",
        )
        # Mocked subprocess.run doesn't actually materialise files;
        # we only need to inspect the argvs that were attempted.
        with contextlib.suppress(Exception):
            cache.get_checkout("https://evil.example.com/o/r", "main")

        argvs = _all_cmd_argvs(mock_run)
        clone_argvs = [a for a in argvs if "clone" in a]
        assert clone_argvs, "no clone subprocess invoked"
        for argv in clone_argvs:
            assert "core.hooksPath=/dev/null" in argv, argv
            assert "submodule.recurse=false" in argv, argv
            assert "--no-recurse-submodules" in argv, argv


class TestFetchHardening:
    @patch("subprocess.run")
    def test_fetch_into_bare_carries_safe_args(self, mock_run: MagicMock, tmp_path: Path) -> None:
        cache = GitCache(tmp_path)
        # _bare_has_sha returns False, then fetch is invoked.
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")

        bare_dir = tmp_path / "bare"
        bare_dir.mkdir()
        # We mocked subprocess.run to return non-zero, which makes
        # check=True raise. We only need the argv that was attempted.
        with contextlib.suppress(Exception):
            cache._fetch_into_bare_locked(
                bare_dir,
                "https://evil.example.com/o/r",
                "a" * 40,
            )

        argvs = _all_cmd_argvs(mock_run)
        fetch_argvs = [a for a in argvs if "fetch" in a]
        assert fetch_argvs, "no fetch subprocess invoked"
        for argv in fetch_argvs:
            assert "core.hooksPath=/dev/null" in argv, argv
            assert "submodule.recurse=false" in argv, argv


class TestCheckoutHardening:
    """The bare-to-working-dir clone in _create_checkout must be hardened."""

    @patch("apm_cli.cache.git_cache.atomic_land", return_value=True)
    @patch("apm_cli.cache.git_cache.verify_checkout_sha", return_value=True)
    @patch("subprocess.run")
    def test_create_checkout_clone_and_checkout_carry_safe_args(
        self,
        mock_run: MagicMock,
        _verify: MagicMock,
        _land: MagicMock,
        tmp_path: Path,
    ) -> None:
        cache = GitCache(tmp_path)
        sha = "a" * 40
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=f"{sha}\trefs/heads/main\n",
            stderr="",
        )
        with contextlib.suppress(Exception):
            cache.get_checkout("https://evil.example.com/o/r", "main")

        argvs = _all_cmd_argvs(mock_run)
        local_clone_argvs = [a for a in argvs if "clone" in a and "--local" in a]
        checkout_argvs = [a for a in argvs if "checkout" in a and "clone" not in a]
        assert local_clone_argvs, "expected a local clone argv"
        for argv in local_clone_argvs + checkout_argvs:
            assert "core.hooksPath=/dev/null" in argv, argv
            assert "submodule.recurse=false" in argv, argv
        for argv in local_clone_argvs:
            assert "--no-recurse-submodules" in argv, argv
