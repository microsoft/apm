"""End-to-end tests for #1159 install-side silent-skip fix.

The audit-side counterpart lives in ``test_audit_silent_skip_e2e.py``;
this file defends the install pipeline parity.  The previous
integration coverage in ``test_policy_install_e2e.py::TestI17NoGitRemote``
mocks ``discover_policy_with_chain`` at both
``policy.discovery`` and ``policy.install_preflight`` import sites.
That mocking proves the routing constant but does NOT exercise the
real ``git remote get-url origin`` -> ``no_git_remote`` outcome wiring
that #1159 fixes.  These tests run a real ``git init`` (no remote
configured) so:

  * the ``git remote`` introspection in ``_extract_org_from_git_remote``
    runs for real,
  * ``discover_policy_with_chain`` returns
    ``PolicyFetchResult(outcome="no_git_remote")`` end-to-end, and
  * the ``policy.fetch_failure_default=block`` knob in the project
    ``apm.yml`` raises ``PolicyViolationError`` through the
    ``policy_gate`` pipeline phase (the install-side mirror of the
    audit ``[x] ... policy.fetch_failure_default=block`` contract).

Note on pipeline ordering: in ``apm install`` the resolve phase
(which constructs the downloader and downloads packages) runs BEFORE
``policy_gate``. The block contract is therefore "non-zero exit with
the verbatim ``policy.fetch_failure_default=block`` message," not
"downloader was never called". The audit-side test asserts the
stronger pre-fetch contract because audit's order is reversed
(discovery -> gate -> render).
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from apm_cli.models.apm_package import clear_apm_yml_cache

# Mock targets -- bypass network/registry but let policy discovery run real.
_PATCH_UPDATES = "apm_cli.commands._helpers.check_for_updates"
_PATCH_VALIDATE_PKG = "apm_cli.commands.install._validate_package_exists"
_PATCH_DOWNLOADER = "apm_cli.deps.github_downloader.GitHubPackageDownloader"


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_apm_yml_cache()
    yield
    clear_apm_yml_cache()


def _git_init_no_remote(path: Path) -> None:
    """Initialise a real git repo with no remote configured."""
    subprocess.run(
        ["git", "init", "--quiet"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=path,
        check=True,
        capture_output=True,
    )


def _write_block_project(project: Path) -> None:
    """Write apm.yml with a fetch_failure_default=block knob."""
    (project / "apm.yml").write_text(
        textwrap.dedent("""\
            name: test-project
            version: '1.0.0'
            dependencies:
              apm:
                - owner/repo#v1.0.0
            policy:
              fetch_failure_default: block
        """),
        encoding="utf-8",
    )
    (project / ".github").mkdir(exist_ok=True)


class TestInstallNoGitRemoteBlockE2E:
    """Real ``git init`` (no remote) + fetch_failure_default=block.

    Pre-#1159 this combination silently proceeded with no enforcement.
    Post-fix it must raise ``PolicyViolationError`` and exit non-zero
    BEFORE any download attempt -- proving the routing reaches both
    the install_preflight and the policy_gate codepaths.
    """

    def test_block_raises_after_discovery_with_no_remote(self, runner, tmp_path, monkeypatch):
        """Real ``git init`` (no remote) + project ``fetch_failure_default=block``
        must raise ``PolicyViolationError`` from the policy_gate phase
        and exit non-zero. The error message contract is the verbatim
        ``policy.fetch_failure_default=block`` token.
        """
        from apm_cli.cli import cli

        monkeypatch.chdir(tmp_path)
        _git_init_no_remote(tmp_path)
        _write_block_project(tmp_path)

        with (
            patch(_PATCH_UPDATES, return_value=None),
            patch(_PATCH_VALIDATE_PKG, return_value=True),
            patch(_PATCH_DOWNLOADER) as mock_dl,
        ):
            # Make the downloader a no-op success so resolve phase
            # completes and we reach policy_gate.  The contract under
            # test is the policy_gate behaviour, not download timing.
            mock_dl.return_value.download_package.return_value = None
            result = runner.invoke(cli, ["install"], catch_exceptions=False)

        assert result.exit_code != 0, (
            f"Expected non-zero exit; got 0.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        combined = (result.stdout or "") + (result.stderr or "")
        assert "policy.fetch_failure_default=block" in combined, (
            f"Expected fetch-failure block message in output:\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

    def test_default_warn_proceeds_without_remote(self, runner, tmp_path, monkeypatch):
        """No fetch_failure_default knob -> warn-and-proceed (legacy default)."""
        from apm_cli.cli import cli

        monkeypatch.chdir(tmp_path)
        _git_init_no_remote(tmp_path)
        # Same project but WITHOUT the block knob -- exercises the warn
        # default for symmetry with the block test above.
        (tmp_path / "apm.yml").write_text(
            textwrap.dedent("""\
                name: test-project
                version: '1.0.0'
                dependencies:
                  apm:
                    - owner/repo#v1.0.0
            """),
            encoding="utf-8",
        )
        (tmp_path / ".github").mkdir(exist_ok=True)

        with (
            patch(_PATCH_UPDATES, return_value=None),
            patch(_PATCH_VALIDATE_PKG, return_value=True),
            patch(_PATCH_DOWNLOADER) as mock_dl,
        ):
            # Make download a no-op success so we exit cleanly past the
            # policy gate.
            mock_dl.return_value.download_package.side_effect = Exception("downloader bypass")
            result = runner.invoke(cli, ["install"], catch_exceptions=False)

        # Warn path: install MAY fail at download time (we forced an
        # exception on the mocked downloader), but the failure must NOT
        # be a fetch_failure_default=block PolicyViolationError --
        # discovery must proceed past the no_git_remote outcome.
        combined = (result.stdout or "") + (result.stderr or "")
        assert "policy.fetch_failure_default=block" not in combined, (
            f"Default warn must not raise the block error:\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
