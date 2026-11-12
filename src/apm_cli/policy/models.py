"""Data models for CI/policy audit checks.

Provides :class:`CheckResult` and :class:`CIAuditResult` used by both
baseline checks (``ci_checks``) and policy checks (``policy_checks``).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apm_cli.security.content_scanner import ScanFinding
    from apm_cli.security.file_scanner import CoverageEntry

# Check name -> most relevant artifact for SARIF locations.
_CHECK_ARTIFACT_MAP: dict[str, str] = {
    "lockfile-exists": "apm.lock.yaml",
    "ref-consistency": "apm.lock.yaml",
    "deployment-ledger-owners": "apm.lock.yaml",
    "deployed-files-present": "apm.lock.yaml",
    "no-orphaned-packages": "apm.lock.yaml",
    "config-consistency": "apm.lock.yaml",
    "content-integrity": "apm.lock.yaml",
    "dependency-allowlist": "apm.yml",
    "dependency-denylist": "apm.yml",
    "required-packages": "apm.yml",
    "required-packages-deployed": "apm.lock.yaml",
    "required-executable-untrusted": "apm.lock.yaml",
    "required-package-version": "apm.lock.yaml",
    "transitive-depth": "apm.lock.yaml",
    "mcp-allowlist": "apm.yml",
    "mcp-denylist": "apm.yml",
    "mcp-transport": "apm.yml",
    "mcp-self-defined": "apm.yml",
    "compilation-target": "apm.yml",
    "compilation-strategy": "apm.yml",
    "source-attribution": "apm.yml",
    "required-manifest-fields": "apm.yml",
    "scripts-policy": "apm.yml",
    "unmanaged-files": "apm.yml",
    "manifest-parse": "apm.yml",
    "manifest-missing": "apm.yml",
}


# ── Result data classes ───────────────────────────────────────────


@dataclass
class CheckResult:
    """Result of a single CI check."""

    name: str  # e.g., "lockfile-exists"
    passed: bool
    message: str  # human-readable description
    details: list[str] = field(default_factory=list)  # individual violations
    coverage: tuple[CoverageEntry, ...] = ()
    content_findings: tuple[ScanFinding, ...] = ()


@dataclass
class CIAuditResult:
    """Aggregate result of all CI checks."""

    checks: list[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def failed_checks(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.passed]

    def to_json(self) -> dict:
        """Serialize to JSON-compatible dict."""
        from apm_cli.security.audit_report import finding_to_json

        return {
            "passed": self.passed,
            "checks": [
                {
                    "name": c.name,
                    "passed": c.passed,
                    "message": c.message,
                    "details": c.details,
                    **(
                        {"content_findings": [finding_to_json(f) for f in c.content_findings]}
                        if c.content_findings
                        else {}
                    ),
                    **(
                        {"primitive_coverage": [asdict(entry) for entry in c.coverage]}
                        if c.coverage
                        else {}
                    ),
                }
                for c in self.checks
            ],
            "summary": {
                "total": len(self.checks),
                "passed": sum(1 for c in self.checks if c.passed),
                "failed": sum(1 for c in self.checks if not c.passed),
            },
        }

    def to_sarif(self) -> dict:
        """Serialize to SARIF v2.1.0 format for GitHub Code Scanning."""
        from apm_cli.security.audit_report import finding_to_sarif

        try:
            from importlib.metadata import version as pkg_version

            tool_version = pkg_version("apm-cli")
        except Exception:
            tool_version = "0.0.0"

        results = []
        for check in self.checks:
            if not check.passed:
                artifact = _CHECK_ARTIFACT_MAP.get(check.name, "apm.lock.yaml")
                content_details = {
                    f"unicode: {finding.file}{finding.pointer or ''}"
                    for finding in check.content_findings
                }
                results.extend(
                    {**finding_to_sarif(finding), "ruleId": check.name}
                    for finding in check.content_findings
                )
                for detail in check.details or [check.message]:
                    if detail in content_details:
                        continue
                    results.append(
                        {
                            "ruleId": check.name,
                            "level": "error",
                            "message": {"text": detail},
                            "locations": [
                                {
                                    "physicalLocation": {
                                        "artifactLocation": {
                                            "uri": artifact,
                                        },
                                    },
                                }
                            ],
                        }
                    )
        return {
            "$schema": (
                "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/"
                "main/sarif-2.1/schema/sarif-schema-2.1.0.json"
            ),
            "version": "2.1.0",
            "runs": [
                {
                    "tool": {
                        "driver": {
                            "name": "apm-audit",
                            "version": tool_version,
                            "informationUri": "https://github.com/microsoft/apm",
                            "rules": [
                                {
                                    "id": check.name,
                                    "shortDescription": {"text": check.message},
                                }
                                for check in self.checks
                                if not check.passed
                            ],
                        },
                    },
                    "results": results,
                    "invocations": [
                        {
                            "executionSuccessful": not any(
                                entry.status == "incomplete"
                                for check in self.checks
                                for entry in check.coverage
                            ),
                            "properties": {
                                "primitiveCoverage": [
                                    asdict(entry)
                                    for check in self.checks
                                    for entry in check.coverage
                                ],
                            },
                        }
                    ],
                }
            ],
        }
