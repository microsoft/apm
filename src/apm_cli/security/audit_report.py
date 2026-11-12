"""Audit report serialization -- JSON and SARIF output for apm audit."""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..core.deployment_ledger import DEPLOYMENT_OWNER_REMEDIATION
from ..utils.diagnostics import printable_ascii_text
from .content_scanner import ScanFinding

if TYPE_CHECKING:
    from ..core.deployment_ledger import DeploymentOwnerViolation
    from .file_scanner import CoverageEntry


def relative_path_for_report(file_path: str) -> str:
    """Ensure paths in reports are relative with forward slashes."""
    p = Path(file_path)
    if p.is_absolute():
        try:
            return p.relative_to(Path.cwd()).as_posix()
        except ValueError:
            return p.name
    return file_path.replace("\\", "/")


def finding_location(finding: ScanFinding) -> str:
    """Distinguish physical source positions from decoded structured prompt offsets."""
    offset = f"{finding.line}:{finding.column}"
    return f"{finding.pointer} (decoded {offset})" if finding.pointer else offset


def _markdown_cell(value: str) -> str:
    """Render untrusted labels as printable, literal Markdown table content."""
    return re.sub(r"([\\`*_{}\[\]<>()|#!&])", r"\\\1", printable_ascii_text(value))


def _markdown_code(value: str) -> str:
    """Keep filename code spans literal even when the filename contains backticks."""
    value = printable_ascii_text(value).replace("|", "\\|")
    delimiter = "`" * (1 + max((len(run) for run in re.findall(r"`+", value)), default=0))
    return f"{delimiter} {value} {delimiter}" if "`" in value else f"{delimiter}{value}{delimiter}"


# SARIF schema version
_SARIF_VERSION = "2.1.0"
_SARIF_SCHEMA = (
    "https://docs.oasis-open.org/sarif/sarif/v2.1.0/cos02/schemas/sarif-schema-2.1.0.json"
)
_TOOL_NAME = "apm-audit"
_TOOL_INFO_URI = "https://apm.github.io/apm/enterprise/security/"
_DEPLOYMENT_OWNER_RULE = "apm/lockfile/deployment-owner"

# Severity mapping: APM -> SARIF
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
    coverage: tuple[CoverageEntry, ...] = (),
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

    items = [finding_to_json(finding) for finding in all_findings]
    items.extend(_owner_json(violation) for violation in owner_violations)

    report = {
        "version": "1",
        "passed": exit_code == 0,
        "exit_code": exit_code,
        "summary": summary,
        "findings": items,
    }
    if coverage:
        report["coverage"] = {
            "complete": not any(entry.status == "incomplete" for entry in coverage),
            "primitives": [asdict(entry) for entry in coverage],
        }
    return report


def finding_to_json(finding: ScanFinding) -> dict[str, Any]:
    """Serialize one finding without conflating decoded and physical coordinates."""
    return {
        "severity": finding.severity,
        "file": relative_path_for_report(finding.file),
        **(
            {
                "coordinate_space": "decoded-prompt",
                "decoded_line": finding.line,
                "decoded_column": finding.column,
            }
            if finding.pointer
            else {"line": finding.line, "column": finding.column}
        ),
        "codepoint": finding.codepoint,
        "category": finding.category,
        "description": finding.description,
        **({"pointer": finding.pointer} if finding.pointer else {}),
    }


def finding_to_sarif(finding: ScanFinding) -> dict[str, Any]:
    """Use the actual prompt artifact and never invent physical native offsets."""
    return {
        "ruleId": _rule_id(finding.category),
        "level": _SEVERITY_MAP.get(finding.severity, "note"),
        "message": {"text": f"{finding.description} ({finding.codepoint})"},
        "locations": [
            {
                "physicalLocation": {
                    "artifactLocation": {"uri": relative_path_for_report(finding.file)},
                    **(
                        {"region": {"startLine": finding.line, "startColumn": finding.column}}
                        if not finding.pointer
                        else {}
                    ),
                }
            }
        ],
        "properties": {
            "codepoint": finding.codepoint,
            "category": finding.category,
            **({"pointer": finding.pointer} if finding.pointer else {}),
        },
    }


