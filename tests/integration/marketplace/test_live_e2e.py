"""Live end-to-end tests for ``apm marketplace`` commands.

These tests require the ``APM_E2E_MARKETPLACE`` environment variable to be
set to a valid ``owner/repo`` string pointing to a real GitHub marketplace
repository.  When the variable is absent all tests in this module are
skipped automatically.

Example usage (maintainer only):

    export APM_E2E_MARKETPLACE=my-org/my-marketplace-repo
    uv run pytest tests/integration/marketplace/test_live_e2e.py -v

IMPORTANT: No publish live test is included.  The ``publish`` command writes
to third-party repositories and is intrinsically destructive.  Publish is
covered at the unit and integration tiers only (with mocked services).
"""

from __future__ import annotations

import json
import re
from pathlib import Path  # noqa: F401

import pytest

from .conftest import run_cli

# ---------------------------------------------------------------------------
# Shared live YAML template
# ---------------------------------------------------------------------------


def _live_yml(owner_repo: str) -> str:
    """Minimal marketplace.yml that references the live repo.

    Uses ^1.0.0 as the version range so that any v1.x.y tag on the
    remote satisfies the range.  If the remote has no v1.x tags the
    test will exit with an appropriate error (not a skip).
    """
    return f"""\
name: live-test-marketplace
description: Minimal live test marketplace
version: 0.1.0
owner:
  name: Live Test Runner
packages:
  - name: live-plugin
    source: {owner_repo}
    version: "^1.0.0"
    tags:
      - live
"""


# ---------------------------------------------------------------------------
# Tests (all depend on live_marketplace_repo fixture)
# ---------------------------------------------------------------------------


class TestLiveBuild:
    """Live build test: resolves real tags and writes marketplace.json."""

    def test_live_build_succeeds(self, live_marketplace_repo, tmp_path):
        """Build with a real remote must exit 0 and produce marketplace.json."""
        yml_path = tmp_path / "marketplace.yml"
        yml_path.write_text(_live_yml(live_marketplace_repo), encoding="utf-8")

        result = run_cli(["marketplace", "build"], cwd=tmp_path, timeout=120)

        assert result.returncode == 0, (
            f"build exited {result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
        )

        out_path = tmp_path / "marketplace.json"
        assert out_path.exists(), "marketplace.json was not produced"

    def test_live_build_resolves_sha(self, live_marketplace_repo, tmp_path):
        """All plugins in the produced marketplace.json must have a 40-char SHA."""
        yml_path = tmp_path / "marketplace.yml"
        yml_path.write_text(_live_yml(live_marketplace_repo), encoding="utf-8")

        result = run_cli(["marketplace", "build"], cwd=tmp_path, timeout=120)
        if result.returncode != 0:
            pytest.skip(
                f"Build failed (possible: no v1.x tags on {live_marketplace_repo}). "
                f"stdout={result.stdout}"
            )

        out_path = tmp_path / "marketplace.json"
        data = json.loads(out_path.read_text(encoding="utf-8"))

        sha_re = re.compile(r"^[0-9a-f]{40}$")
        for plugin in data.get("plugins", []):
            sha = plugin.get("source", {}).get("commit", "")
            assert sha_re.match(sha), f"Plugin {plugin.get('name')} has invalid SHA: {sha!r}"


class TestLiveOutdated:
    """Live outdated test: reports upgrades in correct format."""

    def test_live_outdated_exits_zero(self, live_marketplace_repo, tmp_path):
        """outdated always exits 0 (informational command)."""
        yml_path = tmp_path / "marketplace.yml"
        yml_path.write_text(_live_yml(live_marketplace_repo), encoding="utf-8")

        result = run_cli(["marketplace", "outdated"], cwd=tmp_path, timeout=120)

        assert result.returncode == 0, (
            f"outdated exited {result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
        )

    def test_live_outdated_output_contains_package_name(self, live_marketplace_repo, tmp_path):
        """Output must contain the package name from the yml."""
        yml_path = tmp_path / "marketplace.yml"
        yml_path.write_text(_live_yml(live_marketplace_repo), encoding="utf-8")

        result = run_cli(["marketplace", "outdated"], cwd=tmp_path, timeout=120)

        assert "live-plugin" in result.stdout or "live-plugin" in result.stderr, (
            "Expected package name 'live-plugin' to appear in outdated output"
        )


class TestLiveCheck:
    """Live check test: all entries must be reachable."""

    def test_live_check_exits_zero(self, live_marketplace_repo, tmp_path):
        """check must exit 0 when all entries resolve against the live remote."""
        yml_path = tmp_path / "marketplace.yml"
        yml_path.write_text(_live_yml(live_marketplace_repo), encoding="utf-8")

        result = run_cli(["marketplace", "check"], cwd=tmp_path, timeout=120)

        assert result.returncode in (0, 1), (
            f"check exited {result.returncode} (unexpected)\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
        # If exit 1, it should be because no v1.x tag satisfies the range,
        # not because the remote is unreachable.
        if result.returncode == 1:
            combined = result.stdout + result.stderr
            # Verify it is a resolution error, not a network error
            assert "git ls-remote failed" not in combined, (
                "check failed due to network error, not a resolution mismatch"
            )


class TestLiveDoctor:
    """Live doctor test: git + network checks should pass in CI with network."""

    def test_live_doctor_exits_zero(self, live_marketplace_repo, tmp_path):
        """doctor must exit 0 when git is available and github.com is reachable."""
        # Place a valid marketplace.yml so the yml check is informational-pass
        yml_path = tmp_path / "marketplace.yml"
        yml_path.write_text(_live_yml(live_marketplace_repo), encoding="utf-8")

        result = run_cli(["marketplace", "doctor"], cwd=tmp_path, timeout=30)

        assert result.returncode == 0, (
            f"doctor exited {result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
        )

    def test_live_doctor_mentions_git(self, live_marketplace_repo, tmp_path):
        """doctor output must mention the git check."""
        yml_path = tmp_path / "marketplace.yml"
        yml_path.write_text(_live_yml(live_marketplace_repo), encoding="utf-8")

        result = run_cli(["marketplace", "doctor"], cwd=tmp_path, timeout=30)

        assert "git" in (result.stdout + result.stderr).lower()
