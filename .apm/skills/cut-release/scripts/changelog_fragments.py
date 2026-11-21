#!/usr/bin/env python3
"""CHANGELOG fragments: check, render and assemble ``changelog.d/<pr>.<section>.md``.

Each user-visible PR adds one fragment instead of editing ``## [Unreleased]``
in CHANGELOG.md, so concurrent PRs never touch the same lines. A fragment
holds exactly one bullet line in the format of
``.github/instructions/changelog.instructions.md``. ``cut-release`` assembles
the fragments, plus any legacy ``[Unreleased]`` bullets, into the version
block and deletes them.

Commands (run from the repository root, or pass ``--root``):

- ``check``: validate every fragment.
- ``render [--version X.Y.Z --date YYYY-MM-DD]``: print the release block
  (legacy ``[Unreleased]`` bullets first, then fragments by PR number).
- ``assemble --version X.Y.Z --date YYYY-MM-DD``: write that block into
  CHANGELOG.md under an empty ``## [Unreleased]`` and delete the fragments.
- ``check-unreleased --base-ref REF``: fail when CHANGELOG.md adds a net-new
  bullet under ``## [Unreleased]`` compared with the merge base with REF.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

FRAGMENT_DIR = "changelog.d"
CHANGELOG = "CHANGELOG.md"
UNRELEASED = "## [Unreleased]"
HIGHLIGHTS = "Highlights"
# Release order. PRs never write Highlights; cut-release adds them at release.
SECTIONS = (
    "Breaking changes and upgrade notes",
    "Added",
    "Changed",
    "Deprecated",
    "Removed",
    "Fixed",
    "Security",
)
SECTION_SLUGS: Mapping[str, str] = {
    section.lower().replace(" ", "-"): section for section in SECTIONS
}
NON_FRAGMENT_FILES = frozenset({"README.md", ".gitkeep"})

_FRAGMENT_NAME = re.compile(r"(?P<pr>[1-9][0-9]*)\.(?P<slug>[a-z-]+)\.md")
_TRAILING_REFS = re.compile(r"\(([^()]*)\)$")
_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+.]?[0-9A-Za-z.]+)?")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


class ChangelogError(ValueError):
    """The fragments or CHANGELOG.md cannot form a release block."""


@dataclass(frozen=True)
class Fragment:
    """One fragment file: the PR, its section and its single bullet line."""

    pr: int
    section: str
    line: str


def fragment_problems(name: str, text: str) -> list[str]:
    """Return every rule ``changelog.d/<name>`` with content ``text`` breaks."""
    match = _FRAGMENT_NAME.fullmatch(name)
    if match is None:
        return [f"name must be <pr>.<section>.md, with section one of: {', '.join(SECTION_SLUGS)}"]
    problems = []
    if match["slug"] not in SECTION_SLUGS:
        problems.append(
            f"unknown section '{match['slug']}'; use one of: {', '.join(SECTION_SLUGS)}"
        )
    if any(not (char == "\n" or " " <= char <= "~") for char in text):
        problems.append("content must be printable ASCII")
    body = text.removesuffix("\n")
    if not body or "\n" in body or "\r" in body:
        return [*problems, "content must be exactly one bullet line"]
    if not body.startswith("- ") or body != body.strip() or body[2:3] == " ":
        problems.append("the line must be a single '- ' bullet without extra whitespace")
    refs = _TRAILING_REFS.search(body)
    numbers = re.findall(r"#([0-9]+)", refs.group(1)) if refs else []
    if not numbers or numbers[-1] != match["pr"]:
        problems.append(f"the line must end with the PR reference, as in '(#{match['pr']})'")
    return problems


def read_fragments(root: Path) -> list[Fragment]:
    """Read every fragment, failing with all problems found."""
    directory = root / FRAGMENT_DIR
    if not directory.is_dir():
        return []
    fragments: list[Fragment] = []
    problems: list[str] = []
    for path in sorted(directory.iterdir()):
        if path.name in NON_FRAGMENT_FILES:
            continue
        relative = f"{FRAGMENT_DIR}/{path.name}"
        if not path.is_file():
            problems.append(f"{relative}: fragments are files, not directories")
            continue
        text = path.read_bytes().decode("ascii", errors="replace")
        found = fragment_problems(path.name, text)
        if found:
            problems.extend(f"{relative}: {problem}" for problem in found)
            continue
        pr, slug = path.name.split(".")[:2]
        fragments.append(Fragment(int(pr), SECTION_SLUGS[slug], text.removesuffix("\n")))
    if problems:
        raise ChangelogError("\n".join(problems))
    return sorted(fragments, key=lambda fragment: (fragment.pr, fragment.section))


def _unreleased_span(lines: Sequence[str]) -> tuple[int, int] | None:
    """Return ``(heading index, next version heading index)`` of ``[Unreleased]``."""
    for index, line in enumerate(lines):
        if line.rstrip() == UNRELEASED:
            end = next(
                (j for j in range(index + 1, len(lines)) if lines[j].startswith("## ")),
                len(lines),
            )
            return index, end
    return None


def unreleased_bullets(changelog: str) -> list[str]:
    """Return the bullet lines under ``## [Unreleased]`` (no validation)."""
    lines = changelog.splitlines()
    span = _unreleased_span(lines)
    if span is None:
        return []
    return [line.rstrip() for line in lines[span[0] + 1 : span[1]] if line.startswith("- ")]


