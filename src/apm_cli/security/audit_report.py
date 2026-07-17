"""Audit report serialization — JSON and SARIF output for apm audit."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..core.deployment_ledger import DEPLOYMENT_OWNER_REMEDIATION
from .content_scanner import ScanFinding

if TYPE_CHECKING:
    from ..core.deployment_ledger import DeploymentOwnerViolation


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
_DEPLOYMENT_OWNER_RULE = "apm/lockfile/deployment-owner"

# Severity mapping: APM → SARIF
_SEVERITY_MAP = {
    "critical": "error",
    "warning": "warning",
    "info": "note",
}


def _rule_id(category: str) -> str:
    """Build a SARIF rule ID from a finding category."""
    return f"apm/hidden-unicode/{category}"


def _owner_description(violation: DeploymentOwnerViolation) -> str:
    invalid = ", ".join(violation.invalid_owners)
    active = (
        f"; invalid active owner {violation.invalid_active_owner}"
        if violation.invalid_active_owner is not None
        else ""
    )
    return f"Deployment {violation.locator.key} references invalid owner(s) {invalid}{active}."


def _owner_json(violation: DeploymentOwnerViolation) -> dict[str, Any]:
    locator = violation.locator
    return {
        "severity": "critical",
        "file": "apm.lock.yaml",
        "category": "deployment-owner",
        "locator": {
            "key": locator.key,
            "kind": locator.kind.value,
            "target": locator.target,
            "value": locator.value,
            "runtime": locator.runtime,
            "scope": locator.scope,
        },
        "owners": list(violation.owners),
        "active_owner": violation.active_owner,
        "invalid_owners": list(violation.invalid_owners),
        "invalid_active_owner": violation.invalid_active_owner,
        "description": _owner_description(violation),
        "remediation": DEPLOYMENT_OWNER_REMEDIATION,
    }


def findings_to_json(
    findings_by_file: dict[str, list[ScanFinding]],
    files_scanned: int,
    exit_code: int,
    owner_violations: tuple[DeploymentOwnerViolation, ...] = (),
) -> dict:
    """Convert scan findings to APM's JSON report format."""
    all_findings = [f for ff in findings_by_file.values() for f in ff]

    summary = {
        "files_scanned": files_scanned,
        "files_affected": len(findings_by_file) + bool(owner_violations),
        "critical": (
            sum(1 for f in all_findings if f.severity == "critical") + len(owner_violations)
        ),
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
    items.extend(_owner_json(violation) for violation in owner_violations)

    return {
        "version": "1",
        "passed": exit_code == 0,
        "exit_code": exit_code,
        "summary": summary,
        "findings": items,
    }


def findings_to_sarif(
    findings_by_file: dict[str, list[ScanFinding]],
    files_scanned: int,
    owner_violations: tuple[DeploymentOwnerViolation, ...] = (),
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
    if owner_violations:
        seen_rules[_DEPLOYMENT_OWNER_RULE] = {
            "id": _DEPLOYMENT_OWNER_RULE,
            "shortDescription": {
                "text": "Invalid deployment ledger owner reference",
            },
            "defaultConfiguration": {"level": "error"},
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
    for violation in owner_violations:
        locator = violation.locator
        results.append(
            {
                "ruleId": _DEPLOYMENT_OWNER_RULE,
                "level": "error",
                "message": {
                    "text": (f"{_owner_description(violation)} {DEPLOYMENT_OWNER_REMEDIATION}")
                },
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {"uri": "apm.lock.yaml"},
                        }
                    }
                ],
                "properties": {
                    "locator": {
                        "key": locator.key,
                        "kind": locator.kind.value,
                        "target": locator.target,
                        "value": locator.value,
                        "runtime": locator.runtime,
                        "scope": locator.scope,
                    },
                    "owners": list(violation.owners),
                    "activeOwner": violation.active_owner,
                    "invalidOwners": list(violation.invalid_owners),
                    "invalidActiveOwner": violation.invalid_active_owner,
                    "category": "deployment-owner",
                },
            }
        )

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
    owner_violations: tuple[DeploymentOwnerViolation, ...] = (),
) -> str:
    """Convert scan findings to GitHub-Flavored Markdown.

    Designed for ``$GITHUB_STEP_SUMMARY`` and ``-o report.md``.
    """
    all_findings = [f for ff in findings_by_file.values() for f in ff]

    if not all_findings and not owner_violations:
        return (
            f"## APM Audit Report\n\n"
            f"**Clean** - no security findings across {files_scanned} files.\n"
        )

    critical = sum(1 for f in all_findings if f.severity == "critical") + len(owner_violations)
    warning = sum(1 for f in all_findings if f.severity == "warning")
    info = sum(1 for f in all_findings if f.severity == "info")
    affected = len(findings_by_file) + bool(owner_violations)

    # Summary line
    parts = []
    if critical:
        parts.append(f"{critical} critical")
    if warning:
        parts.append(f"{warning} warning{'s' if warning != 1 else ''}")
    if info:
        parts.append(f"{info} info")
    total = len(all_findings) + len(owner_violations)
    count_label = f"**{total} finding{'s' if total != 1 else ''}**"
    summary = (
        f"{count_label} across {affected} file{'s' if affected != 1 else ''}"
        f" ({', '.join(parts)}) | {files_scanned} files scanned"
    )

    severity_order = {"critical": 0, "warning": 1, "info": 2}
    sorted_findings = sorted(
        all_findings,
        key=lambda f: (severity_order.get(f.severity, 3), f.file, f.line),
    )

    lines = [
        "## APM Audit Report",
        "",
        summary,
    ]
    if owner_violations:
        lines.extend(
            [
                "",
                "### Lockfile integrity",
                "",
                "| Severity | Locator | Owners | Active owner |",
                "|----------|---------|--------|--------------|",
            ]
        )
        for violation in owner_violations:
            owners = ", ".join(violation.owners).replace("|", "\\|")
            lines.append(
                f"| CRITICAL | `{violation.locator.key}` | `{owners}` | "
                f"`{violation.active_owner}` |"
            )
        lines.extend(["", DEPLOYMENT_OWNER_REMEDIATION])
    if sorted_findings:
        lines.extend(
            [
                "",
                "### Content findings",
                "",
                "| Severity | File | Location | Codepoint | Description |",
                "|----------|------|----------|-----------|-------------|",
            ]
        )
        for finding in sorted_findings:
            severity = finding.severity.upper()
            escaped_desc = finding.description.replace("|", "\\|")
            lines.append(
                f"| {severity} | `{relative_path_for_report(finding.file)}` | "
                f"{finding.line}:{finding.column} | `{finding.codepoint}` | "
                f"{escaped_desc} |"
            )
        lines.extend(
            [
                "",
                "Run `apm audit --strip` to remove flagged characters.",
            ]
        )
    lines.append("")

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
