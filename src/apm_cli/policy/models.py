"""Data models for CI/policy audit checks.

Provides :class:`CheckResult` and :class:`CIAuditResult` used by both
baseline checks (``ci_checks``) and policy checks (``policy_checks``).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apm_cli.core.remediation import Remediation
    from apm_cli.models.dependency.reference import DependencyReference
    from apm_cli.security.content_scanner import ScanFinding
    from apm_cli.security.file_scanner import CoverageEntry

# Check name -> most relevant artifact for SARIF locations.
_CHECK_ARTIFACT_MAP: dict[str, str] = {
    "lockfile-exists": "apm.lock.yaml",
    "ref-consistency": "apm.lock.yaml",
    "deployment-ledger-owners": "apm.lock.yaml",
    "deployed-files-present": "apm.lock.yaml",
    "no-orphaned-packages": "apm.lock.yaml",
    "lockfile-targets-current": "apm.lock.yaml",
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
    "mcp-trust-transitive": "apm.yml",
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


class PolicyRemedy(StrEnum):
    """The apm.yml edit a policy violation hint may suggest.

    The single vocabulary for install remediation hints: producers
    (``policy_checks``) pick a member and the install logger branches on
    members only. ``None`` (or anything outside this enum) means no apm.yml
    edit applies.
    """

    REMOVE = "remove"
    ADD = "add"
    PIN = "pin"


@dataclass(frozen=True)
class PolicySubject:
    """One offending dependency or MCP server named by a policy check.

    ``remedy`` selects the apm.yml edit the install hint suggests:
    :attr:`PolicyRemedy.REMOVE` (the subject is declared and must go),
    :attr:`PolicyRemedy.ADD` (a required subject is missing),
    :attr:`PolicyRemedy.PIN` (the declared ref must be bounded), or ``None``
    (no apm.yml edit applies; ``reason`` carries the remediation). ``declared_by`` is the lock key of
    the direct apm.yml dependency that brings in a transitive MCP server
    (never a package display name), so the hint points at an entry the user
    can actually remove.
    """

    ref: str
    reason: str
    remedy: PolicyRemedy | None = PolicyRemedy.REMOVE
    declared_by: str | None = None


@dataclass(frozen=True)
class LocalBundleSubjects:
    """What a local bundle presents to org dependency policy (#4646).

    ``names`` are the bundle itself as ``_local/<name>``. The bundle author
    chooses that name, so it meets ``dependencies.deny`` only and never
    satisfies ``dependencies.allow``. ``packages`` are the packages the
    bundle's embedded lockfile records; they meet both lists, matched as a
    remote dependency is. Under an allow list a bundle with no ``packages``
    is refused: nothing in it can be matched against the list.
    """

    names: tuple[DependencyReference, ...]
    packages: tuple[DependencyReference, ...]


# Checks whose violation names an org policy field rather than a dependency
# (#4826): the field a violation line cites, and the edit that clears it.
POLICY_FIELD_FIXES: dict[str, tuple[str, str]] = {
    "scripts-policy": (
        "manifest.scripts: deny",
        "Remove the scripts: block from apm.yml, or ask your policy admin to allow scripts.",
    ),
    "unmanaged-files": (
        "unmanaged_files.action: deny",
        "Move or delete each listed file, or ask your policy admin to exclude it.",
    ),
    "transitive-depth": (
        "dependencies.max_depth",
        "Reduce the dependency nesting, or ask your policy admin to raise dependencies.max_depth.",
    ),
}


@dataclass
class CheckResult:
    """Result of a single CI check."""

    name: str  # e.g., "lockfile-exists"
    passed: bool
    message: str  # human-readable description
    details: list[str] = field(default_factory=list)  # individual violations
    coverage: tuple[CoverageEntry, ...] = ()
    content_findings: tuple[ScanFinding, ...] = ()
    # Structured per-dependency / per-MCP-server violations. Not serialized:
    # ``details`` stays the report contract for ``apm audit``.
    subjects: tuple[PolicySubject, ...] = ()
    # A violation downgraded to pass by ``enforcement: warn``; text output
    # still names its subjects (#3994). Not serialized: ``passed`` is the contract.
    warned: bool = False
    # Structured advice that clears this failure, first one primary; serialized
    # by ``apm audit --format json`` as ``remediation`` and ``remediations``.
    remediations: tuple[Remediation, ...] = ()
    # One rendered catalog sentence that clears this failure, for checks whose
    # ``message`` does not already carry its remedy: text output prints it after
    # the details and SARIF appends it once, to the check's last result (#4570).
    next_step: str = ""

    def violation_subjects(self) -> tuple[PolicySubject, ...]:
        """Return one :class:`PolicySubject` per violation to report.

        The single owner for turning a check into per-subject violation
        lines (install gate and install preflight). Checks that name
        dependencies or MCP servers populate ``subjects``; any other check
        yields one entry keyed by the check name (its category) with no
        apm.yml edit suggested.
        """
        field_fix = POLICY_FIELD_FIXES.get(self.name)
        if self.subjects:
            if field_fix is None:
                return self.subjects
            return tuple(
                replace(s, reason=f"{s.reason} ({field_fix[0]}). {field_fix[1]}")
                for s in self.subjects
            )
        reason = self.message
        if self.details:
            reason = f"{self.message}: {', '.join(self.details[:5])}"
        if field_fix is None:
            return (PolicySubject(ref=self.name, reason=reason, remedy=None),)
        return (PolicySubject(ref=field_fix[0], reason=f"{reason}. {field_fix[1]}", remedy=None),)


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
                    **(
                        {
                            "remediation": c.remediations[0].to_json(),
                            "remediations": [item.to_json() for item in c.remediations],
                        }
                        if c.remediations
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
                    f"unicode: {finding.location}" for finding in check.content_findings
                }
                results.extend(
                    {**finding_to_sarif(finding), "ruleId": check.name}
                    for finding in check.content_findings
                )
                details = [
                    detail
                    for detail in check.details or [check.message]
                    if detail not in content_details
                ]
                for index, detail in enumerate(details):
                    # The remedy names every selector: emit it once per check, on
                    # the last result, so the report grows linearly (#4570).
                    last = index == len(details) - 1
                    text = f"{detail}. {check.next_step}" if check.next_step and last else detail
                    results.append(
                        {
                            "ruleId": check.name,
                            "level": "error",
                            "message": {"text": text},
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
