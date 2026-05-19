"""Shared helpers for CI and policy checks."""

from __future__ import annotations

from pathlib import Path


def _parse_apm_yml_safe(apm_yml_path: Path, result) -> object | None:
    """Try to parse *apm_yml_path*, appending a check-result on failure.

    Assumes the caller has already verified that *apm_yml_path* exists.

    Args:
        apm_yml_path: Path to the ``apm.yml`` file.
        result: :class:`~apm_cli.policy.models.CIAuditResult` to append a
            ``"manifest-parse"`` check to on failure.

    Returns:
        :class:`~apm_cli.models.apm_package.APMPackage` on success; ``None``
        on parse failure — in which case a ``"manifest-parse"`` check has
        been appended to *result* and the caller should return immediately.
    """
    import yaml

    from ..models.apm_package import APMPackage, clear_apm_yml_cache
    from .models import CheckResult

    try:
        clear_apm_yml_cache()
        return APMPackage.from_apm_yml(apm_yml_path)
    except (ValueError, yaml.YAMLError, OSError) as exc:
        result.checks.append(
            CheckResult(
                name="manifest-parse",
                passed=False,
                message="Cannot parse apm.yml: %s -- fix the YAML syntax error in apm.yml and re-run."  # noqa: UP031
                % exc,
            )
        )
        return None
