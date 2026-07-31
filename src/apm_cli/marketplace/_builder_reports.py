"""Report and options dataclasses for the marketplace builder.

Leaf module -- no imports from ``builder`` (cycle-safe).  All public
symbols are re-exported by ``builder`` so existing import paths such
as ``from apm_cli.marketplace.builder import BuildReport`` continue
to work without changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .diagnostics import BuildDiagnostic

__all__ = [
    "BuildDiagnostic",
    "BuildOptions",
    "BuildReport",
    "MarketplaceOutputReport",
    "ResolveResult",
    "ResolvedPackage",
]


@dataclass(frozen=True)
class ResolvedPackage:
    """A package entry after ref resolution."""

    name: str
    source_repo: str  # "owner/repo" only
    subdir: str | None  # APM-only (used to compose the output ``source`` object)
    ref: str  # resolved tag name, e.g. "v1.2.0"
    sha: str  # 40-char git SHA
    requested_version: str | None  # original APM-only range (for diagnostics)
    tags: tuple[str, ...]
    is_prerelease: bool  # True if the resolved ref was a prerelease semver
    host: str | None = None  # non-default git host parsed from apm.yml source
    source_url: str | None = None  # canonical URL for sourceBase-composed entries


@dataclass(frozen=True)
class ResolveResult:
    """Result of resolving package refs in a marketplace build."""

    entries: tuple[ResolvedPackage, ...]
    errors: tuple[tuple[str, str], ...]  # (package name, error message) pairs

    @property
    def ok(self) -> bool:
        """True when every package resolved without error."""
        return len(self.errors) == 0


@dataclass(frozen=True)
class MarketplaceOutputReport:
    """Summary for one generated marketplace output profile."""

    profile: str
    resolved: tuple[ResolvedPackage, ...]
    errors: tuple[tuple[str, str], ...]  # (package name, error message) pairs
    warnings: tuple[str, ...]  # non-fatal diagnostic messages
    diagnostics: tuple[BuildDiagnostic, ...] = ()  # structured diagnostics
    unchanged_count: int = 0
    added_count: int = 0
    updated_count: int = 0
    removed_count: int = 0
    output_path: Path = field(default_factory=lambda: Path("."))
    dry_run: bool = False


@dataclass(frozen=True)
class BuildReport:
    """Summary of a marketplace build run across one or more output profiles."""

    outputs: tuple[MarketplaceOutputReport, ...]

    @property
    def primary_output(self) -> MarketplaceOutputReport:
        """Return the first output report for legacy single-output callers."""
        if not self.outputs:
            return MarketplaceOutputReport(
                profile="",
                resolved=(),
                errors=(),
                warnings=(),
            )
        return self.outputs[0]

    @property
    def resolved(self) -> tuple[ResolvedPackage, ...]:
        return self.primary_output.resolved

    @property
    def errors(self) -> tuple[tuple[str, str], ...]:
        return self.primary_output.errors

    @property
    def warnings(self) -> tuple[str, ...]:
        return tuple(warn for output in self.outputs for warn in output.warnings)

    @property
    def diagnostics(self) -> tuple[BuildDiagnostic, ...]:
        return tuple(diag for output in self.outputs for diag in output.diagnostics)

    @property
    def unchanged_count(self) -> int:
        return self.primary_output.unchanged_count

    @property
    def added_count(self) -> int:
        return self.primary_output.added_count

    @property
    def updated_count(self) -> int:
        return self.primary_output.updated_count

    @property
    def removed_count(self) -> int:
        return self.primary_output.removed_count

    @property
    def output_path(self) -> Path:
        return self.primary_output.output_path

    @property
    def dry_run(self) -> bool:
        return any(output.dry_run for output in self.outputs)

    def to_json_dict(self) -> dict[str, Any]:
        """Serialize build report as the S4 JSON contract.

        Shape: {ok, dry_run, warnings[], errors[],
                marketplace: {outputs: [{format, path, added, updated,
                unchanged, skipped}]}, bundle: null}
        """
        all_warnings = list(self.warnings)
        all_errors: list[dict[str, str]] = []
        output_entries: list[dict[str, Any]] = []

        for out in self.outputs:
            output_entries.append(
                {
                    "format": out.profile,
                    "path": str(out.output_path),
                    "added": out.added_count,
                    "updated": out.updated_count,
                    "unchanged": out.unchanged_count,
                    "skipped": out.removed_count,
                }
            )
            for pkg_name, err_msg in out.errors:
                all_errors.append({"code": "build_error", "message": f"{pkg_name}: {err_msg}"})

        ok = len(all_errors) == 0
        return {
            "ok": ok,
            "dry_run": self.dry_run,
            "warnings": all_warnings,
            "errors": all_errors,
            "marketplace": {
                "outputs": output_entries,
            },
            "bundle": None,
        }

    @classmethod
    def failure_to_json_dict(
        cls,
        *,
        errors: list[dict[str, str]],
        warnings: list[str] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Produce the S4 JSON shape for a pre-build failure."""
        return {
            "ok": False,
            "dry_run": dry_run,
            "warnings": warnings or [],
            "errors": errors,
            "marketplace": {
                "outputs": [],
            },
            "bundle": None,
        }


@dataclass
class BuildOptions:
    """Configuration knobs for MarketplaceBuilder."""

    concurrency: int = 8
    timeout_seconds: float = 10.0
    include_prerelease: bool = False
    allow_head: bool = False
    continue_on_error: bool = False
    offline: bool = False
    # Backwards-compatible spelling for callers that predate ``apm pack``.
    output_override: Path | None = None
    dry_run: bool = False
