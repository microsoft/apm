#!/usr/bin/env python3
"""Create a hermetic acceptance world and print its environment.

A world is a fresh directory with an isolated HOME, APM_HOME and harness
homes. Every journey step runs inside one world, so no real user
configuration (~/.claude, ~/.copilot, ~/.codex, ~/.apm) is ever touched.

stdout: JSON {"root": ..., "project": ..., "env": {...}, "unset": [...]}.
With --shell, prints POSIX export/unset lines instead.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
import tempfile
from pathlib import Path

UNSET = (
    "GITHUB_TOKEN",
    "GITHUB_APM_PAT",
    "GH_TOKEN",
    "GITHUB_API_TOKEN",
    "ADO_APM_PAT",
    "GITLAB_TOKEN",
    "APM_NON_INTERACTIVE",
)


def build_world(parent: Path, name: str) -> dict:
    parent.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix=f"{name}-", dir=parent)).resolve()
    home = root / "home"
    for sub in ("home/.apm", "home/.config", "scratch", "project", "remotes", "logs"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    env = {
        "HOME": str(home),
        "APM_HOME": str(home / ".apm"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "CODEX_HOME": str(home / ".codex"),
        "COPILOT_HOME": str(home / ".copilot"),
        "TMPDIR": str(root / "scratch"),
        "GIT_CONFIG_GLOBAL": str(home / ".gitconfig"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "APM_E2E_TESTS": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    (home / ".gitconfig").write_text(
        "[user]\n\tname = Acceptance\n\temail = acceptance@example.invalid\n"
        "[init]\n\tdefaultBranch = main\n",
        encoding="ascii",
    )
    return {
        "root": str(root),
        "project": str(root / "project"),
        "logs": str(root / "logs"),
        "remotes": str(root / "remotes"),
        "env": env,
        "unset": list(UNSET),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a hermetic acceptance world.")
    parser.add_argument("--parent", required=True, help="directory that holds worlds")
    parser.add_argument("--name", default="world", help="world name prefix")
    parser.add_argument("--shell", action="store_true", help="print export lines")
    args = parser.parse_args(argv)
    world = build_world(Path(args.parent), args.name)
    if args.shell:
        for key in world["unset"]:
            print(f"unset {key}")
        for key, value in world["env"].items():
            print(f"export {key}={shlex.quote(value)}")
    else:
        print(json.dumps(world, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
