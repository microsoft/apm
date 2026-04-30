"""``apm marketplace doctor`` command."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import click

from ...core.command_logger import CommandLogger
from ...marketplace.errors import MarketplaceYmlError
from ...marketplace.git_stderr import translate_git_stderr
from ...marketplace.migration import ConfigSource, detect_config_source
from ...marketplace.yml_schema import (
    load_marketplace_from_apm_yml,
    load_marketplace_yml,
)
from . import (
    _DoctorCheck,
    _find_duplicate_names,
    _render_doctor_table,
    marketplace,
)


@marketplace.command(help="Run environment diagnostics for marketplace publishing")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def doctor(verbose):
    """Check git, network, auth, and marketplace config readiness."""
    logger = CommandLogger("marketplace-doctor", verbose=verbose)
    checks = []

    # Check 1: git on PATH
    git_ok = False
    git_detail = ""
    try:
        result = subprocess.run(
            ["git", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            git_ok = True
            git_detail = result.stdout.strip()
        else:
            git_detail = "git returned non-zero exit code"
    except FileNotFoundError:
        git_detail = "git not found on PATH"
    except subprocess.TimeoutExpired:
        git_detail = "git --version timed out"
    except (subprocess.SubprocessError, OSError) as exc:
        git_detail = str(exc)[:60]

    checks.append(
        _DoctorCheck(
            name="git",
            passed=git_ok,
            detail=git_detail,
        )
    )

    # Check 2: network reachability
    net_ok = False
    net_detail = ""
    try:
        result = subprocess.run(
            ["git", "ls-remote", "https://github.com/git/git.git", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            net_ok = True
            net_detail = "github.com reachable"
        else:
            translated = translate_git_stderr(
                result.stderr,
                exit_code=result.returncode,
                operation="ls-remote",
                remote="github.com",
            )
            net_detail = translated.hint[:80]
    except subprocess.TimeoutExpired:
        net_detail = "Network check timed out (5s)"
    except FileNotFoundError:
        net_detail = "git not found; cannot test network"
    except (subprocess.SubprocessError, OSError) as exc:
        net_detail = str(exc)[:60]

    checks.append(
        _DoctorCheck(
            name="network",
            passed=net_ok,
            detail=net_detail,
        )
    )

    # Check 3: auth tokens (delegate to AuthResolver for full coverage)
    try:
        from ...core.auth import AuthResolver

        resolver = AuthResolver()
        # Try to get a token for github.com as a representative check
        token = resolver.resolve("github.com").token
        has_token = bool(token)
    except Exception:
        has_token = False
    auth_detail = "Token detected" if has_token else "No token; unauthenticated rate limits apply"
    checks.append(
        _DoctorCheck(
            name="auth",
            passed=True,  # informational; never fails
            detail=auth_detail,
            informational=True,
        )
    )

    # Check 4: gh CLI availability (informational; only needed for publish)
    gh_ok = False
    gh_detail = ""
    try:
        result = subprocess.run(
            ["gh", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            gh_ok = True
            gh_detail = result.stdout.strip().split("\n")[0]
        else:
            gh_detail = "gh CLI returned non-zero exit code"
    except FileNotFoundError:
        gh_detail = "gh CLI not found (install: https://cli.github.com/)"
    except subprocess.TimeoutExpired:
        gh_detail = "gh --version timed out"
    except (subprocess.SubprocessError, OSError) as exc:
        gh_detail = str(exc)[:60]

    checks.append(
        _DoctorCheck(
            name="gh CLI",
            passed=gh_ok,
            detail=gh_detail,
            informational=True,
        )
    )

    # Check 5: marketplace config presence + parsability
    project_root = Path.cwd()
    apm_path = project_root / "apm.yml"
    legacy_path = project_root / "marketplace.yml"
    yml_obj = None
    config_passed = True
    config_detail = ""

    try:
        source = detect_config_source(project_root)
        if source == ConfigSource.APM_YML:
            try:
                yml_obj = load_marketplace_from_apm_yml(apm_path)
                config_detail = "apm.yml 'marketplace:' block found and valid"
            except MarketplaceYmlError as exc:
                config_passed = False
                config_detail = f"apm.yml marketplace block has errors: {str(exc)[:60]}"
        elif source == ConfigSource.LEGACY_YML:
            try:
                yml_obj = load_marketplace_yml(legacy_path)
                config_detail = (
                    "marketplace.yml found (legacy). Run 'apm marketplace "
                    "migrate' to fold it into apm.yml."
                )
            except MarketplaceYmlError as exc:
                config_passed = False
                config_detail = f"marketplace.yml has errors: {str(exc)[:60]}"
        else:
            config_detail = "No marketplace authoring config in current directory"
    except MarketplaceYmlError as exc:
        config_passed = False
        config_detail = str(exc)[:120]

    checks.append(
        _DoctorCheck(
            name="marketplace config",
            passed=config_passed,
            detail=config_detail,
            informational=True,
        )
    )

    # Check 6: duplicate package names (defence-in-depth)
    if yml_obj is not None:
        dup_detail = _find_duplicate_names(yml_obj)
        if dup_detail:
            checks.append(
                _DoctorCheck(
                    name="duplicate names",
                    passed=False,
                    detail=dup_detail,
                    informational=True,
                )
            )
        else:
            checks.append(
                _DoctorCheck(
                    name="duplicate names",
                    passed=True,
                    detail="No duplicate package names",
                    informational=True,
                )
            )

    _render_doctor_table(logger, checks)

    # Exit: 0 if checks 1-2 pass; config checks are informational
    critical_checks = [c for c in checks if not c.informational]
    if any(not c.passed for c in critical_checks):
        sys.exit(1)
