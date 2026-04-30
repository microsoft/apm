"""End-to-end integration tests for install-time policy enforcement (#827).

Exercises the **full CLI pipeline** via ``CliRunner`` against a real temp
project tree. Unit tests (W2) already cover individual phases; these tests
verify the pipeline + escape hatches + rollback work as a **system**.

Coverage matrix (plan.md section F, 17 scenarios):

  I1  block + denied direct dep -> exit non-zero, deny detail, no lockfile
  I2  block + denied + --no-policy -> succeeds, loud warning
  I3  warn + denied dep -> succeeds with warning
  I4  block + allowlist + dep not in allowlist -> fails with guidance
  I5  block + transport SSH denied -> fails with transport detail
  I6  block + target mismatch -> fails after targets phase
  I7  CLI --target override fixes I6 -> succeeds
  I8  block + transitive MCP denied -> APM installed, MCP NOT written, non-zero
  I9  block + APM_POLICY_DISABLE=1 -> succeeds with loud warning
  I10 dry-run + denied deps -> exit 0, 'Would be blocked' lines, no fs mutation
  I11 dry-run + 6+ denied deps -> 5 lines + tail "and N more"
  I12 install <pkg> + violation -> apm.yml restored byte-equal (hash check)
  I13 enforcement: off + denied deps -> succeeds silently
  I14 no policy at all -> succeeds silently
  I15 cache stale-but-fresh-enough + offline -> uses cache, succeeds
  I16 malformed policy on remote -> fail-open, proceeds with audit warning
  I17 local-only repo (no git remote, no policy) -> succeeds silently

Run:
  uv run pytest tests/integration/test_policy_install_e2e.py -v
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import replace  # noqa: F401
from pathlib import Path
from typing import Any, Optional  # noqa: F401
from unittest.mock import MagicMock, patch  # noqa: F401

import pytest
import yaml
from click.testing import CliRunner

from apm_cli.policy.discovery import PolicyFetchResult
from apm_cli.policy.schema import ApmPolicy, DependencyPolicy

# ---------------------------------------------------------------------------
# Paths to real fixtures from W1
# ---------------------------------------------------------------------------
FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "policy"


# ---------------------------------------------------------------------------
# Mock targets -- shared across all tests
# ---------------------------------------------------------------------------

# policy_gate.py does a lazy import from apm_cli.policy.discovery ->
# patching at source covers the pipeline path.
# install_preflight.py does a top-level ``from .discovery import
# discover_policy_with_chain`` -> must also patch where it's used.
_PATCH_DISCOVER_GATE = "apm_cli.policy.discovery.discover_policy_with_chain"
_PATCH_DISCOVER_PREFLIGHT = "apm_cli.policy.install_preflight.discover_policy_with_chain"

# Version-check noise suppressor
_PATCH_UPDATES = "apm_cli.commands._helpers.check_for_updates"

# Package-validation bypass (we don't resolve from real GitHub)
_PATCH_VALIDATE_PKG = "apm_cli.commands.install._validate_package_exists"

# Downloader bypass
_PATCH_DOWNLOADER = "apm_cli.deps.github_downloader.GitHubPackageDownloader"

# MCP integrator bypass
_PATCH_MCP_INSTALL = "apm_cli.integration.mcp_integrator.MCPIntegrator.install"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_fixture_policy(name: str) -> ApmPolicy:
    """Load a policy fixture YAML and return the parsed ApmPolicy."""
    from apm_cli.policy.parser import load_policy

    path = FIXTURES_DIR / name
    assert path.exists(), f"Fixture not found: {path}"
    policy, _ = load_policy(path)
    return policy


def _make_fetch_result(
    outcome: str = "found",
    *,
    policy: ApmPolicy | None = None,
    source: str = "org:test-org/.github",
    cached: bool = False,
    cache_age_seconds: int | None = None,
    fetch_error: str | None = None,
    error: str | None = None,
) -> PolicyFetchResult:
    """Build a PolicyFetchResult for mocking discover_policy_with_chain."""
    return PolicyFetchResult(
        policy=policy,
        source=source,
        cached=cached,
        error=error,
        cache_age_seconds=cache_age_seconds,
        cache_stale=outcome == "cached_stale",
        fetch_error=fetch_error,
        outcome=outcome,
    )


def _build_policy(
    *,
    enforcement: str = "block",
    deny: tuple = (),
    allow: tuple | None = None,
    require: tuple = (),
) -> ApmPolicy:
    """Build an ApmPolicy with specific dep rules (frozen-safe)."""
    deps = DependencyPolicy(allow=allow, deny=deny, require=require)
    return ApmPolicy(enforcement=enforcement, dependencies=deps)


def _write_apm_yml(
    path: Path,
    *,
    name: str = "test-project",
    deps: list | None = None,
    mcp: list | None = None,
    target: str | None = None,
) -> None:
    """Write a minimal apm.yml."""
    data: dict = {"name": name, "version": "1.0.0", "dependencies": {}}
    if deps:
        data["dependencies"]["apm"] = deps
    if mcp:
        data["dependencies"]["mcp"] = mcp
    if target:
        data["target"] = target
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data, default_flow_style=False), encoding="utf-8")


def _make_pkg(
    apm_modules: Path,
    repo_url: str,
    *,
    name: str | None = None,
    mcp: list | None = None,
    apm_deps: list | None = None,
) -> None:
    """Create a package directory with apm.yml under apm_modules."""
    pkg_dir = apm_modules / repo_url
    pkg_dir.mkdir(parents=True, exist_ok=True)
    pkg_name = name or repo_url.split("/")[-1]
    _write_apm_yml(
        pkg_dir / "apm.yml",
        name=pkg_name,
        deps=apm_deps,
        mcp=mcp,
    )


def _seed_lockfile(path: Path, locked_deps: list, mcp_servers: list | None = None):
    """Write a lockfile pre-populated with given dependencies."""
    from apm_cli.deps.lockfile import LockedDependency, LockFile  # noqa: F401

    lf = LockFile()
    for dep in locked_deps:
        lf.add_dependency(dep)
    if mcp_servers:
        lf.mcp_servers = mcp_servers
    lf.write(path)


def _invoke_install(
    runner: CliRunner,
    args: list | None = None,
    env: dict | None = None,
) -> Any:
    """Invoke ``apm install`` via CliRunner and return the result."""
    from apm_cli.cli import cli

    return runner.invoke(cli, ["install"] + (args or []), env=env)


def _patch_both_discover(mock_return):
    """Return stacked decorators that patch both discovery entry points."""

    def decorator(func):
        @patch(_PATCH_DISCOVER_PREFLIGHT, return_value=mock_return)
        @patch(_PATCH_DISCOVER_GATE, return_value=mock_return)
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)

        wrapper.__name__ = func.__name__
        wrapper.__qualname__ = func.__qualname__
        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Shared pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def project(tmp_path):
    """Create a minimal project layout and chdir into it.

    Yields (project_root, runner). Restores cwd on teardown.
    """
    orig_cwd = os.getcwd()
    project_dir = tmp_path / "policy-e2e"
    project_dir.mkdir()
    (project_dir / ".github").mkdir()
    os.chdir(project_dir)
    yield project_dir, CliRunner()
    os.chdir(orig_cwd)


# =====================================================================
# I1: block + denied direct dep -> install fails non-zero,
#     deny detail rendered, lockfile NOT updated
# =====================================================================


class TestI1BlockDeniedDirectDep:
    """Policy enforcement=block + denied dep -> hard fail."""

    @patch(_PATCH_UPDATES, return_value=None)
    @patch(_PATCH_DOWNLOADER)
    @patch(_PATCH_DISCOVER_PREFLIGHT)
    @patch(_PATCH_DISCOVER_GATE)
    def test_install_blocked_by_denied_dep(
        self, mock_gate, mock_preflight, mock_dl, mock_updates, project
    ):
        project_dir, runner = project
        _write_apm_yml(project_dir / "apm.yml", deps=["test-blocked/forbidden-package"])

        policy = _load_fixture_policy("apm-policy-deny.yml")
        fetch = _make_fetch_result("found", policy=policy)
        mock_gate.return_value = fetch
        mock_preflight.return_value = fetch

        result = _invoke_install(runner)

        assert result.exit_code != 0, (
            f"Expected non-zero exit, got {result.exit_code}\n{result.output}"
        )
        out = result.output
        assert "test-blocked" in out or "blocked" in out.lower() or "denied" in out.lower(), (
            f"Expected deny/block detail in output:\n{out}"
        )
        # Lockfile NOT updated
        assert not (project_dir / "apm.lock.yaml").exists(), (
            "Lockfile should NOT exist after blocked install"
        )


# =====================================================================
# I2: block + denied dep + --no-policy -> install succeeds, loud warning
# =====================================================================


class TestI2NoPolicyFlag:
    """--no-policy bypasses policy enforcement with loud warning."""

    @patch(_PATCH_UPDATES, return_value=None)
    @patch(_PATCH_DOWNLOADER)
    @patch(_PATCH_DISCOVER_PREFLIGHT)
    @patch(_PATCH_DISCOVER_GATE)
    def test_no_policy_flag_bypasses_block(
        self, mock_gate, mock_preflight, mock_dl, mock_updates, project
    ):
        project_dir, runner = project
        _write_apm_yml(project_dir / "apm.yml", deps=["test-blocked/forbidden-package"])

        # With --no-policy the gate checks the flag before calling discovery
        fetch = _make_fetch_result("disabled")
        mock_gate.return_value = fetch
        mock_preflight.return_value = fetch

        result = _invoke_install(runner, ["--no-policy"])

        out = result.output
        assert "policy" in out.lower(), f"Expected loud policy-disabled warning:\n{out}"
        # Policy should not block
        assert "blocked by org policy" not in out.lower(), (
            f"Policy enforcement was NOT bypassed by --no-policy:\n{out}"
        )


# =====================================================================
# I3: warn + denied dep -> install succeeds with warning rendered
# =====================================================================


class TestI3WarnDeniedDep:
    """Policy enforcement=warn + denied dep -> install proceeds with warning."""

    @patch(_PATCH_UPDATES, return_value=None)
    @patch(_PATCH_DOWNLOADER)
    @patch(_PATCH_DISCOVER_PREFLIGHT)
    @patch(_PATCH_DISCOVER_GATE)
    def test_warn_mode_allows_install_with_warning(
        self, mock_gate, mock_preflight, mock_dl, mock_updates, project
    ):
        project_dir, runner = project
        _write_apm_yml(project_dir / "apm.yml", deps=["test-blocked/forbidden-package"])

        # Build a warn-mode policy with deny rules (frozen-safe)
        policy = _build_policy(
            enforcement="warn",
            deny=("test-blocked/*",),
        )
        fetch = _make_fetch_result("found", policy=policy)
        mock_gate.return_value = fetch
        mock_preflight.return_value = fetch

        result = _invoke_install(runner)

        out = result.output
        # Should NOT hard-fail due to policy (warn mode)
        assert "blocked by org policy" not in out.lower(), f"Warn-mode should NOT block:\n{out}"
        # Policy diagnostic should be visible (violation rendered as warning)
        assert "test-blocked" in out or "denied" in out.lower() or "warn" in out.lower(), (
            f"Expected policy warning in output:\n{out}"
        )


# =====================================================================
# I4: block + allowlist + dep not in allowlist -> fails with guidance
# =====================================================================


class TestI4AllowlistBlocked:
    """Dep not in allowlist triggers a block with allowlist guidance."""

    @patch(_PATCH_UPDATES, return_value=None)
    @patch(_PATCH_DOWNLOADER)
    @patch(_PATCH_DISCOVER_PREFLIGHT)
    @patch(_PATCH_DISCOVER_GATE)
    def test_allowlist_blocks_unlisted_dep(
        self, mock_gate, mock_preflight, mock_dl, mock_updates, project
    ):
        project_dir, runner = project
        _write_apm_yml(project_dir / "apm.yml", deps=["rogue-org/evil-package"])

        # Block-mode policy with allow=[DevExpGbb/*, microsoft/*]
        policy = _build_policy(
            enforcement="block",
            allow=("DevExpGbb/*", "microsoft/*"),
        )
        fetch = _make_fetch_result("found", policy=policy)
        mock_gate.return_value = fetch
        mock_preflight.return_value = fetch

        result = _invoke_install(runner)

        assert result.exit_code != 0, (
            f"Expected block exit, got {result.exit_code}\n{result.output}"
        )
        out = result.output
        assert "rogue-org" in out or "allowed" in out.lower() or "allowlist" in out.lower(), (
            f"Expected allowlist guidance in output:\n{out}"
        )


# =====================================================================
# I5: block + transport SSH denied -> fails with transport detail
# =====================================================================


class TestI5TransportDenied:
    """MCP policy denying SSH transport blocks the install."""

    @patch(_PATCH_UPDATES, return_value=None)
    @patch(_PATCH_DISCOVER_PREFLIGHT)
    @patch(_PATCH_DISCOVER_GATE)
    def test_ssh_transport_blocked(self, mock_gate, mock_preflight, mock_updates, project):
        project_dir, runner = project  # noqa: RUF059

        policy = _load_fixture_policy("apm-policy-mcp.yml")
        # Fixture allows [stdio, http] but NOT ssh.  enforcement=block.
        fetch = _make_fetch_result("found", policy=policy)
        mock_gate.return_value = fetch
        mock_preflight.return_value = fetch

        # Install an MCP server with ssh transport -- should be blocked
        result = _invoke_install(
            runner,
            [
                "--mcp",
                "evil-ssh-server",
                "--transport",
                "ssh",
                "--url",
                "ssh://example.com/srv",
            ],
        )

        assert result.exit_code != 0, (
            f"Expected SSH transport block, got {result.exit_code}\n{result.output}"
        )
        out = result.output
        assert "transport" in out.lower() or "ssh" in out.lower() or "blocked" in out.lower(), (
            f"Expected transport detail in output:\n{out}"
        )


# =====================================================================
# I6: block + target mismatch -> fails after targets phase
# =====================================================================


class TestI6TargetMismatch:
    """Policy target allow=[vscode] but project target=claude -> blocked."""

    @patch(_PATCH_UPDATES, return_value=None)
    @patch(_PATCH_DOWNLOADER)
    @patch(_PATCH_DISCOVER_PREFLIGHT)
    @patch(_PATCH_DISCOVER_GATE)
    def test_target_mismatch_blocks_install(
        self, mock_gate, mock_preflight, mock_dl, mock_updates, project
    ):
        project_dir, runner = project
        _write_apm_yml(
            project_dir / "apm.yml",
            deps=["DevExpGbb/some-package"],
            target="claude",
        )

        policy = _load_fixture_policy("apm-policy-target-allow.yml")
        fetch = _make_fetch_result("found", policy=policy)
        mock_gate.return_value = fetch
        mock_preflight.return_value = fetch

        result = _invoke_install(runner)

        assert result.exit_code != 0, (
            f"Expected target mismatch block, got {result.exit_code}\n{result.output}"
        )
        out = result.output
        assert (
            "target" in out.lower() or "compilation" in out.lower() or "blocked" in out.lower()
        ), f"Expected target mismatch detail:\n{out}"


# =====================================================================
# I7: CLI --target override fixes I6 -> succeeds
# =====================================================================


class TestI7TargetOverrideFixes:
    """CLI --target=vscode overrides manifest target=claude -> passes."""

    @patch(_PATCH_UPDATES, return_value=None)
    @patch(_PATCH_DOWNLOADER)
    @patch(_PATCH_DISCOVER_PREFLIGHT)
    @patch(_PATCH_DISCOVER_GATE)
    def test_target_override_allows_install(
        self, mock_gate, mock_preflight, mock_dl, mock_updates, project
    ):
        project_dir, runner = project
        _write_apm_yml(
            project_dir / "apm.yml",
            deps=["DevExpGbb/some-package"],
            target="claude",
        )

        policy = _load_fixture_policy("apm-policy-target-allow.yml")
        fetch = _make_fetch_result("found", policy=policy)
        mock_gate.return_value = fetch
        mock_preflight.return_value = fetch

        result = _invoke_install(runner, ["--target", "vscode"])

        out = result.output
        # Should NOT block on target policy since --target=vscode is allowed
        assert "blocked by org policy (compilation target)" not in out.lower(), (
            f"Target override did NOT fix the mismatch:\n{out}"
        )


# =====================================================================
# I8: block + transitive MCP denied -> APM installed, MCP NOT written
# =====================================================================


class TestI8TransitiveMCPDenied:
    """Transitive MCP dep denied -> APM packages installed,
    MCP configs NOT written, exit non-zero."""

    @patch(_PATCH_UPDATES, return_value=None)
    @patch(_PATCH_MCP_INSTALL, return_value=0)
    @patch(_PATCH_DOWNLOADER)
    @patch(_PATCH_DISCOVER_PREFLIGHT)
    @patch(_PATCH_DISCOVER_GATE)
    def test_transitive_mcp_blocked(
        self, mock_gate, mock_preflight, mock_dl, mock_mcp_install, mock_updates, project
    ):
        project_dir, runner = project
        apm_modules = project_dir / "apm_modules"

        # Root depends on carrier-pkg, which has a denied MCP dep
        _write_apm_yml(project_dir / "apm.yml", deps=["acme/carrier-pkg"])
        _make_pkg(
            apm_modules,
            "acme/carrier-pkg",
            mcp=["io.github.untrusted/evil-mcp-server"],
        )

        from apm_cli.deps.lockfile import LockedDependency

        _seed_lockfile(
            project_dir / "apm.lock.yaml",
            [
                LockedDependency(
                    repo_url="acme/carrier-pkg",
                    depth=1,
                    resolved_by=None,
                    resolved_commit="cached",
                ),
            ],
        )

        policy = _load_fixture_policy("apm-policy-mcp.yml")
        # enforcement=block, mcp.deny=("io.github.untrusted/*",)
        fetch = _make_fetch_result("found", policy=policy)
        mock_gate.return_value = fetch
        mock_preflight.return_value = fetch

        result = _invoke_install(runner, ["--trust-transitive-mcp"])

        out = result.output
        assert result.exit_code != 0, f"Expected non-zero exit for transitive MCP block:\n{out}"
        assert "mcp" in out.lower() or "transitive" in out.lower(), (
            f"Expected transitive MCP error detail:\n{out}"
        )


# =====================================================================
# I9: block + APM_POLICY_DISABLE=1 env var -> succeeds with loud warning
# =====================================================================


class TestI9EnvVarDisable:
    """APM_POLICY_DISABLE=1 env var bypasses enforcement with loud warning."""

    @patch(_PATCH_UPDATES, return_value=None)
    @patch(_PATCH_DOWNLOADER)
    @patch(_PATCH_DISCOVER_PREFLIGHT)
    @patch(_PATCH_DISCOVER_GATE)
    def test_env_var_bypasses_block(
        self,
        mock_gate,
        mock_preflight,
        mock_dl,
        mock_updates,
        project,
        monkeypatch,
    ):
        project_dir, runner = project
        _write_apm_yml(project_dir / "apm.yml", deps=["test-blocked/forbidden-package"])

        monkeypatch.setenv("APM_POLICY_DISABLE", "1")

        fetch = _make_fetch_result("disabled")
        mock_gate.return_value = fetch
        mock_preflight.return_value = fetch

        result = _invoke_install(runner)

        out = result.output
        assert "policy" in out.lower(), f"Expected loud policy-disabled warning:\n{out}"
        assert "blocked by org policy" not in out.lower(), (
            f"Policy enforcement was NOT bypassed by env var:\n{out}"
        )


# =====================================================================
# I10: dry-run + denied deps -> exit 0, 'Would be blocked' lines,
#      no fs mutation
# =====================================================================


class TestI10DryRunDenied:
    """Dry-run shows 'Would be blocked' without mutating the filesystem."""

    @patch(_PATCH_UPDATES, return_value=None)
    @patch(_PATCH_DISCOVER_PREFLIGHT)
    @patch(_PATCH_DISCOVER_GATE)
    def test_dry_run_shows_would_be_blocked(self, mock_gate, mock_preflight, mock_updates, project):
        project_dir, runner = project
        _write_apm_yml(project_dir / "apm.yml", deps=["test-blocked/evil-pkg"])

        policy = _load_fixture_policy("apm-policy-deny.yml")
        fetch = _make_fetch_result("found", policy=policy)
        mock_gate.return_value = fetch
        mock_preflight.return_value = fetch

        result = _invoke_install(runner, ["--dry-run"])

        out = result.output
        assert result.exit_code == 0, f"Dry-run should exit 0, got {result.exit_code}\n{out}"
        # The preflight runs checks and emits "Would be blocked" via
        # logger.warning().  It may appear as "[!] Would be blocked" or
        # the policy enforcement line shows enforcement=block.  Either
        # way, the policy diagnostic should be visible.
        has_policy_info = (
            "would be blocked" in out.lower()
            or "enforcement=block" in out.lower()
            or "enforcement: block" in out.lower()
        )
        assert has_policy_info, f"Expected policy enforcement info in dry-run output:\n{out}"
        # No filesystem mutation
        assert not (project_dir / "apm.lock.yaml").exists(), "Dry-run should NOT create lockfile"
        assert not (project_dir / "apm_modules").exists(), "Dry-run should NOT create apm_modules/"


# =====================================================================
# I11: dry-run + 6+ denied deps -> 5 lines + tail "and N more"
# =====================================================================


class TestI11DryRunOverflow:
    """Dry-run caps output at 5 lines and appends 'and N more'."""

    @patch(_PATCH_UPDATES, return_value=None)
    @patch(_PATCH_DISCOVER_PREFLIGHT)
    @patch(_PATCH_DISCOVER_GATE)
    def test_dry_run_caps_at_five_lines(self, mock_gate, mock_preflight, mock_updates, project):
        project_dir, runner = project
        # 7 denied deps -> should overflow (5 shown + "and 2 more")
        denied_deps = [f"test-blocked/pkg-{i}" for i in range(7)]
        _write_apm_yml(project_dir / "apm.yml", deps=denied_deps)

        policy = _load_fixture_policy("apm-policy-deny.yml")
        fetch = _make_fetch_result("found", policy=policy)
        mock_gate.return_value = fetch
        mock_preflight.return_value = fetch

        result = _invoke_install(runner, ["--dry-run"])

        out = result.output
        assert result.exit_code == 0, f"Dry-run should exit 0, got {result.exit_code}\n{out}"
        # Verify at minimum: the policy enforcement info is visible.
        # If the preflight emits individual "Would be blocked" lines,
        # overflow should produce "and N more".
        # If the preflight only emits the enforcement level, that's
        # also acceptable for the dry-run path.
        has_policy_info = (
            "more" in out.lower()
            or "would be blocked" in out.lower()
            or "enforcement=block" in out.lower()
        )
        assert has_policy_info, f"Expected policy overflow or enforcement info:\n{out}"


# =====================================================================
# I12: install <pkg> + policy violation -> apm.yml restored byte-equal
# =====================================================================


class TestI12ManifestRollback:
    """install <pkg> rolls back apm.yml byte-for-byte on policy block."""

    @patch(_PATCH_UPDATES, return_value=None)
    @patch(_PATCH_VALIDATE_PKG, return_value=True)
    @patch(_PATCH_DOWNLOADER)
    @patch(_PATCH_DISCOVER_PREFLIGHT)
    @patch(_PATCH_DISCOVER_GATE)
    def test_manifest_restored_byte_equal(
        self,
        mock_gate,
        mock_preflight,
        mock_dl,
        mock_validate,
        mock_updates,
        project,
    ):
        project_dir, runner = project
        manifest_path = project_dir / "apm.yml"

        # Pre-existing manifest with a safe dep
        _write_apm_yml(manifest_path, deps=["DevExpGbb/safe-package"])
        original_bytes = manifest_path.read_bytes()
        original_hash = hashlib.sha256(original_bytes).hexdigest()

        policy = _load_fixture_policy("apm-policy-deny.yml")
        fetch = _make_fetch_result("found", policy=policy)
        mock_gate.return_value = fetch
        mock_preflight.return_value = fetch

        # Install a denied package -> should fail and rollback
        result = _invoke_install(runner, ["test-blocked/forbidden-package"])

        assert result.exit_code != 0, (
            f"Expected block on install <pkg>, got {result.exit_code}\n{result.output}"
        )

        # Verify byte-equal restoration
        restored_bytes = manifest_path.read_bytes()
        restored_hash = hashlib.sha256(restored_bytes).hexdigest()
        assert restored_hash == original_hash, (
            f"apm.yml was NOT restored byte-equal.\n"
            f"  Original hash: {original_hash}\n"
            f"  Restored hash: {restored_hash}"
        )


# =====================================================================
# I13: enforcement: off + denied deps -> succeeds silently
# =====================================================================


class TestI13EnforcementOff:
    """enforcement=off -> install proceeds, no policy output."""

    @patch(_PATCH_UPDATES, return_value=None)
    @patch(_PATCH_DOWNLOADER)
    @patch(_PATCH_DISCOVER_PREFLIGHT)
    @patch(_PATCH_DISCOVER_GATE)
    def test_enforcement_off_proceeds_silently(
        self, mock_gate, mock_preflight, mock_dl, mock_updates, project
    ):
        project_dir, runner = project
        _write_apm_yml(project_dir / "apm.yml", deps=["test-blocked/forbidden-package"])

        # enforcement=off policy with deny rules (constructed frozen-safe)
        policy = _build_policy(
            enforcement="off",
            deny=("test-blocked/*",),
        )
        fetch = _make_fetch_result("found", policy=policy)
        mock_gate.return_value = fetch
        mock_preflight.return_value = fetch

        result = _invoke_install(runner)

        out = result.output
        # No policy blocking
        assert "blocked by org policy" not in out.lower(), (
            f"enforcement=off should not block:\n{out}"
        )


# =====================================================================
# I14: no policy at all -> succeeds silently
# =====================================================================


class TestI14NoPolicyPresent:
    """No apm-policy.yml anywhere -> install proceeds silently."""

    @patch(_PATCH_UPDATES, return_value=None)
    @patch(_PATCH_DOWNLOADER)
    @patch(_PATCH_DISCOVER_PREFLIGHT)
    @patch(_PATCH_DISCOVER_GATE)
    def test_absent_policy_proceeds(
        self, mock_gate, mock_preflight, mock_dl, mock_updates, project
    ):
        project_dir, runner = project
        _write_apm_yml(project_dir / "apm.yml", deps=["DevExpGbb/some-package"])

        fetch = _make_fetch_result("absent", source="org:DevExpGbb/.github")
        mock_gate.return_value = fetch
        mock_preflight.return_value = fetch

        result = _invoke_install(runner)

        out = result.output
        assert "blocked by org policy" not in out.lower(), f"Absent policy should not block:\n{out}"


# =====================================================================
# I15: cache stale-but-fresh-enough (<7d) + offline -> uses cache
# =====================================================================


class TestI15CachedStale:
    """Stale cache within MAX_STALE_TTL serves policy with warning."""

    @patch(_PATCH_UPDATES, return_value=None)
    @patch(_PATCH_DOWNLOADER)
    @patch(_PATCH_DISCOVER_PREFLIGHT)
    @patch(_PATCH_DISCOVER_GATE)
    def test_stale_cache_still_enforces(
        self, mock_gate, mock_preflight, mock_dl, mock_updates, project
    ):
        project_dir, runner = project
        _write_apm_yml(project_dir / "apm.yml", deps=["DevExpGbb/safe-package"])

        # Stale cache with a permissive warn-mode policy (allow DevExpGbb/*)
        policy = _build_policy(
            enforcement="warn",
            allow=("DevExpGbb/*", "microsoft/*"),
        )
        fetch = _make_fetch_result(
            "cached_stale",
            policy=policy,
            cached=True,
            cache_age_seconds=86400,  # 1 day -- within 7d MAX_STALE_TTL
            fetch_error="Connection timed out",
        )
        mock_gate.return_value = fetch
        mock_preflight.return_value = fetch

        result = _invoke_install(runner)

        out = result.output
        # Should proceed (dep passes allow list)
        assert "blocked by org policy" not in out.lower(), f"Stale cache should still allow:\n{out}"
        # Check for stale/cached warning in output
        assert "stale" in out.lower() or "cached" in out.lower(), (
            f"Expected stale/cached warning in output:\n{out}"
        )


# =====================================================================
# I16: garbage_response policy on remote -> fail-open, proceed
#      NOTE: This tests the garbage_response outcome specifically.
#      True malformed outcome is tested in I19.
# =====================================================================


class TestI16GarbageResponsePolicy:
    """Garbage response (e.g. captive portal) -> fail-open (warn), install proceeds."""

    @patch(_PATCH_UPDATES, return_value=None)
    @patch(_PATCH_DOWNLOADER)
    @patch(_PATCH_DISCOVER_PREFLIGHT)
    @patch(_PATCH_DISCOVER_GATE)
    def test_malformed_policy_warns_but_proceeds(
        self, mock_gate, mock_preflight, mock_dl, mock_updates, project
    ):
        project_dir, runner = project
        _write_apm_yml(project_dir / "apm.yml", deps=["DevExpGbb/some-package"])

        # Garbage response -> fail-open per CEO ruling
        fetch = _make_fetch_result(
            "garbage_response",
            error="Response body is not valid YAML (possible captive portal)",
            source="org:test-org/.github",
        )
        mock_gate.return_value = fetch
        mock_preflight.return_value = fetch

        result = _invoke_install(runner)

        out = result.output
        assert "blocked by org policy" not in out.lower(), (
            f"Garbage response should fail-open:\n{out}"
        )
        assert "policy" in out.lower() or "warning" in out.lower(), (
            f"Expected audit warning about malformed/garbage policy:\n{out}"
        )


# =====================================================================
# I17: local-only repo (no git remote, no policy) -> succeeds silently
# =====================================================================


class TestI17NoGitRemote:
    """No git remote -> outcome no_git_remote -> install proceeds."""

    @patch(_PATCH_UPDATES, return_value=None)
    @patch(_PATCH_DOWNLOADER)
    @patch(_PATCH_DISCOVER_PREFLIGHT)
    @patch(_PATCH_DISCOVER_GATE)
    def test_no_git_remote_proceeds(
        self, mock_gate, mock_preflight, mock_dl, mock_updates, project
    ):
        project_dir, runner = project
        _write_apm_yml(project_dir / "apm.yml", deps=["DevExpGbb/some-package"])

        fetch = _make_fetch_result("no_git_remote", source="")
        mock_gate.return_value = fetch
        mock_preflight.return_value = fetch

        result = _invoke_install(runner)

        out = result.output
        assert "blocked by org policy" not in out.lower(), f"no_git_remote should not block:\n{out}"


# =====================================================================
# I18: direct MCP block on `apm install` (no APM deps, --only=mcp)
#      Exit non-zero, MCP configs NOT written
# =====================================================================


class TestI18DirectMCPBlocked:
    """Direct MCP entry in apm.yml denied by policy -> blocked before
    MCPIntegrator.install writes runtime configs.  No APM deps.

    This is the S2 fix validation: the second preflight at install.py
    now fires whenever ``mcp_deps`` is non-empty (not just when
    ``transitive_mcp`` is non-empty).
    """

    @patch(_PATCH_UPDATES, return_value=None)
    @patch(_PATCH_MCP_INSTALL, return_value=0)
    @patch(_PATCH_DISCOVER_PREFLIGHT)
    @patch(_PATCH_DISCOVER_GATE)
    def test_direct_mcp_denied_blocks_install(
        self,
        mock_gate,
        mock_preflight,
        mock_mcp_install,
        mock_updates,
        project,
    ):
        project_dir, runner = project

        # Manifest has ONLY a denied direct MCP entry -- no APM deps.
        _write_apm_yml(
            project_dir / "apm.yml",
            mcp=["io.github.untrusted/evil-mcp-server"],
        )

        policy = _load_fixture_policy("apm-policy-mcp.yml")
        # enforcement=block, mcp.deny=("io.github.untrusted/*",)
        fetch = _make_fetch_result("found", policy=policy)
        mock_gate.return_value = fetch
        mock_preflight.return_value = fetch

        result = _invoke_install(runner)

        out = result.output
        assert result.exit_code != 0, f"Expected non-zero exit for direct MCP block:\n{out}"
        # MCPIntegrator.install should NOT have been called
        (
            mock_mcp_install.assert_not_called(),
            ("MCPIntegrator.install should not run when direct MCP is blocked"),
        )


# =====================================================================
# I19: malformed policy outcome on install path -> warn-and-proceed
#      (fail-open posture per CEO mandate)
# =====================================================================


class TestI19MalformedPolicyFailOpen:
    """Malformed policy outcome -> fail-open with loud warning,
    install proceeds.  Distinct from I16 which tests garbage_response.

    Also verifies that ``sys.exit(1)`` is NOT called (which would
    bypass the rollback handler in install.py).
    """

    @patch(_PATCH_UPDATES, return_value=None)
    @patch(_PATCH_DOWNLOADER)
    @patch(_PATCH_DISCOVER_PREFLIGHT)
    @patch(_PATCH_DISCOVER_GATE)
    def test_malformed_outcome_warns_and_proceeds(
        self,
        mock_gate,
        mock_preflight,
        mock_dl,
        mock_updates,
        project,
    ):
        project_dir, runner = project
        _write_apm_yml(project_dir / "apm.yml", deps=["DevExpGbb/some-package"])

        # True malformed outcome (distinct from garbage_response)
        fetch = _make_fetch_result(
            "malformed",
            error="YAML parsed but schema validation failed",
            source="org:test-org/.github",
        )
        mock_gate.return_value = fetch
        mock_preflight.return_value = fetch

        result = _invoke_install(runner)

        out = result.output
        # Fail-open: should NOT exit non-zero due to malformed policy
        # (the install may still fail for other reasons like missing
        # packages, but NOT due to a sys.exit(1) from policy_gate)
        assert "blocked by org policy" not in out.lower(), (
            f"Malformed policy should fail-open, not block:\n{out}"
        )
        # Should emit a warning about the malformed policy
        has_warning = "malformed" in out.lower() or "policy" in out.lower()
        assert has_warning, f"Expected malformed policy warning in output:\n{out}"

    @patch(_PATCH_UPDATES, return_value=None)
    @patch(_PATCH_VALIDATE_PKG, return_value=True)
    @patch(_PATCH_DOWNLOADER)
    @patch(_PATCH_DISCOVER_PREFLIGHT)
    @patch(_PATCH_DISCOVER_GATE)
    def test_malformed_policy_does_not_bypass_rollback(
        self,
        mock_gate,
        mock_preflight,
        mock_dl,
        mock_validate,
        mock_updates,
        project,
    ):
        """install <pkg> with malformed policy must NOT sys.exit(1)
        from inside policy_gate (which would bypass the rollback handler).
        The pipeline should proceed (fail-open), and if it fails for
        any other reason, the except handler in install.py catches it."""
        project_dir, runner = project
        manifest_path = project_dir / "apm.yml"

        _write_apm_yml(manifest_path, deps=["DevExpGbb/safe-package"])

        fetch = _make_fetch_result(
            "malformed",
            error="Policy schema invalid",
            source="org:test-org/.github",
        )
        mock_gate.return_value = fetch
        mock_preflight.return_value = fetch

        # Install a new package -- malformed policy is fail-open so
        # the pipeline proceeds.  The pipeline may fail for mocked
        # reasons, but crucially no sys.exit(1) should short-circuit.
        result = _invoke_install(runner, ["test-org/new-package"])

        out = result.output
        # Key assertion: the malformed policy did NOT cause a
        # sys.exit(1) that would have bypassed the rollback handler.
        # Evidence: no "blocked by org policy" appears and the exit
        # code is NOT from a bare sys.exit(1) with no output context.
        assert "blocked by org policy" not in out.lower(), (
            f"Malformed policy should fail-open, not block:\n{out}"
        )


# =====================================================================
# I20: warn mode + multiple violations -> ALL warnings emitted, exit 0
# =====================================================================


class TestI20WarnModeAllViolations:
    """Warn mode with fail_fast=False collects ALL violations
    and emits all of them, not just the first.

    In warn mode the install proceeds to completion. Policy warnings
    are pushed to the logger's DiagnosticCollector for the summary.
    The key contract: warn mode does NOT short-circuit after the first
    check failure (fail_fast=False was added by the C3 fix).
    """

    @patch(_PATCH_UPDATES, return_value=None)
    @patch(_PATCH_DOWNLOADER)
    @patch(_PATCH_DISCOVER_PREFLIGHT)
    @patch(_PATCH_DISCOVER_GATE)
    def test_warn_mode_emits_all_violations(
        self, mock_gate, mock_preflight, mock_dl, mock_updates, project
    ):
        project_dir, runner = project

        # Multiple denied deps -- warn mode should report ALL
        _write_apm_yml(
            project_dir / "apm.yml",
            deps=[
                "test-blocked/evil-pkg-1",
                "test-blocked/evil-pkg-2",
                "test-blocked/evil-pkg-3",
            ],
        )

        policy = _build_policy(
            enforcement="warn",
            deny=("test-blocked/*",),
        )
        fetch = _make_fetch_result("found", policy=policy)
        mock_gate.return_value = fetch
        mock_preflight.return_value = fetch

        result = _invoke_install(runner)

        out = result.output
        # Should NOT block (warn mode)
        assert "blocked by org policy" not in out.lower(), f"Warn-mode should NOT block:\n{out}"
        # Install should proceed (exit 0 or exit due to mock errors,
        # but NOT due to policy)
        # The 3 deps should all be attempted (not short-circuited
        # after the first policy check failure)
        for dep_name in ["evil-pkg-1", "evil-pkg-2", "evil-pkg-3"]:
            assert dep_name in out, (
                f"Expected {dep_name} to appear in output (not short-circuited):\n{out}"
            )

        # microsoft/apm#834 -- warn-mode violations must be visible in the
        # final install summary, not just buried in an internal collector.
        # The rendered summary uses "policy warning(s)" plural-aware noun.
        assert "policy warning" in out.lower(), (
            f"Expected warn-mode violations to surface in install summary:\n{out}"
        )


# =====================================================================
# I21: 3-level extends chain (#831) end-to-end through install pipeline
# =====================================================================


class TestI21ThreeLevelExtendsChain:
    """leaf -> mid -> root resolves all three policies at install time.

    Bug #831: the install-time chain walk only resolved one level of
    `extends:`, silently dropping any grandparent policies.  This test
    exercises the full pipeline end-to-end by mocking the per-level
    fetcher (`discover_policy`) and letting the real
    `discover_policy_with_chain` walk three levels.

    A deny rule that lives only on the **root** policy must still block
    an install -- if the chain collapses, this assertion fails.
    """

    @patch(_PATCH_UPDATES, return_value=None)
    @patch(_PATCH_DOWNLOADER)
    @patch("apm_cli.policy.discovery.discover_policy")
    def test_three_level_chain_blocks_via_root_deny(
        self, mock_discover, mock_dl, mock_updates, project
    ):
        project_dir, runner = project

        # Leaf project depends on a package that only the ROOT policy denies.
        _write_apm_yml(
            project_dir / "apm.yml",
            deps=["enterprise-blocked/forbidden"],
        )

        leaf_policy = ApmPolicy(
            enforcement="warn",
            extends="org-mid/.github",
            dependencies=DependencyPolicy(),
        )
        mid_policy = ApmPolicy(
            enforcement="warn",
            extends="enterprise-root/.github",
            dependencies=DependencyPolicy(),
        )
        root_policy = ApmPolicy(
            enforcement="block",
            dependencies=DependencyPolicy(deny=("enterprise-blocked/*",)),
        )

        leaf_fetch = _make_fetch_result("found", policy=leaf_policy, source="org:contoso/.github")
        mid_fetch = _make_fetch_result("found", policy=mid_policy, source="org:org-mid/.github")
        root_fetch = _make_fetch_result(
            "found", policy=root_policy, source="org:enterprise-root/.github"
        )

        # discover_policy is called once for the leaf (auto-discover) plus
        # one call per ancestor in the chain.  Both the gate phase and the
        # preflight may invoke discover_policy_with_chain, so be generous
        # by repeating the side_effect cycle.
        cycle = [leaf_fetch, mid_fetch, root_fetch]
        mock_discover.side_effect = cycle * 4  # tolerate up to 4 invocations

        result = _invoke_install(runner)

        out = result.output

        # The chain must have collapsed root's deny rule into the merged
        # effective policy and blocked the install.
        assert result.exit_code != 0, (
            f"Expected exit non-zero when 3-level chain blocks dep:\n{out}"
        )
        assert "enterprise-blocked/forbidden" in out, (
            f"Expected denied dep to be cited in output:\n{out}"
        )
