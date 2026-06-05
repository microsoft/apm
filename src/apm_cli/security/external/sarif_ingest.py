"""Pure SARIF 2.1.0 -> :class:`ScanFinding` reader (no I/O).

This is the inverse of :func:`apm_cli.security.audit_report.findings_to_sarif`.
It takes a parsed SARIF document (a ``dict``) and produces APM findings
grouped by file, so external scanner output renders through the exact same
text/json/sarif/markdown pipeline as APM's native content scan.

The module is intentionally dependency-free and fully unit-testable from
fixture documents.  It fails *closed*: malformed or partial SARIF yields the
findings it can safely extract and raises :class:`ExternalScanError` only
when the top-level shape is not a SARIF document at all.
"""

from __future__ import annotations

import re

from ..audit_report import _SEVERITY_MAP, relative_path_for_report
from ..content_scanner import ScanFinding
from .base import ExternalScanError

# Matches ANSI SGR escape sequences (e.g. \x1b[0m, \x1b[31;1m).
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")

# Inverse of audit_report._SEVERITY_MAP (APM severity -> SARIF level).
# SARIF level -> APM severity.  ``none`` and any unknown level map to "info"
# so an external tool can never silently escalate to a gating severity.
_LEVEL_TO_SEVERITY: dict[str, str] = {v: k for k, v in _SEVERITY_MAP.items()}
_LEVEL_TO_SEVERITY.setdefault("none", "info")


def _sarif_level_to_severity(level: str | None) -> str:
    """Map a SARIF result/rule ``level`` to an APM severity (fail to info)."""
    if not isinstance(level, str):
        return "info"
    return _LEVEL_TO_SEVERITY.get(level.lower(), "info")


def _rule_levels(run: dict) -> dict[str, str]:
    """Build a ``{ruleId: default level}`` map from a run's tool driver rules.

    SARIF results may omit ``level`` and inherit it from the matching rule's
    ``defaultConfiguration.level``.  This precomputes that lookup.
    """
    levels: dict[str, str] = {}
    driver = (((run or {}).get("tool") or {}).get("driver")) or {}
    for rule in driver.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        rid = rule.get("id")
        level = (rule.get("defaultConfiguration") or {}).get("level")
        if isinstance(rid, str) and isinstance(level, str):
            levels[rid] = level
    return levels


def _result_location(result: dict) -> tuple[str, int, int]:
    """Extract ``(file, line, column)`` from the first physical location.

    Missing pieces degrade gracefully: unknown file -> ``"<unknown>"``,
    missing line/column -> ``1``.
    """
    locations = result.get("locations") or []
    if locations and isinstance(locations[0], dict):
        phys = locations[0].get("physicalLocation") or {}
        uri = ((phys.get("artifactLocation") or {}).get("uri")) or "<unknown>"
        region = phys.get("region") or {}
        line = region.get("startLine") or 1
        column = region.get("startColumn") or 1
        try:
            return str(uri), int(line), int(column)
        except (TypeError, ValueError):
            return str(uri), 1, 1
    return "<unknown>", 1, 1


def _result_message(result: dict) -> str:
    """Extract a human-readable message from a SARIF result.

    ANSI escape codes are stripped so that external scanners emitting
    Rich-formatted text (e.g. SkillSpector) do not leak escape
    sequences into APM's table output.
    """
    message = result.get("message") or {}
    text = message.get("text") if isinstance(message, dict) else None
    if not isinstance(text, str) or not text:
        return "(no message)"
    cleaned = _ANSI_ESCAPE_RE.sub("", text)
    return cleaned if cleaned else "(no message)"


def sarif_to_findings(
    document: dict,
    *,
    tool_name: str = "external",
) -> dict[str, list[ScanFinding]]:
    """Convert a parsed SARIF 2.1.0 document into findings grouped by file.

    Args:
        document: A parsed SARIF document (``json.load`` output).
        tool_name: Short identifier of the producing tool, used as a
            category prefix (e.g. ``"skillspector"``) so findings are
            attributable in merged reports.

    Returns:
        ``{file: [ScanFinding, ...]}`` ready to merge into the audit report.

    Raises:
        ExternalScanError: If *document* is not a SARIF-shaped object.
    """
    if not isinstance(document, dict) or "runs" not in document:
        raise ExternalScanError("External scanner output is not a SARIF document (missing 'runs').")

    runs = document.get("runs") or []
    if not isinstance(runs, list):
        raise ExternalScanError("External scanner SARIF 'runs' is not a list.")

    findings_by_file: dict[str, list[ScanFinding]] = {}

    for run in runs:
        if not isinstance(run, dict):
            continue
        rule_levels = _rule_levels(run)
        for result in run.get("results") or []:
            if not isinstance(result, dict):
                continue
            rule_id = result.get("ruleId")
            level = result.get("level")
            if level is None and isinstance(rule_id, str):
                level = rule_levels.get(rule_id)
            severity = _sarif_level_to_severity(level)

            file_path, line, column = _result_location(result)
            rel = relative_path_for_report(file_path)
            category = f"{tool_name}/{rule_id}" if isinstance(rule_id, str) else tool_name

            finding = ScanFinding(
                file=rel,
                line=line,
                column=column,
                char="",
                codepoint="",
                severity=severity,
                category=category,
                description=_result_message(result),
            )
            findings_by_file.setdefault(rel, []).append(finding)

    return findings_by_file