def findings_to_sarif(
    findings_by_file: dict[str, list[ScanFinding]],
    files_scanned: int,
    owner_violations: tuple[DeploymentOwnerViolation, ...] = (),
    coverage: tuple[CoverageEntry, ...] = (),
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
    results = [finding_to_sarif(finding) for finding in all_findings]
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

    coverage_errors = [entry for entry in coverage if entry.status == "incomplete"]
    if coverage_errors:
        seen_rules["apm/audit/incomplete-coverage"] = {
            "id": "apm/audit/incomplete-coverage",
            "shortDescription": {"text": "Incomplete primitive prompt coverage"},
        }
        for entry in coverage_errors:
            results.append(
                {
                    "ruleId": "apm/audit/incomplete-coverage",
                    "level": "error",
                    "message": {
                        "text": f"{entry.diagnostic}; review format or file access and rerun audit."
                    },
                    "locations": [
                        {
                            "physicalLocation": {
                                "artifactLocation": {"uri": relative_path_for_report(entry.file)},
                            }
                        }
                    ],
                    "properties": {"pointer": entry.pointer},
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
                        "executionSuccessful": not coverage_errors,
                        "properties": {
                            "filesScanned": files_scanned,
                            **(
                                {"primitiveCoverage": [asdict(entry) for entry in coverage]}
                                if coverage
                                else {}
                            ),
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
        json.dumps(report, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )


def serialize_report(report: dict) -> str:
    """Serialize a report dict to a JSON string (for stdout)."""
    return json.dumps(report, indent=2, ensure_ascii=True)


def findings_to_markdown(
    findings_by_file: dict[str, list[ScanFinding]],
    files_scanned: int,
    owner_violations: tuple[DeploymentOwnerViolation, ...] = (),
    coverage: tuple[CoverageEntry, ...] = (),
) -> str:
    """Convert scan findings to GitHub-Flavored Markdown.

    Designed for ``$GITHUB_STEP_SUMMARY`` and ``-o report.md``.
    """
    all_findings = [f for ff in findings_by_file.values() for f in ff]

    coverage_lines: list[str] = []
    if coverage:
        coverage_lines = [
            "",
            "### Primitive coverage",
            "",
            "Discovery does not establish ownership or verify a hash.",
            "",
            "| File / field | Tracking | Prompt check | Detail |",
            "|--------------|----------|--------------|--------|",
        ]
        for entry in coverage:
            values = (
                entry.file + entry.pointer,
                "recorded-file" if entry.tracked else "untracked",
                entry.status,
                entry.diagnostic or "",
            )
            coverage_lines.append(
                "| " + " | ".join(_markdown_cell(value) for value in values) + " |"
            )
    incomplete = any(entry.status == "incomplete" for entry in coverage)
    if not all_findings and not owner_violations and not incomplete:
        return (
            f"## APM Audit Report\n\n"
            f"**Clean** - no security findings across {files_scanned} files.\n"
        ) + "\n".join(coverage_lines)

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
    if incomplete:
        lines.extend(
            [
                "",
                "**Incomplete coverage** - review the reported format or file access and rerun audit.",
            ]
        )
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
            escaped_desc = _markdown_cell(finding.description)
            lines.append(
                f"| {severity} | {_markdown_code(relative_path_for_report(finding.file))} | "
                f"{_markdown_cell(finding_location(finding))} | `{finding.codepoint}` | "
                f"{escaped_desc} |"
            )
        lines.extend(
            [
                "",
                "Review structured prompt fields manually; use `apm audit --strip` only for eligible regular documents."
                if any(not entry.strippable and entry.status == "checked" for entry in coverage)
                else "Run `apm audit --strip` to remove flagged characters.",
            ]
        )
    lines.extend(coverage_lines)
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
