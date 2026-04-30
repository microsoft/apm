"""
E2E tests for Azure DevOps AAD bearer-token authentication.

These tests require the Azure CLI (`az`) to be installed and a logged-in
session against a tenant that has access to the test ADO repository. They
make real network calls to dev.azure.com.

Skip conditions:
    - `az` is not on PATH
    - `az account get-access-token` fails (not logged in)
    - APM_TEST_ADO_BEARER is not set to "1" (opt-in, since these tests need
      tenant context the test runner cannot itself control)

Maintainer note (#852):
    These tests run in CI only behind a Workload Identity Federation (WIF)
    service connection that maintainers must provision (see the
    `ado-bearer-tests` job in `.github/workflows/auth-acceptance.yml` for
    setup steps). External contributors will see the job skipped, which is
    expected -- the bearer-token logic is exhaustively unit-tested in
    `tests/unit/test_azure_cli.py` and `tests/unit/test_auth.py`. Live
    network coverage is the maintainer's responsibility.

Refs: microsoft/apm#852
"""

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

# ---------------------------------------------------------------------------
# Module-level skip conditions
# ---------------------------------------------------------------------------

_AZ_BIN = shutil.which("az")
_AZ_AVAILABLE = _AZ_BIN is not None
_BEARER_REACHABLE = False

