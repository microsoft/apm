"""``apm marketplace doctor`` command."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import click

from ...core.command_logger import CommandLogger
from ...marketplace.errors import MarketplaceYmlError
from ...marketplace.git_stderr import translate_git_stderr
from ...marketplace.yml_schema import load_marketplace_yml
from . import marketplace, _DoctorCheck, _render_doctor_table


@marketplace.command(help="Run environment diagnostics for marketplace builds")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def doctor(verbose: bool) -> None:
    """Check git, network, auth, and marketplace.yml readiness."""
    from . import subprocess

    logger = CommandLogger("marketplace-doctor", verbose=verbose)
    checks: list[_DoctorCheck] = []

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
    except Exception as exc:
        git_detail = str(exc)[:60]

    checks.append(_DoctorCheck(name="git", passed=git_ok, detail=git_detail))

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
    except Exception as exc:
        net_detail = str(exc)[:60]

    checks.append(_DoctorCheck(name="network", passed=net_ok, detail=net_detail))

    has_token = bool(os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"))
    auth_detail = (
        "Token detected" if has_token else "No token; unauthenticated rate limits apply"
    )
    checks.append(
        _DoctorCheck(
            name="auth",
            passed=True,
            detail=auth_detail,
            informational=True,
        )
    )

    yml_path = Path.cwd() / "marketplace.yml"
    yml_found = yml_path.exists()
    yml_detail = ""
    yml_parsed = False
    if yml_found:
        try:
            load_marketplace_yml(yml_path)
            yml_parsed = True
            yml_detail = "marketplace.yml found and valid"
        except MarketplaceYmlError as exc:
            yml_detail = f"marketplace.yml has errors: {str(exc)[:60]}"
    else:
        yml_detail = "No marketplace.yml in current directory"

    checks.append(
        _DoctorCheck(
            name="marketplace.yml",
            passed=yml_parsed if yml_found else True,
            detail=yml_detail,
            informational=True,
        )
    )

    _render_doctor_table(logger, checks)

    critical_checks = [check for check in checks if not check.informational]
    if any(not check.passed for check in critical_checks):
        sys.exit(1)
