"""Journey transcript normalizers N1-N7 and the golden file format (#4110).

``test_journeys.py`` and ``regen_goldens.py`` share these helpers, so a golden
is written and compared through the same code.

- N1 ``JOURNEY_ENV`` and ``strip_ansi``; N6 is its ``COLUMNS`` (no soft wrap).
- N2-N4 ``normalize_text`` (hex ids, durations, dates, version, paths).
- N5 ``canonical`` sorts each run of package blocks, so blocks compare as a
  multiset while the lines inside one block stay ordered.
- N7 ``golden_problems`` is the refusal rule of the regen script (O3, O4).
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path

GOLDEN_SCHEMA = 1
GOLDEN_HEADER = f"# apm journey golden v{GOLDEN_SCHEMA}"
GOLDENS = Path(__file__).parent / "goldens"

# N1 / N6: the environment every journey command runs with. N6 asks for no soft
# wrap; COLUMNS=200 still wraps the longest diagnostics, so the journeys keep
# the lifecycle world's 400.
JOURNEY_ENV: Mapping[str, str] = {
    "NO_COLOR": "1",
    "TERM": "dumb",
    "COLUMNS": "400",
    "LANG": "C.UTF-8",
    "APM_E2E_TESTS": "1",
}

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_SUBSTITUTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    # N3: the version line carries the build commit.
    (re.compile(r"version \d+\.\d+\.\d+\S*(?: \([0-9a-f]{7,40}\))?"), "version <VER>"),
    # N2: hex ids.
    (re.compile(r"sha256:[0-9a-f]{64}"), "sha256:<H>"),
    (re.compile(r"sha256:[0-9a-f]{8,63}(?=\.\.\.)"), "sha256:<H>"),
    (re.compile(r"\b[0-9a-f]{40}\b"), "<SHA40>"),
    (re.compile(r"@[0-9a-f]{8}\b"), "@<SHA8>"),
    (re.compile(r"=[0-9a-f]{12}(?=\.\.\.)"), "=<SHA12>"),
    # N3: durations and dates.
    (re.compile(r"\b(in|after) \d+(?:\.\d+)?s\b"), r"\1 <DUR>"),
    (
        re.compile(
            r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?"
        ),
        "<DATE>",
    ),
)
# The engine driver's usage line names the module; the binary names itself.
_ENTRY_POINT = re.compile(r"(?:\S*python\S*|\S*/python3?) -m apm_cli\.cli")
_BLOCK_HEAD = re.compile(r"^\s*\[\+\] \S")
_BLOCK_CHILD = re.compile(r"^\s*\|--")
_RULE = re.compile(r"^[\s+\-|=_.:]*$")
_ADJACENT_TOKEN = re.compile(r"\b([A-Za-z][A-Za-z'-]*) \1\b")


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def normalize_text(text: str, paths: Mapping[str, Path | str]) -> str:
    """Apply N1-N4: strip ANSI, mask paths (longest first), hex ids, durations, dates."""
    text = strip_ansi(text).replace("\r\n", "\n")
    for placeholder, path in sorted(paths.items(), key=lambda item: -len(str(item[1]))):
        raw = str(path)
        for form in {raw, raw.replace("\\", "/")}:
            text = text.replace(form, placeholder)
    text = _ENTRY_POINT.sub("apm", text)
    for pattern, replacement in _SUBSTITUTIONS:
        text = pattern.sub(replacement, text)
    return "\n".join(line.rstrip() for line in text.split("\n"))


def is_block_line(line: str) -> bool:
    return bool(_BLOCK_HEAD.match(line) or _BLOCK_CHILD.match(line))


def package_blocks(lines: Iterable[str]) -> list[list[str]]:
    """N5: every ``[+]`` header line with its ``|--`` children."""
    blocks: list[list[str]] = []
    for line in lines:
        if _BLOCK_HEAD.match(line):
            blocks.append([line])
        elif _BLOCK_CHILD.match(line) and blocks:
            blocks[-1].append(line)
    return [block for block in blocks if len(block) > 1]


def canonical(lines: list[str]) -> list[str]:
    """N5: sort each run of consecutive package blocks; other lines keep their order."""
    out: list[str] = []
    index = 0
    while index < len(lines):
        run: list[list[str]] = []
        while index < len(lines) and _BLOCK_HEAD.match(lines[index]):
            block = [lines[index]]
            index += 1
            while index < len(lines) and _BLOCK_CHILD.match(lines[index]):
                block.append(lines[index])
                index += 1
            run.append(block)
        if run:
            for block in sorted(run):
                out.extend(block)
            continue
        out.append(lines[index])
        index += 1
    return out


def render_command(display: str, exit_code: int, stdout: str, stderr: str) -> list[str]:
    """One command of a step transcript (already normalized streams)."""
    lines = [f"$ {display}", f"[exit {exit_code}]", "--- stdout"]
    lines += canonical(stdout.strip("\n").split("\n")) if stdout.strip() else []
    lines.append("--- stderr")
    lines += canonical(stderr.strip("\n").split("\n")) if stderr.strip() else []
    return lines


def render_golden(journey: str, step: int, commands: list[list[str]]) -> str:
    body = [f"{GOLDEN_HEADER}: {journey} step {step}"]
    for command in commands:
        body.extend(command)
    return "\n".join(body) + "\n"


def golden_path(journey: str, step: int, root: Path = GOLDENS) -> Path:
    return root / journey / f"{step:02d}.txt"


def duplicate_lines(lines: Iterable[str], allow: Iterable[str] = ()) -> list[str]:
    """O4 (i): non-blank lines printed twice; block children and table rules are per-item."""
    allowed = set(allow)
    counted = Counter(
        line.strip()
        for line in lines
        if line.strip() and not _BLOCK_CHILD.match(line) and not _RULE.match(line)
    )
    return sorted(line for line, count in counted.items() if count > 1 and line not in allowed)


def adjacent_tokens(lines: Iterable[str]) -> list[str]:
    """O4 (iii): a word repeated back to back ("the the")."""
    return sorted({m.group(0) for line in lines for m in _ADJACENT_TOKEN.finditer(line)})


def golden_problems(text: str) -> list[str]:
    """N7: a golden must keep O3 (ASCII) and O4 (no duplicate line within one command)."""
    problems = []
    if not text.isascii():
        bad = sorted({hex(ord(char)) for char in text if ord(char) > 0x7E})[:5]
        problems.append(f"non-ASCII characters {bad}")
    command: list[str] = []
    for line in [*text.split("\n"), "$ <end>"]:
        if line.startswith("$ "):
            problems.extend(f"duplicate line {dup!r}" for dup in duplicate_lines(command))
            command = []
        elif not line.startswith(("--- ", "[exit ", "#")):
            command.append(line)
    return problems