def legacy_sections(changelog: str) -> dict[str, list[str]]:
    """Parse ``## [Unreleased]`` into ``{section: bullet lines}``, strictly."""
    lines = changelog.splitlines()
    span = _unreleased_span(lines)
    if span is None:
        raise ChangelogError(f"{CHANGELOG} has no '{UNRELEASED}' heading")
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in lines[span[0] + 1 : span[1]]:
        if not line.strip():
            continue
        if line.startswith("### "):
            current = line[4:].strip()
            if current not in (HIGHLIGHTS, *SECTIONS):
                raise ChangelogError(f"{CHANGELOG} [Unreleased]: unknown section '{current}'")
            sections.setdefault(current, [])
        elif line.startswith("- ") and current is not None:
            sections[current].append(line.rstrip())
        else:
            raise ChangelogError(f"{CHANGELOG} [Unreleased]: unexpected line: {line}")
    return sections


def merge_sections(
    legacy: Mapping[str, Sequence[str]], fragments: Sequence[Fragment]
) -> dict[str, list[str]]:
    """Legacy bullets first, then fragments by PR, in release section order."""
    merged: dict[str, list[str]] = {}
    for section in (HIGHLIGHTS, *SECTIONS):
        lines = [*legacy.get(section, ()), *(f.line for f in fragments if f.section == section)]
        if lines:
            merged[section] = lines
    return merged


def render_sections(sections: Mapping[str, Sequence[str]]) -> str:
    """Render ``### <Section>`` blocks in cut-release format."""
    blocks = [f"### {section}\n\n" + "\n".join(lines) + "\n" for section, lines in sections.items()]
    return "\n".join(blocks)


def release_block(root: Path, version: str | None = None, date: str | None = None) -> str:
    """Render the release block from legacy bullets and fragments."""
    changelog = (root / CHANGELOG).read_text(encoding="utf-8")
    sections = merge_sections(legacy_sections(changelog), read_fragments(root))
    if not sections:
        raise ChangelogError("nothing to release: no fragments and no [Unreleased] bullets")
    body = render_sections(sections)
    if version is None:
        return body
    _require_version(changelog, version, date)
    return f"## [{version}] - {date}\n\n{body}"


def assemble(root: Path, version: str, date: str) -> list[Path]:
    """Write the version block into CHANGELOG.md and delete the fragments."""
    block = release_block(root, version, date)
    path = root / CHANGELOG
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    span = _unreleased_span(lines)
    if span is None:
        raise ChangelogError(f"{CHANGELOG} has no '{UNRELEASED}' heading")
    tail = lines[span[1] :]
    updated = [*lines[: span[0] + 1], "\n", block, *(["\n", *tail] if tail else [])]
    path.write_text("".join(updated), encoding="utf-8")
    removed = [
        path
        for path in sorted((root / FRAGMENT_DIR).glob("*.md"))
        if path.name not in NON_FRAGMENT_FILES
    ]
    for fragment in removed:
        fragment.unlink()
    return removed


