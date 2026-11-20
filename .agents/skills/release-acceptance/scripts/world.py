#!/usr/bin/env python3
"""Create a hermetic acceptance world and print its environment.

A world is a fresh directory with an isolated HOME, APM_HOME and harness
homes. Every journey step runs inside one world, so no real user
configuration (~/.claude, ~/.copilot, ~/.codex, ~/.apm) is ever touched,
and no host git configuration (signing, askpass, proxies, injected
config) leaks in.

Options:
  --apm-bin PATH    put this apm executable first on PATH as `apm`
  --git-host HOST   map https://HOST/<owner>/<repo>.git to bare repos under
                    <world>/remotes/<owner>/<repo>.git via insteadOf. Use a
                    non-GitHub host name (default git.example.invalid);
                    github.com is validated against its API and fails.

stdout: JSON {"root", "project", "logs", "remotes", "bin", "git_host",
"env", "unset", "unset_prefixes"}. With --shell, prints POSIX
export/unset lines instead.
"""

from __future__ import annotations

import argparse
import json
import os
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
    "GIT_ASKPASS",
    "SSH_ASKPASS",
    "GIT_SSH_COMMAND",
    "GIT_SSH",
    "GIT_PROXY_COMMAND",
    "GIT_CONFIG_SYSTEM",
    "GIT_CONFIG_PARAMETERS",
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "VIRTUAL_ENV",
)
UNSET_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_", "GIT_AUTHOR_", "GIT_COMMITTER_")


def build_world(parent: Path, name: str, apm_bin: str | None, git_host: str) -> dict:
    parent.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix=f"{name}-", dir=parent)).resolve()
    home = root / "home"
    for sub in ("home/.apm", "home/.config", "scratch", "project", "remotes", "logs", "bin"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    bin_dir = root / "bin"
    if apm_bin:
        target = Path(apm_bin).resolve()
        if not target.is_file():
            raise FileNotFoundError(f"--apm-bin {target} is not a file")
        (bin_dir / "apm").symlink_to(target)
    remotes = root / "remotes"
    (home / ".gitconfig").write_text(
        "[user]\n\tname = Acceptance\n\temail = acceptance@example.invalid\n"
        "[init]\n\tdefaultBranch = main\n"
        "[commit]\n\tgpgsign = false\n"
        "[tag]\n\tgpgsign = false\n"
        "[credential]\n\thelper =\n"
        f'[url "file://{remotes.as_posix()}/"]\n\tinsteadOf = https://{git_host}/\n',
        encoding="ascii",
    )
    env = {
        "HOME": str(home),
        "APM_HOME": str(home / ".apm"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "CODEX_HOME": str(home / ".codex"),
        "COPILOT_HOME": str(home / ".copilot"),
        "TMPDIR": str(root / "scratch"),
        "GIT_CONFIG_GLOBAL": str(home / ".gitconfig"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_COUNT": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ALLOW_PROTOCOL": "file:https",
        "APM_E2E_TESTS": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PATH": os.pathsep.join([str(bin_dir), os.environ.get("PATH", "")]),
    }
    return {
        "root": str(root),
        "project": str(root / "project"),
        "logs": str(root / "logs"),
        "remotes": str(remotes),
        "bin": str(bin_dir),
        "git_host": git_host,
        "env": env,
        "unset": list(UNSET),
        "unset_prefixes": list(UNSET_PREFIXES),
    }


def scrub_env(base: dict[str, str], world: dict) -> dict[str, str]:
    """Return base minus host leaks, plus the world's own environment."""
    unset = set(world.get("unset", []))
    prefixes = tuple(world.get("unset_prefixes", []))
    env = {k: v for k, v in base.items() if k not in unset and not k.startswith(prefixes)}
    env.update(world["env"])
    return env


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a hermetic acceptance world.")
    parser.add_argument("--parent", required=True, help="directory that holds worlds")
    parser.add_argument("--name", default="world", help="world name prefix")
    parser.add_argument("--apm-bin", help="apm executable to expose as `apm` on PATH")
    parser.add_argument(
        "--git-host",
        default="git.example.invalid",
        help="host name rewritten to local bare repos (default: git.example.invalid)",
    )
    parser.add_argument("--shell", action="store_true", help="print export lines")
    args = parser.parse_args(argv)
    try:
        world = build_world(Path(args.parent), args.name, args.apm_bin, args.git_host)
    except FileNotFoundError as exc:
        print(f"world: {exc}", file=sys.stderr)
        return 2
    if args.shell:
        for key in world["unset"]:
            print(f"unset {key}")
        for prefix in world["unset_prefixes"]:
            print(f"for v in $(env | sed -n 's/^\\({prefix}[^=]*\\)=.*/\\1/p'); do unset \"$v\"; done")
        for key, value in world["env"].items():
            print(f"export {key}={shlex.quote(value)}")
    else:
        print(json.dumps(world, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
