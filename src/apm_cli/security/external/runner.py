"""Vendor-neutral orchestration for external SARIF-native scanners.

Both ``apm audit --external ...`` (``commands/audit.py``) and the optional
install-time audit phase (``install/phases/audit.py``) need the same
sequence: resolve each requested scanner name to an adapter, verify it can
run in the current environment, invoke it, and merge its findings.

Extracting that loop here keeps a single code path (DRY) and a single,
consistent failure contract.  Callers layer their own concerns on top:

* the experimental ``external_scanners`` gate (callers enforce it, not this
  module -- see :mod:`apm_cli.security.external.gate`), and
* how to surface a failure (the command exits non-zero; the install phase
  raises so the pipeline blocks).

This module never calls ``sys.exit`` and never enforces the experimental
flag: it raises :class:`ExternalScanError` on any failure so each caller can
map it to its own UX.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ..content_scanner import ScanFinding
from .base import ExternalScanError
from .options import ScannerOptions

if TYPE_CHECKING:  # pragma: no cover - import for type hints only
    from collections.abc import Iterable


def merge_findings(
    base: dict[str, list[ScanFinding]],
    extra: dict[str, list[ScanFinding]],
) -> None:
    """Merge *extra* findings into *base* in place, grouping by file."""
    for file_key, findings in extra.items():
        base.setdefault(file_key, []).extend(findings)


def run_external_scanners(
    external: Iterable[str],
    external_sarif: str | None,
    scan_paths: list[Path],
    *,
    options_by_name: dict[str, ScannerOptions] | None = None,
    logger=None,
) -> dict[str, list[ScanFinding]]:
    """Resolve, validate, run, and merge external scanners.

    Args:
        external: Scanner names to run (e.g. ``("skillspector", "sarif")``).
        external_sarif: SARIF file path for the generic ``sarif`` adapter.
        scan_paths: Files/directories handed to each adapter's ``scan``.
        options_by_name: Per-scanner resolved :class:`ScannerOptions`. A name
            with no entry (or ``None``) gets ``ScannerOptions()`` (adapter
            defaults). Callers own resolution/precedence; this module just
            threads the result through.
        logger: Optional object with a ``progress(msg)`` method for status.

    Returns:
        Findings grouped by file, merged across every requested scanner.

    Raises:
        ExternalScanError: If a name is unknown, an adapter is unavailable
            (e.g. its CLI is not on ``PATH``), or a scan fails. The message
            is user-facing and actionable.
    """
    from .registry import resolve_scanner

    options_by_name = options_by_name or {}
    merged: dict[str, list[ScanFinding]] = {}
    for name in external:
        try:
            scanner = resolve_scanner(name, sarif_file=external_sarif)
        except ValueError as exc:
            raise ExternalScanError(str(exc)) from exc

        options = options_by_name.get(name, ScannerOptions())

        available, reason = scanner.is_available(options=options)
        if not available:
            raise ExternalScanError(f"External scanner '{name}' is unavailable: {reason}")

        if logger is not None:
            if options.llm:
                logger.warning(
                    f"LLM analysis enabled for '{name}' -- outbound API calls "
                    f"will be made (network egress; API billing may apply)"
                )
            logger.progress(f"Running external scanner: {name}")
        try:
            results = scanner.scan(scan_paths, options=options)
        except ExternalScanError as exc:
            raise ExternalScanError(f"External scanner '{name}' failed: {exc}") from exc
        merge_findings(merged, results)

    return merged