def _require_version(changelog: str, version: str, date: str | None) -> None:
    if _VERSION.fullmatch(version) is None:
        raise ChangelogError(f"version must look like X.Y.Z, got '{version}'")
    if date is None or _DATE.fullmatch(date) is None:
        raise ChangelogError(f"date must be YYYY-MM-DD, got '{date}'")
    if f"## [{version}]" in changelog:
        raise ChangelogError(f"{CHANGELOG} already has a '## [{version}]' block")


def new_unreleased_bullets(base: str, head: str) -> list[str]:
    """Return the bullets ``head`` adds under ``[Unreleased]``, net of removals.

    Editing or rewording an existing bullet (one removed, one added) passes;
    so does moving bullets out at release. Only a net-new bullet is reported.
    """
    before, after = Counter(unreleased_bullets(base)), Counter(unreleased_bullets(head))
    added = list((after - before).elements())
    removed = sum((before - after).values())
    return added if len(added) > removed else []


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    executable = shutil.which("git")
    if executable is None:
        raise ChangelogError("git is required")
    return subprocess.run(  # noqa: S603 - resolved executable, argument vector, no shell
        [executable, *args], cwd=root, capture_output=True, text=True, check=False
    )


def check_unreleased(root: Path, base_ref: str) -> list[str]:
    """Return net-new ``[Unreleased]`` bullets in the working tree vs the merge base."""
    merge_base = _git(root, "merge-base", base_ref, "HEAD")
    # A shallow CI checkout of a PR merge commit has no merge base; its first
    # parent is the base ref itself.
    base = merge_base.stdout.strip() if merge_base.returncode == 0 else base_ref
    shown = _git(root, "show", f"{base}:{CHANGELOG}")
    if shown.returncode != 0:
        raise ChangelogError(f"cannot read {CHANGELOG} at {base}: {shown.stderr.strip()}")
    head = (root / CHANGELOG).read_text(encoding="utf-8")
    return new_unreleased_bullets(shown.stdout, head)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="repository root")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="validate every fragment")
    render = commands.add_parser("render", help="print the release block (dry run)")
    render.add_argument("--version")
    render.add_argument("--date")
    assemble_cmd = commands.add_parser("assemble", help="write the version block")
    assemble_cmd.add_argument("--version", required=True)
    assemble_cmd.add_argument("--date", required=True)
    unreleased = commands.add_parser("check-unreleased", help="reject new [Unreleased] bullets")
    unreleased.add_argument("--base-ref", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command; print ``[x]`` diagnostics and return 1 on failure."""
    args = _parser().parse_args(argv)
    root: Path = args.root
    try:
        if args.command == "check":
            count = len(read_fragments(root))
            print(f"[+] {count} changelog fragment(s) valid")
        elif args.command == "render":
            if (args.version is None) != (args.date is None):
                raise ChangelogError("pass --version and --date together")
            sys.stdout.write(release_block(root, args.version, args.date))
        elif args.command == "assemble":
            removed = assemble(root, args.version, args.date)
            print(f"[+] wrote [{args.version}] to {CHANGELOG}; removed {len(removed)} fragment(s)")
        else:
            added = check_unreleased(root, args.base_ref)
            if added:
                print(f"[x] new bullets under {UNRELEASED} in {CHANGELOG}:")
                for line in added:
                    print(f"    {line}")
                print(
                    f"[i] Move each one to {FRAGMENT_DIR}/<pr>.<section>.md "
                    f"(see {FRAGMENT_DIR}/README.md); editing an existing bullet is fine."
                )
                return 1
            print(f"[+] no new bullets under {UNRELEASED}")
    except ChangelogError as exc:
        for line in str(exc).splitlines():
            print(f"[x] {line}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
