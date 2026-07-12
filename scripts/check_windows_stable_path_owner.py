#!/usr/bin/env python3
"""Canonical checker for the Windows stable executable path owner.

install.ps1 is the single canonical owner of the Windows "stable"
executable path: it alone may declare ``$currentDir`` (the
``current`` directory under the install root), ``$currentExe`` (the
``apm.exe`` file inside it), and the ``Add-ToUserPath`` call that
publishes ``$currentDir`` on the user's PATH. No other production
source may re-derive that path, or the two copies will silently drift
the next time one side is patched (see
``.github/instructions/architecture.instructions.md``).

This module is the ONE place that owns this check. It is consumed by
both:

  * ``scripts/lint-architecture-boundaries.sh`` (AC8), which shells out
    to this script and folds any nonzero exit into a single boundary
    violation; and
  * ``tests/integration/test_architecture_authorities.py``, which
    imports and calls this module directly instead of re-implementing
    its regexes or globs.

Two things are checked:

1. **Owner presence** -- ``install.ps1`` must contain each of the
   required owner statements verbatim.
2. **Duplicate derivation** -- no production file under
   ``src/apm_cli/**/*.py``, ``.github/workflows/**/*.{yml,yaml}``, or
   ``scripts/windows/**/*.ps1`` (excluding files whose basename starts
   with ``test-``, which are black-box validators, not owners) may
   contain a literal ``current\\apm.exe`` / ``current/apm.exe`` path or
   a ``Join-Path`` expression that derives a quoted ``current`` child,
   in either positional (``Join-Path $x "current"``) or named-parameter
   (``Join-Path -Path $x -ChildPath "current"``) form.

A line that carries the ``architecture-authority-exempt:`` marker is
always skipped, regardless of which pattern it would otherwise match.
This is a narrow, line-oriented text scan -- it is not a PowerShell (or
Python, or YAML) parser, and it is not meant to become one.

Exit code is 0 when clean, 1 when any violation is found. Diagnostics
are printed one per line, in deterministic (sorted) order.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# Repository root, derived from this file's location (scripts/<this file>).
REPO_ROOT = Path(__file__).resolve().parent.parent

INSTALL_PS1_RELATIVE = "install.ps1"

# Verbatim statements that must exist somewhere in install.ps1. These are
# intentionally exact strings, not regexes: the owner statements are
# hand-written and any drift in their shape should fail loudly rather than
# be silently tolerated by a looser pattern.
REQUIRED_OWNER_STATEMENTS: tuple[str, ...] = (
    '$currentDir = Join-Path $installRoot "current"',
    '$currentExe = Join-Path $currentDir "apm.exe"',
    "Add-ToUserPath -PathEntry $currentDir",
)

# Line-level opt-out marker. Any line containing this text is skipped by
# the duplicate scan entirely, matching the convention documented at the
# top of scripts/lint-architecture-boundaries.sh.
EXEMPT_MARKER = "architecture-authority-exempt:"

# Literal stable-path forms: current\apm.exe (Windows) or current/apm.exe.
_LITERAL_STABLE_EXE = re.compile(r"current[\\/]apm\.exe")

# A Join-Path call is a duplicate derivation if it appears on the same
# line as a quoted "current" (single or double quotes), regardless of
# whether "current" is passed positionally or via -ChildPath / -Path.
_JOIN_PATH_CALL = re.compile(r"Join-Path", re.IGNORECASE)
_QUOTED_CURRENT = re.compile(r"""['"]current['"]""")

# (subdirectory relative to repo root, file suffixes to scan)
_GUARDED_LOCATIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("src/apm_cli", (".py",)),
    (".github/workflows", (".yml", ".yaml")),
    ("scripts/windows", (".ps1",)),
)


@dataclass(frozen=True)
class DuplicateHit:
    """One line that re-derives the canonical stable path."""

    path: Path
    line_no: int
    text: str


def _is_duplicate_line(line: str) -> bool:
    """Return True if `line` re-derives the stable current/apm.exe path."""
    if EXEMPT_MARKER in line:
        return False
    if _LITERAL_STABLE_EXE.search(line):
        return True
    return bool(_JOIN_PATH_CALL.search(line) and _QUOTED_CURRENT.search(line))


def _guarded_files(root: Path) -> list[Path]:
    """List production files subject to the duplicate-derivation scan.

    Recurses under each guarded location and excludes any file whose
    basename starts with ``test-`` (black-box validators such as
    ``scripts/windows/test-install-script.ps1`` are not owners).
    """
    files: list[Path] = []
    for subdir, suffixes in _GUARDED_LOCATIONS:
        base = root / subdir
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix not in suffixes:
                continue
            if path.name.startswith("test-"):
                continue
            files.append(path)
    return sorted(files)


def find_owner_violations(root: Path) -> list[str]:
    """Return diagnostics for missing canonical owner statements."""
    install_ps1 = root / INSTALL_PS1_RELATIVE
    if not install_ps1.is_file():
        return [f"[x] {INSTALL_PS1_RELATIVE} does not exist; cannot own the stable path"]
    text = install_ps1.read_text(encoding="utf-8")
    return [
        f"[x] install.ps1 is missing canonical owner statement: {statement}"
        for statement in REQUIRED_OWNER_STATEMENTS
        if statement not in text
    ]


def find_duplicate_hits(root: Path) -> list[DuplicateHit]:
    """Return every duplicate-derivation line found in guarded files."""
    hits: list[DuplicateHit] = []
    for path in _guarded_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            if _is_duplicate_line(line):
                hits.append(DuplicateHit(path=path, line_no=line_no, text=line.strip()))
    return hits


def find_duplicate_violations(root: Path) -> list[str]:
    """Return diagnostics, one per duplicate-derivation line, sorted."""
    diagnostics = [
        f"[x] duplicate stable-path derivation: {hit.path.relative_to(root).as_posix()}:"
        f"{hit.line_no}: {hit.text}"
        for hit in find_duplicate_hits(root)
    ]
    return sorted(diagnostics)


def check(root: Path) -> list[str]:
    """Return all diagnostics for `root`. Empty list means clean."""
    return [*find_owner_violations(root), *find_duplicate_violations(root)]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Check that install.ps1 is the sole owner of the Windows stable "
            "executable path (current/apm.exe)."
        )
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Repository root to scan (defaults to the real repository root).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.root.resolve() if args.root is not None else REPO_ROOT
    violations = check(root)
    for diagnostic in violations:
        print(diagnostic)
    if violations:
        print(f"[x] {len(violations)} Windows stable path owner violation(s) found")
        return 1
    print("[+] Windows stable executable path owner check clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
