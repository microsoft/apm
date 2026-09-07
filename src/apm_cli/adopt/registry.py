"""Pluggable scanner registry for brownfield discovery.

A :class:`ToolScanner` yields :class:`~apm_cli.adopt.model.RawFinding`
objects for one family of harness artifacts. Built-in scanners register in
``apm_cli.adopt.scanners``; third parties call :meth:`ScannerRegistry.register`.

Every built-in rule is a *scoped* glob (one directory, optionally its
subtree) so a scan costs proportional to the harness directories present, not
to the size of the workspace.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Protocol, runtime_checkable

from apm_cli.constants import DEFAULT_SKIP_DIRS
from apm_cli.integration.targets import TargetProfile
from apm_cli.utils.path_security import PathTraversalError, ensure_path_within

from .model import HarnessKind, RawFinding, Risk, ScanError, Scope
from .redact import Redactor

_PROBE_BYTES = 4096


@dataclass(frozen=True)
class ScanLimits:
    """Bounds that keep discovery cheap on large trees."""

    max_file_bytes: int = 1_048_576
    max_files_per_rule: int = 500
    max_skill_dirs: int = 200


@dataclass
class ScanContext:
    """Everything a scanner needs for one scope."""

    root: Path
    scope: Scope
    targets: tuple[TargetProfile, ...]
    redactor: Redactor
    limits: ScanLimits = field(default_factory=ScanLimits)
    errors: list[ScanError] = field(default_factory=list)

    def display(self, path: Path) -> str:
        return self.redactor.path(path, self.scope)

    def error(self, path: Path, reason: str) -> None:
        self.errors.append(ScanError(display_path=self.display(path), reason=reason))


@dataclass(frozen=True)
class ScanRule:
    """One scoped glob that maps files to a harness kind."""

    tool: str
    kind: HarnessKind
    relative_glob: str
    """Posix glob relative to the scan root, e.g. ``.cursor/rules/*.mdc``.

    The leading wildcard-free segments form the directory that is opened; the
    remaining segments (which may contain ``*``, ``?`` or ``[...]`` in any
    position, e.g. ``*/SKILL.md``) are handed to ``Path.glob``. ``**`` is
    rejected so a rule can never walk the whole workspace; ``recursive=True``
    widens the match to the subtree below the rule directory instead.
    """
    recursive: bool = False
    is_dir_rule: bool = False
    """When set, the finding is the *parent directory* of the matched file."""
    format_id: str | None = None
    primitive: str | None = None
    risk: frozenset[Risk] = frozenset()
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if "**" in self.relative_glob:
            raise ValueError(
                f"ScanRule glob {self.relative_glob!r} must not use '**'; "
                "set recursive=True on a scoped directory instead"
            )

    def _split(self) -> tuple[str, str]:
        """Split into (literal directory prefix, wildcard pattern for Path.glob)."""
        parts = PurePosixPath(self.relative_glob).parts
        for index, part in enumerate(parts):
            if any(ch in part for ch in "*?["):
                return "/".join(parts[:index]), "/".join(parts[index:])
        return "/".join(parts[:-1]), parts[-1]

    @property
    def directory(self) -> str:
        return self._split()[0] or "."

    @property
    def pattern(self) -> str:
        return self._split()[1]


@runtime_checkable
class ToolScanner(Protocol):
    """A source of raw findings."""

    name: str

    def scan(self, ctx: ScanContext) -> Iterable[RawFinding]: ...


class ScannerRegistry:
    """Ordered collection of scanners."""

    def __init__(self) -> None:
        self._scanners: list[ToolScanner] = []

    def register(self, scanner: ToolScanner, *, before: str | None = None) -> None:
        if any(existing.name == scanner.name for existing in self._scanners):
            raise ValueError(f"scanner {scanner.name!r} is already registered")
        if before is None:
            self._scanners.append(scanner)
            return
        for index, existing in enumerate(self._scanners):
            if existing.name == before:
                self._scanners.insert(index, scanner)
                return
        raise ValueError(f"unknown scanner {before!r}")

    def scanners(self) -> tuple[ToolScanner, ...]:
        return tuple(self._scanners)

    def scan_all(self, ctx: ScanContext) -> list[RawFinding]:
        """Run every scanner and drop duplicate (scope, path, kind) hits."""
        seen: set[tuple[Scope, str, HarnessKind]] = set()
        results: list[RawFinding] = []
        for scanner in self._scanners:
            for raw in scanner.scan(ctx):
                key_path = raw.abs_path.as_posix() if raw.abs_path else raw.display_path
                key = (raw.scope, key_path, raw.kind)
                if key in seen:
                    continue
                seen.add(key)
                results.append(raw)
        return results


REGISTRY = ScannerRegistry()


# ---------------------------------------------------------------------------
# Shared filesystem helpers
# ---------------------------------------------------------------------------


def probe_text(path: Path, limit: int = _PROBE_BYTES) -> bytes | None:
    """Return the first *limit* bytes when *path* is readable text, else ``None``."""
    try:
        with path.open("rb") as handle:
            head = handle.read(limit)
    except OSError:
        return None
    if b"\x00" in head:
        return None
    return head


def _skipped(rel: PurePosixPath) -> bool:
    return any(part in DEFAULT_SKIP_DIRS for part in rel.parts[:-1])


def _resolve_within(ctx: ScanContext, path: Path) -> Path | None:
    """Resolve *path*, rejecting symlinks that escape the scan root."""
    if not path.is_symlink():
        return path
    resolved = path.resolve(strict=False)
    try:
        ensure_path_within(resolved, ctx.root)
    except PathTraversalError:
        ctx.error(path, "symlink escapes scan root")
        return None
    return resolved


def iter_rule_matches(ctx: ScanContext, rule: ScanRule) -> Iterator[Path]:
    """Yield files matching *rule* under ``ctx.root`` within the configured limits."""
    directory = ctx.root / rule.directory
    if not directory.is_dir():
        return
    matches = directory.rglob(rule.pattern) if rule.recursive else directory.glob(rule.pattern)
    count = 0
    for candidate in sorted(matches):
        try:
            rel = PurePosixPath(candidate.relative_to(ctx.root).as_posix())
        except ValueError:
            rel = PurePosixPath(candidate.as_posix())
        if _skipped(rel):
            continue
        if not candidate.is_file():
            continue
        count += 1
        if count > ctx.limits.max_files_per_rule:
            ctx.error(directory, f"more than {ctx.limits.max_files_per_rule} matches; truncated")
            return
        yield candidate


def findings_for_rule(ctx: ScanContext, rule: ScanRule) -> Iterator[RawFinding]:
    """Turn every match of *rule* into a raw finding, recording unreadable paths."""
    for candidate in iter_rule_matches(ctx, rule):
        target = _resolve_within(ctx, candidate)
        if target is None:
            continue
        subject = candidate.parent if rule.is_dir_rule else candidate
        try:
            size = os.path.getsize(target)
        except OSError:
            ctx.error(candidate, "unreadable")
            continue
        notes = list(rule.notes)
        if probe_text(target) is None:
            ctx.error(candidate, "binary or unreadable")
            continue
        if size > ctx.limits.max_file_bytes:
            notes.append("oversize; not parsed")
        yield RawFinding(
            tool=rule.tool,
            scope=ctx.scope,
            kind=rule.kind,
            display_path=ctx.display(subject),
            abs_path=subject,
            size_bytes=None if rule.is_dir_rule else size,
            format_id=rule.format_id,
            primitive=rule.primitive,
            risk=rule.risk,
            notes=tuple(notes),
            evidence=(f"rule:{rule.relative_glob}",),
        )
