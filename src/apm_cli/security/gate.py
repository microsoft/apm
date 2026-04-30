"""Centralized security scanning gate for all APM commands.

Every command that reads or writes files passes through SecurityGate
instead of reimplementing scan→classify→decide→report inline.
Commands declare *intent* via ScanPolicy; the gate handles the rest.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Literal, Optional  # noqa: F401, UP035

from ..utils.paths import portable_relpath
from .content_scanner import ContentScanner, ScanFinding

# ---------------------------------------------------------------------------
# Policy & Verdict
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScanPolicy:
    """Declares how a command handles security findings.

    Attributes:
        on_critical: ``"block"`` exits / blocks deployment,
                     ``"warn"`` continues with a warning,
                     ``"ignore"`` collects findings silently.
        force_overrides: When True, ``--force`` downgrades ``block`` to ``warn``.
    """

    on_critical: Literal["block", "warn", "ignore"] = "block"
    force_overrides: bool = True

    def effective_block(self, force: bool) -> bool:
        """Return True when this policy would block deployment."""
        return self.on_critical == "block" and not (self.force_overrides and force)


# Pre-built policies — import these instead of constructing ad-hoc ones.
BLOCK_POLICY = ScanPolicy(on_critical="block", force_overrides=True)
WARN_POLICY = ScanPolicy(on_critical="warn", force_overrides=False)
REPORT_POLICY = ScanPolicy(on_critical="ignore", force_overrides=False)


@dataclass(frozen=True)
class ScanVerdict:
    """Result of a SecurityGate check."""

    findings_by_file: dict[str, list[ScanFinding]] = field(default_factory=dict)
    has_critical: bool = False
    should_block: bool = False
    critical_count: int = 0
    warning_count: int = 0
    files_scanned: int = 0

    @property
    def has_findings(self) -> bool:
        return bool(self.findings_by_file)

    @property
    def all_findings(self) -> list[ScanFinding]:
        """Flatten findings across all files."""
        return [f for ff in self.findings_by_file.values() for f in ff]


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


class SecurityGate:
    """Single entry point for security scanning across all commands."""

    @staticmethod
    def scan_files(
        root: Path,
        *,
        policy: ScanPolicy = BLOCK_POLICY,
        force: bool = False,
    ) -> ScanVerdict:
        """Walk *root*, scan every regular file, return a verdict.

        Symlinks are never followed (``followlinks=False``, ``is_symlink()``).
        All files are scanned to produce a complete findings report.
        """
        findings_by_file: dict[str, list[ScanFinding]] = {}
        files_scanned = 0

        for dirpath, _dirs, filenames in os.walk(root, followlinks=False):
            for fname in filenames:
                fpath = Path(dirpath) / fname
                if fpath.is_symlink():
                    continue
                files_scanned += 1
                try:
                    file_findings = ContentScanner.scan_file(fpath)
                except OSError:
                    continue
                if file_findings:
                    rel = portable_relpath(fpath, root)
                    findings_by_file[rel] = file_findings

        return SecurityGate._build_verdict(findings_by_file, files_scanned, policy, force)

    @staticmethod
    def scan_text(
        content: str,
        filename: str,
        *,
        policy: ScanPolicy = BLOCK_POLICY,
    ) -> ScanVerdict:
        """Scan in-memory text (compiled output, generated files)."""
        file_findings = ContentScanner.scan_text(content, filename=filename)
        findings_by_file: dict[str, list[ScanFinding]] = {}
        if file_findings:
            findings_by_file[filename] = file_findings
        return SecurityGate._build_verdict(findings_by_file, 1, policy, force=False)

    @staticmethod
    def report(
        verdict: ScanVerdict,
        diagnostics,
        *,
        package: str = "",
        force: bool = False,
    ) -> None:
        """Record findings into a DiagnosticCollector with consistent messaging."""
        if not verdict.has_findings:
            return

        if verdict.has_critical and not verdict.should_block and force:
            # --force: deployed despite critical
            diagnostics.security(
                message=("Deployed with --force despite critical hidden characters"),
                package=package,
                detail=(
                    f"{verdict.critical_count} critical finding(s) — "
                    "run 'apm audit --strip' to clean up"
                ),
                severity="critical",
            )
        elif verdict.has_critical and verdict.should_block:
            diagnostics.security(
                message=(
                    "Blocked — critical hidden characters in source. Use --force to override."
                ),
                package=package,
                detail=f"at least {verdict.critical_count} critical, "
                f"{verdict.warning_count} warning(s)",
                severity="critical",
            )
        elif verdict.has_critical:
            # Warn-only policy with critical findings (e.g. pack, compile)
            diagnostics.security(
                message="Critical hidden characters in source files",
                package=package,
                detail=(
                    f"{verdict.critical_count} critical, "
                    f"{verdict.warning_count} warning(s) — "
                    "run 'apm audit' to inspect"
                ),
                severity="critical",
            )
        elif verdict.warning_count > 0:
            diagnostics.security(
                message="Hidden characters in source files",
                package=package,
                detail=(
                    f"{verdict.warning_count} warning(s) — run 'apm audit --strip' after install"
                ),
                severity="warning",
            )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _build_verdict(
        findings_by_file: dict[str, list[ScanFinding]],
        files_scanned: int,
        policy: ScanPolicy,
        force: bool,
    ) -> ScanVerdict:
        if not findings_by_file:
            return ScanVerdict(files_scanned=files_scanned)

        flat = [f for ff in findings_by_file.values() for f in ff]
        has_critical, counts = ContentScanner.classify(flat)
        should_block = has_critical and policy.effective_block(force)

        return ScanVerdict(
            findings_by_file=findings_by_file,
            has_critical=has_critical,
            should_block=should_block,
            critical_count=counts.get("critical", 0),
            warning_count=counts.get("warning", 0),
            files_scanned=files_scanned,
        )


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------


def ignore_symlinks(directory, contents):
    """``shutil.copytree`` ignore callback that filters out symlinks."""
    return [c for c in contents if (Path(directory) / c).is_symlink()]
