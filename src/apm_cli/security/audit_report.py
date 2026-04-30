"""Audit report serialization — JSON and SARIF output for apm audit."""

import json
from pathlib import Path
from typing import Any, Dict, List  # noqa: F401, UP035

from .content_scanner import ScanFinding


def relative_path_for_report(file_path: str) -> str:
    """Ensure paths in reports are relative with forward slashes."""
    p = Path(file_path)
    if p.is_absolute():
        try:
            return p.relative_to(Path.cwd()).as_posix()
        except ValueError:
            return p.name
    return file_path.replace("\\", "/")


# SARIF schema version
_SARIF_VERSION = "2.1.0"
_SARIF_SCHEMA = (
    "https://docs.oasis-open.org/sarif/sarif/v2.1.0/cos02/schemas/sarif-schema-2.1.0.json"
)
_TOOL_NAME = "apm-audit"
_TOOL_INFO_URI = "https://apm.github.io/apm/enterprise/security/"

# Severity mapping: APM → SARIF
_SEVERITY_MAP = {
    "critical": "error",
    "warning": "warning",
    "info": "note",
}


def _rule_id(category: str) -> str:
    """Build a SARIF rule ID from a finding category."""
    return f"apm/hidden-unicode/{category}"


def findings_to_json(
    findings_by_file: dict[str, list[ScanFinding]],
    files_scanned: int,
    exit_code: int,
) -> dict:
    """Convert scan findings to APM's JSON report format."""
    all_findings = [f for ff in findings_by_file.values() for f in ff]

    summary = {
        "files_scanned": files_scanned,
        "files_affected": len(findings_by_file),
        "critical": sum(1 for f in all_findings if f.severity == "critical"),
        "warning": sum(1 for f in all_findings if f.severity == "warning"),
        "info": sum(1 for f in all_findings if f.severity == "info"),
    }

    items = []
    for finding in all_findings:
        items.append(
            {
                "severity": finding.severity,
                "file": relative_path_for_report(finding.file),
                "line": finding.line,
                "column": finding.column,
                "codepoint": finding.codepoint,
                "category": finding.category,
                "description": finding.description,
            }
        )

    return {
        "version": "1",
        "exit_code": exit_code,
        "summary": summary,
        "findings": items,
    }


def findings_to_sarif(
    findings_by_file: dict[str, list[ScanFinding]],
    files_scanned: int,
) -> dict:
    """Convert scan findings to SARIF 2.1.0 format.

    SARIF output uses relative paths only and never includes file content
    snippets to avoid leaking private repository content.
    """
    all_findings = [f for ff in findings_by_file.values() for f in ff]

    # Collect unique rules from categories
    seen_rules: dict[str, dict] = {}
    for f in all_findings:
        rid = _rule_id(f.category)
        if rid not in seen_rules:
            seen_rules[rid] = {
                "id": rid,
                "shortDescription": {
                    "text": f.category.replace("-", " ").title(),
                },
                "defaultConfiguration": {
                    "level": _SEVERITY_MAP.get(f.severity, "note"),
                },
                "helpUri": _TOOL_INFO_URI,
            }

    # Build results
    results = []
    for finding in all_findings:
        result: dict[str, Any] = {
            "ruleId": _rule_id(finding.category),
            "level": _SEVERITY_MAP.get(finding.severity, "note"),
            "message": {"text": f"{finding.description} ({finding.codepoint})"},
            "locations": [
                {
                    "physicalLocation": {
                        "artifactLocation": {
                            "uri": relative_path_for_report(finding.file),
                        },
                        "region": {
                            "startLine": finding.line,
                            "startColumn": finding.column,
                        },
                    }
                }
            ],
            "properties": {
                "codepoint": finding.codepoint,
                "category": finding.category,
            },
        }
        results.append(result)

    return {
        "$schema": _SARIF_SCHEMA,
        "version": _SARIF_VERSION,
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": _TOOL_NAME,
                        "informationUri": _TOOL_INFO_URI,
                        "rules": list(seen_rules.values()),
                    }
                },
                "results": results,
                "invocations": [
                    {
                        "executionSuccessful": True,
                        "properties": {
                            "filesScanned": files_scanned,
                        },
                    }
                ],
            }
        ],
    }


def write_report(report: dict, output_path: Path) -> None:
    """Write a report dict (JSON or SARIF) to a file."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def serialize_report(report: dict) -> str:
    """Serialize a report dict to a JSON string (for stdout)."""
    return json.dumps(report, indent=2, ensure_ascii=False)


def findings_to_markdown(
    findings_by_file: dict[str, list[ScanFinding]],
    files_scanned: int,
) -> str:
    """Convert scan findings to GitHub-Flavored Markdown.

    Designed for ``$GITHUB_STEP_SUMMARY`` and ``-o report.md``.
    """
    all_findings = [f for ff in findings_by_file.values() for f in ff]

    if not all_findings:
        return (
            f"## APM Audit Report\n\n"
            f"**Clean** — no security findings across {files_scanned} files.\n"
        )

    # Count severities
    critical = sum(1 for f in all_findings if f.severity == "critical")
    warning = sum(1 for f in all_findings if f.severity == "warning")
    info = sum(1 for f in all_findings if f.severity == "info")
    affected = len(findings_by_file)

    # Summary line
    parts = []
    if critical:
        parts.append(f"{critical} critical")
    if warning:
        parts.append(f"{warning} warning{'s' if warning != 1 else ''}")
    if info:
        parts.append(f"{info} info")
    total = len(all_findings)
    count_label = f"**{total} finding{'s' if total != 1 else ''}**"
    summary = (
        f"{count_label} across {affected} file{'s' if affected != 1 else ''}"
        f" ({', '.join(parts)}) | {files_scanned} files scanned"
    )

    # Sort: severity (critical first), then file, then line
    severity_order = {"critical": 0, "warning": 1, "info": 2}
    sorted_findings = sorted(
        all_findings,
        key=lambda f: (severity_order.get(f.severity, 3), f.file, f.line),
    )

    # Table
    lines = [
        "## APM Audit Report",
        "",
        summary,
        "",
        "| Severity | File | Location | Codepoint | Description |",
        "|----------|------|----------|-----------|-------------|",
    ]
    for f in sorted_findings:
        sev = f.severity.upper()
        escaped_desc = f.description.replace("|", "\\|")
        lines.append(
            f"| {sev} | `{relative_path_for_report(f.file)}` | {f.line}:{f.column}"
            f" | `{f.codepoint}` | {escaped_desc} |"
        )
    lines.append("")
    lines.append("Run `apm audit --strip` to remove flagged characters.\n")

    return "\n".join(lines)


def detect_format_from_extension(path: Path) -> str:
    """Auto-detect output format from file extension.

    Returns 'sarif' for .sarif/.sarif.json, 'json' for .json,
    'markdown' for .md, 'text' as default.
    """
    name = path.name.lower()
    if name.endswith(".sarif.json") or name.endswith(".sarif"):
        return "sarif"
    if name.endswith(".json"):
        return "json"
    if name.endswith(".md"):
        return "markdown"
    return "text"