if _AZ_AVAILABLE and os.getenv("APM_TEST_ADO_BEARER") == "1":
    try:
        _probe = subprocess.run(
            [
                _AZ_BIN,
                "account",
                "get-access-token",
                "--resource",
                "499b84ac-1321-427f-aa17-267ca6975798",
                "--query",
                "accessToken",
                "-o",
                "tsv",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        _BEARER_REACHABLE = _probe.returncode == 0 and _probe.stdout.startswith("eyJ")
    except Exception:
        _BEARER_REACHABLE = False

pytestmark = pytest.mark.skipif(
    not (_AZ_AVAILABLE and _BEARER_REACHABLE and os.getenv("APM_TEST_ADO_BEARER") == "1"),
    reason="Requires az CLI logged in + APM_TEST_ADO_BEARER=1",
)


def run_apm(
    cmd: str, cwd: Path, env_overrides: dict, timeout: int = 90
) -> subprocess.CompletedProcess:
    """Run apm with a controlled env dict.

    env_overrides is merged into a copy of os.environ; values of None DELETE
    that key from the merged env.
    """
    apm_on_path = shutil.which("apm")
    if apm_on_path:
        apm_path = apm_on_path
    elif sys.platform == "win32":
        apm_path = str(Path(__file__).parent.parent.parent / ".venv" / "Scripts" / "apm.exe")
    else:
        apm_path = str(Path(__file__).parent.parent.parent / ".venv" / "bin" / "apm")

    env = {**os.environ}
    for k, v in env_overrides.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v

    return subprocess.run(
        # B4 #852: list-form (shell=False) avoids command injection via
        # CI-supplied repo names that may contain shell metacharacters.
        [apm_path, *shlex.split(cmd)],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        encoding="utf-8",
        errors="replace",
    )


def _init_project(project_dir: Path) -> None:
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "apm.yml").write_text(
        yaml.dump(
            {
                "name": "test-project",
                "version": "1.0.0",
                "dependencies": {"apm": [], "mcp": []},
            }
        )
    )


def _expected_path_parts_from_repo(repo: str) -> tuple[str, str, str]:
    """Derive the (org, project, repo) path parts from an ADO repo URL fragment.

    Accepts forms like:
      dev.azure.com/<org>/<project>/_git/<repo>
      <org>.visualstudio.com/<project>/_git/<repo>

    Mirrors how :func:`apm_cli.utils.github_host.parse_ado_url` normalizes
    the on-disk install path. Used by the bearer e2e tests so #856 review C2/C3
    can override the test repo via APM_TEST_ADO_REPO without editing source.
    """
    cleaned = repo.replace("https://", "").replace("http://", "")
    parts = cleaned.split("/")
    if not parts:
        raise ValueError(f"Cannot parse ADO repo: {repo!r}")
    host = parts[0]
    if host == "dev.azure.com":
        if len(parts) < 5 or parts[3] != "_git":
            raise ValueError(f"Expected dev.azure.com/<org>/<project>/_git/<repo>, got {repo!r}")
        return (parts[1], parts[2], parts[4])
    if host.endswith(".visualstudio.com"):
        if len(parts) < 4 or parts[2] != "_git":
            raise ValueError(f"Expected <org>.visualstudio.com/<project>/_git/<repo>, got {repo!r}")
        org = host.split(".", 1)[0]
        return (org, parts[1], parts[3])
    raise ValueError(f"Unrecognised ADO host {host!r} in {repo!r}")


# C2/C3 #856: read APM_TEST_ADO_REPO so the workflow can override the
# test target via input without code change.
ADO_TEST_REPO = os.getenv(
    "APM_TEST_ADO_REPO",
    "dev.azure.com/dmeppiel-org/market-js-app/_git/compliance-rules",
)
EXPECTED_PATH_PARTS = _expected_path_parts_from_repo(ADO_TEST_REPO)


# ---------------------------------------------------------------------------
# T3H: bearer-only (no PAT, az logged in)
# ---------------------------------------------------------------------------


class TestBearerOnly:
    """Install an ADO package with NO ADO_APM_PAT set; bearer is the only path."""

    def test_install_via_bearer_only(self, tmp_path):
        project_dir = tmp_path / "bearer-only"
        _init_project(project_dir)

        result = run_apm(
            f'install --only apm "{ADO_TEST_REPO}"',
            project_dir,
            env_overrides={"ADO_APM_PAT": None},
        )

        assert result.returncode == 0, (
            f"Install failed (exit {result.returncode}).\n"
            f"STDOUT:\n{result.stdout}\n\nSTDERR:\n{result.stderr}"
        )

        installed = (
            project_dir
            / "apm_modules"
            / EXPECTED_PATH_PARTS[0]
            / EXPECTED_PATH_PARTS[1]
            / EXPECTED_PATH_PARTS[2]
        )
        assert installed.exists(), f"Expected {installed} to exist after bearer install"

    def test_verbose_shows_bearer_source(self, tmp_path):
        """apm install --verbose should reveal 'bearer from az cli' as the token source."""
        project_dir = tmp_path / "bearer-verbose"
        _init_project(project_dir)

        result = run_apm(
            f'install --only apm --verbose "{ADO_TEST_REPO}"',
            project_dir,
            env_overrides={"ADO_APM_PAT": None},
        )
        combined = result.stdout + result.stderr
        assert "AAD_BEARER_AZ_CLI" in combined or "bearer" in combined.lower(), (
            f"Verbose output should mention bearer source.\nOutput:\n{combined}"
        )


# ---------------------------------------------------------------------------
# T3I: stale PAT fallback to bearer
# ---------------------------------------------------------------------------


class TestStalePatFallback:
    """A bogus PAT triggers 401, then bearer fallback succeeds with a [!] warning."""

    def test_bogus_pat_falls_back_to_bearer(self, tmp_path):
        project_dir = tmp_path / "stale-pat"
        _init_project(project_dir)

        # 52-char PAT-shaped string; ADO will reject this with 401
        bogus = "x" * 52

        result = run_apm(
            f'install --only apm "{ADO_TEST_REPO}"',
            project_dir,
            env_overrides={"ADO_APM_PAT": bogus},
        )

        assert result.returncode == 0, (
            f"Stale-PAT fallback expected success (exit 0), got {result.returncode}.\n"
            f"STDOUT:\n{result.stdout}\n\nSTDERR:\n{result.stderr}"
        )

        installed = (
            project_dir
            / "apm_modules"
            / EXPECTED_PATH_PARTS[0]
            / EXPECTED_PATH_PARTS[1]
            / EXPECTED_PATH_PARTS[2]
        )
        assert installed.exists(), "Bearer fallback should have completed the install"

        combined = result.stdout + result.stderr
        # The stale-PAT diagnostic should have surfaced
        assert (
            "rejected" in combined.lower() and "az cli bearer" in combined.lower()
        ) or "ADO_APM_PAT" in combined, (
            f"Expected stale-PAT warning in output.\nOutput:\n{combined}"
        )


# ---------------------------------------------------------------------------
# T3J: wrong-tenant -> Case 2 error wording
# ---------------------------------------------------------------------------
# Note: This test cannot reliably switch the user's az session mid-test.
# It is documented but skipped in CI. Manual reproduction steps live in the
# PR test report under session-state/files/.


@pytest.mark.skip(reason="Requires manual tenant switch; reproduced manually in PR report")
class TestWrongTenant:
    def test_wrong_tenant_renders_case_2_error(self, tmp_path):
        pass


# ---------------------------------------------------------------------------
# T3K: PAT regression (PAT path must be unchanged)
# ---------------------------------------------------------------------------


class TestPatRegression:
    """With a valid PAT set, ADO_APM_PAT path must work exactly as before."""

    @pytest.mark.skipif(
        not os.getenv("ADO_APM_PAT"),
        reason="ADO_APM_PAT not set; regression test requires real PAT",
    )
    def test_pat_install_unchanged(self, tmp_path):
        project_dir = tmp_path / "pat-regress"
        _init_project(project_dir)

        # Use the user's real PAT as-is
        result = run_apm(
            f'install --only apm "{ADO_TEST_REPO}"',
            project_dir,
            env_overrides={},
        )
        assert result.returncode == 0, (
            f"PAT install regressed (exit {result.returncode}).\n"
            f"STDOUT:\n{result.stdout}\n\nSTDERR:\n{result.stderr}"
        )
        installed = (
            project_dir
            / "apm_modules"
            / EXPECTED_PATH_PARTS[0]
            / EXPECTED_PATH_PARTS[1]
            / EXPECTED_PATH_PARTS[2]
        )
        assert installed.exists()


# ---------------------------------------------------------------------------
# Issue #1015 regression tests
# ---------------------------------------------------------------------------


class TestIssue1015BearerInstallRegression:
    """#1015 bug repro: bearer-only install (no PAT) should succeed."""

    def test_bearer_only_install_succeeds(self, tmp_path):
        """With ADO_APM_PAT deleted, install via az cli bearer should work."""
        project_dir = tmp_path / "issue-1015-repro"
        _init_project(project_dir)

        result = run_apm(
            f'install --only apm "{ADO_TEST_REPO}"',
            project_dir,
            env_overrides={"ADO_APM_PAT": None},
        )

        assert result.returncode == 0, (
            f"#1015 regression: bearer-only install failed (exit {result.returncode}).\n"
            f"STDOUT:\n{result.stdout}\n\nSTDERR:\n{result.stderr}"
        )

        installed = (
            project_dir
            / "apm_modules"
            / EXPECTED_PATH_PARTS[0]
            / EXPECTED_PATH_PARTS[1]
            / EXPECTED_PATH_PARTS[2]
        )
        assert installed.exists(), f"Expected {installed} after bearer-only install"


class TestIssue1015DiagnosticOnAuthFailure:
    """#1015: auth failure surfaces actionable diagnostics, not legacy wording."""

    def test_bogus_pat_no_az_shows_diagnostic(self, tmp_path):
        """With a bogus PAT and az NOT available, stderr shows diagnostics."""
        project_dir = tmp_path / "issue-1015-diag"
        _init_project(project_dir)

        # 52-char PAT-shaped string that ADO will reject, plus clear AZURE_*
        # vars so az-cli bearer is unavailable for fallback.
        bogus = "x" * 52
        result = run_apm(
            f'install --only apm "{ADO_TEST_REPO}"',
            project_dir,
            env_overrides={
                "ADO_APM_PAT": bogus,
                # Suppress az cli so bearer fallback also fails
                "AZURE_CONFIG_DIR": "/nonexistent",
                "PATH": os.environ.get("PATH", "").replace(
                    str(Path(_AZ_BIN).parent) if _AZ_BIN else "", ""
                ),
            },
        )
        combined = result.stdout + result.stderr

        # Must NOT contain the legacy ambiguous wording
        assert "not accessible or doesn't exist" not in combined.lower(), (
            f"Legacy wording should not appear. Output:\n{combined}"
        )
        # Must NOT contain double-wrapped error
        assert "Failed to install APM dependencies: Failed to resolve" not in combined, (
            f"Double-wrapped error should not appear. Output:\n{combined}"
        )
        # Should contain auth-related diagnostic markers
        has_diagnostic = (
            "ADO_APM_PAT" in combined
            or "authentication" in combined.lower()
            or "az login" in combined
        )
        assert has_diagnostic, f"Expected auth diagnostic in output. Output:\n{combined}"
        assert result.returncode != 0


class TestIssue1015UpdatePreflightAbort:
    """#1015: apm install --update aborts cleanly on auth failure."""

    def test_update_aborts_no_files_modified(self, tmp_path):
        """--update with no auth must exit non-zero and not modify files."""
        import hashlib

        project_dir = tmp_path / "issue-1015-update"
        _init_project(project_dir)

        # Create a lock file to simulate an existing project
        lock_path = project_dir / "apm.lock.yaml"
        lock_path.write_text("# test lockfile\n")

        # Snapshot file contents before the run
        apm_yml_hash = hashlib.sha256((project_dir / "apm.yml").read_bytes()).hexdigest()
        lock_hash = hashlib.sha256(lock_path.read_bytes()).hexdigest()
        modules_existed = (project_dir / "apm_modules").exists()

        # Use a made-up ADO repo that the bearer cannot access
        # and suppress all auth so it fails cleanly.
        result = run_apm(
            'install --only apm --update "dev.azure.com/nonexistent-org-xyzzy/fake/_git/fake"',
            project_dir,
            env_overrides={
                "ADO_APM_PAT": None,
                "AZURE_CONFIG_DIR": "/nonexistent",
                "PATH": os.environ.get("PATH", "").replace(
                    str(Path(_AZ_BIN).parent) if _AZ_BIN else "", ""
                ),
            },
        )

        assert result.returncode != 0, (
            f"Expected non-zero exit on auth failure, got {result.returncode}.\n"
            f"STDOUT:\n{result.stdout}\n\nSTDERR:\n{result.stderr}"
        )

        combined = result.stdout + result.stderr
        # Verify "No files were modified" message
        assert "No files were modified" in combined, (
            f"Expected 'No files were modified' in output. Output:\n{combined}"
        )

        # Verify files are byte-identical to pre-run state
        assert hashlib.sha256((project_dir / "apm.yml").read_bytes()).hexdigest() == apm_yml_hash, (
            "apm.yml was modified!"
        )
        assert hashlib.sha256(lock_path.read_bytes()).hexdigest() == lock_hash, (
            "apm.lock.yaml was modified!"
        )
        if not modules_existed:
            assert not (project_dir / "apm_modules").exists(), (
                "apm_modules/ was created despite auth failure!"
            )


class TestIssue1015PatRegressionExplicit:
    """#1015: PAT auth_scheme=basic still works after bearer fix."""

    @pytest.mark.skipif(
        not os.getenv("ADO_APM_PAT"),
        reason="ADO_APM_PAT not set; PAT regression test requires real PAT",
    )
    def test_pat_still_works_after_bearer_fix(self, tmp_path):
        project_dir = tmp_path / "issue-1015-pat"
        _init_project(project_dir)

        result = run_apm(
            f'install --only apm "{ADO_TEST_REPO}"',
            project_dir,
            env_overrides={},  # Use real PAT from env
        )
        assert result.returncode == 0, (
            f"PAT install regressed after bearer fix (exit {result.returncode}).\n"
            f"STDOUT:\n{result.stdout}\n\nSTDERR:\n{result.stderr}"
        )
        installed = (
            project_dir
            / "apm_modules"
            / EXPECTED_PATH_PARTS[0]
            / EXPECTED_PATH_PARTS[1]
            / EXPECTED_PATH_PARTS[2]
        )
        assert installed.exists()
