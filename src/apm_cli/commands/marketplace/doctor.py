"""``apm marketplace doctor`` command."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import click

from ...core.command_logger import CommandLogger
from ...marketplace.yml_schema import load_marketplace_yml
from . import doctor_checks as _doctor_checks
from . import marketplace
from ._doctor import _DoctorCheck, _render_doctor_table
from .doctor_checks import (
    _check_auth,
    _check_duplicate_names,
    _check_gh_cli,
    _check_git,
    _check_marketplace_config,
    _check_network,
    _critical_checks_passed,
)


@marketplace.command(help="Run environment diagnostics for marketplace publishing")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
def doctor(verbose):
    """Check git, network, auth, and marketplace config readiness."""
    _doctor_checks.subprocess = subprocess
    _doctor_checks.load_marketplace_yml = load_marketplace_yml
    logger = CommandLogger("marketplace-doctor", verbose=verbose)
    project_root = Path.cwd()
    checks = [
        _check_git(),
        _check_network(),
        _check_auth(),
        _check_gh_cli(),
    ]
    config_check, yml_obj = _check_marketplace_config(project_root)
    checks.append(config_check)
    duplicate_check = _check_duplicate_names(yml_obj)
    if duplicate_check is not None:
        checks.append(duplicate_check)

    # Check 6: format coverage (informational; only when config is present)
    if yml_obj is not None:
        from ...marketplace.output_profiles import known_output_names

        configured = frozenset(getattr(yml_obj, "outputs", ()) or ())
        supported = known_output_names()
        missing = sorted(supported - configured)
        configured_sorted = sorted(configured)
        if not missing:
            fc_detail = f"Publishing for all known formats: {', '.join(configured_sorted)}."
        else:
            fc_detail = (
                f"Configured: {', '.join(configured_sorted) or '(none)'}. "
                f"Also supported: {', '.join(missing)}. "
                f"Add e.g. '{missing[0]}: {{}}' under 'marketplace.outputs' "
                "in apm.yml to publish for more consumers."
            )
        checks.append(
            _DoctorCheck(
                name="format coverage",
                passed=True,
                detail=fc_detail,
                informational=True,
            )
        )

    # Check 7: version alignment (informational; only when config is present)
    if yml_obj is not None and hasattr(yml_obj, "versioning"):
        from ...marketplace.version_check import check_version_alignment

        va_report = check_version_alignment(yml_obj, Path.cwd())
        total = len(va_report.packages)
        aligned = sum(1 for p in va_report.packages if p.ok)
        if total == 0:
            va_detail = f"strategy={va_report.strategy}, no local packages to align"
            va_passed = True
        elif va_report.ok:
            va_detail = f"strategy={va_report.strategy}, {aligned}/{total} packages aligned"
            va_passed = True
        else:
            misaligned = [p.path for p in va_report.packages if not p.ok]
            misaligned_count = len(misaligned)
            va_detail = (
                f"strategy={va_report.strategy}, "
                f"{misaligned_count}/{total} packages misaligned: "
                f"{misaligned[0]}"
            )
            va_passed = False
        checks.append(
            _DoctorCheck(
                name="version alignment",
                passed=va_passed,
                detail=va_detail,
                informational=True,
            )
        )

    _render_doctor_table(logger, checks)
    if not _critical_checks_passed(checks):
        sys.exit(1)
