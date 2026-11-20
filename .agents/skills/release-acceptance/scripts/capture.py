#!/usr/bin/env python3
"""Run one journey step in a world and lint every output line.

Captures stdout, stderr and the exit code separately, writes them to
the world's log directory, applies the line lint, and extracts the
hints the CLI printed (commands to run, docs URLs) so the walker can
execute each one.

stdout: one JSON record (see FIELDS in the skill's report template).
The exit code of this script is always 0 unless usage is wrong (2);
the command's own exit code is in the record.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HINT_RE = re.compile(
    r"(?:`|')(apm [^`'\n]{1,200})(?:`|')|(?:^|[\s:])(apm\s+[a-z][a-z-]*(?:\s+[^\s].*)?)$"
)
URL_RE = re.compile(r"https://microsoft\.github\.io/apm/([A-Za-z0-9_./-]*)(#[A-Za-z0-9_-]+)?")
STALE_NAMES = ("python -m apm_cli", "apm_cli.cli", "apm manifest migrate")
TRACEBACK = ("Traceback (most recent call last)", 'File "/')
ERROR_MARK = "[x]"


def lint_lines(stdout: str, stderr: str, rc: int) -> list[dict]:
    """Return deterministic line-level problems for one command."""
    issues: list[dict] = []
    for stream_name, text in (("stdout", stdout), ("stderr", stderr)):
        seen: dict[str, int] = {}
        for lineno, line in enumerate(text.splitlines(), start=1):
            bad = [ch for ch in line if ord(ch) > 0x7E or (ord(ch) < 0x20 and ch != "\t")]
            if bad:
                codepoints = sorted({f"U+{ord(ch):04X}" for ch in bad})
                issues.append(
                    {
                        "rule": "non-ascii",
                        "stream": stream_name,
                        "line": lineno,
                        "detail": ",".join(codepoints),
                        "text": line.strip()[:160],
                    }
                )
            for stale in STALE_NAMES:
                if stale in line:
                    issues.append(
                        {
                            "rule": "stale-command-name",
                            "stream": stream_name,
                            "line": lineno,
                            "detail": stale,
                            "text": line.strip()[:160],
                        }
                    )
            for marker in TRACEBACK:
                if line.lstrip().startswith(marker):
                    issues.append(
                        {
                            "rule": "traceback",
                            "stream": stream_name,
                            "line": lineno,
                            "detail": marker,
                            "text": line.strip()[:160],
                        }
                    )
            key = line.strip()
            if len(key) >= 12 and not set(key) <= set("-=|+ "):
                if key in seen:
                    issues.append(
                        {
                            "rule": "duplicate-line",
                            "stream": stream_name,
                            "line": lineno,
                            "detail": f"first at line {seen[key]}",
                            "text": key[:160],
                        }
                    )
                else:
                    seen[key] = lineno
    if rc != 0 and not stderr.strip():
        issues.append(
            {
                "rule": "error-without-stderr",
                "stream": "stderr",
                "line": 0,
                "detail": f"exit {rc} with empty stderr",
                "text": "",
            }
        )
    if rc != 0 and any(line.lstrip().startswith(ERROR_MARK) for line in stdout.splitlines()):
        issues.append(
            {
                "rule": "error-on-stdout",
                "stream": "stdout",
                "line": 0,
                "detail": "an [x] line was printed to stdout",
                "text": "",
            }
        )
    return issues


def extract_hints(text: str, docs_root: Path | None) -> list[dict]:
    """Return commands and docs links the CLI told the user to use."""
    hints: list[dict] = []
    seen: set[str] = set()
    for line in text.splitlines():
        for match in HINT_RE.finditer(line.strip()):
            cmd = (match.group(1) or match.group(2) or "").strip().rstrip(".,;)")
            if cmd and cmd not in seen and "<" not in cmd:
                seen.add(cmd)
                hints.append({"kind": "command", "value": cmd, "line": line.strip()[:200]})
        for match in URL_RE.finditer(line):
            url = match.group(0)
            if url in seen:
                continue
            seen.add(url)
            hint = {"kind": "docs-url", "value": url, "line": line.strip()[:200]}
            if docs_root is not None:
                hint["resolves"] = docs_url_resolves(docs_root, match.group(1), match.group(2))
            hints.append(hint)
    return hints


def docs_url_resolves(docs_root: Path, slug: str, anchor: str | None) -> bool:
    slug = slug.strip("/")
    candidates = [
        docs_root / f"{slug}.md",
        docs_root / f"{slug}.mdx",
        docs_root / slug / "index.md",
        docs_root / slug / "index.mdx",
    ]
    page = next((p for p in candidates if p.is_file()), None)
    if page is None:
        return False
    if not anchor:
        return True
    wanted = anchor.lstrip("#").lower()
    for line in page.read_text(encoding="utf-8").splitlines():
        if line.startswith("#"):
            heading = line.lstrip("#").strip().lower()
            slugged = re.sub(r"[^a-z0-9 -]", "", heading).replace(" ", "-")
            if slugged == wanted:
                return True
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one step and lint its output.")
    parser.add_argument("--world", required=True, help="world JSON written by world.py")
    parser.add_argument("--step-id", required=True, help="journey.step id, e.g. J01.4")
    parser.add_argument("--cwd", help="working directory (default: the world project)")
    parser.add_argument("--docs-root", help="docs/src/content/docs to resolve links")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--stdin", default="", help="text fed to stdin")
    parser.add_argument("cmd", nargs=argparse.REMAINDER, help="-- command and args")
    args = parser.parse_args(argv)
    cmd = args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd
    if not cmd:
        parser.error("missing command after --")
    world = json.loads(Path(args.world).read_text(encoding="utf-8"))
    env = {k: v for k, v in os.environ.items() if k not in world["unset"]}
    env.update(world["env"])
    cwd = args.cwd or world["project"]
    try:
        # The command is the journey step the operator asked to run.
        proc = subprocess.run(  # noqa: S603
            cmd,
            cwd=cwd,
            env=env,
            input=args.stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=args.timeout,
            check=False,
        )
        rc, out, err = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        rc, out, err = 124, exc.stdout or "", (exc.stderr or "") + "\n[capture] timeout"
    logs = Path(world["logs"])
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", args.step_id)
    (logs / f"{safe}.stdout").write_text(out, encoding="utf-8")
    (logs / f"{safe}.stderr").write_text(err, encoding="utf-8")
    docs_root = Path(args.docs_root) if args.docs_root else None
    record = {
        "step": args.step_id,
        "cmd": cmd,
        "cwd": cwd,
        "rc": rc,
        "stdout_log": str(logs / f"{safe}.stdout"),
        "stderr_log": str(logs / f"{safe}.stderr"),
        "lint": lint_lines(out, err, rc),
        "hints": extract_hints(out + "\n" + err, docs_root),
    }
    print(json.dumps(record, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
