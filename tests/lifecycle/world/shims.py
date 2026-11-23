"""OS-aware stub executables on the lifecycle world's PATH (#4094).

A shim is a Python script plus a launcher: ``/bin/sh`` on Linux and macOS, a
``.cmd`` file on Windows (``shutil.which`` and ``CreateProcess`` find it through
PATHEXT). The script runs under the test interpreter, so a shim behaves the
same on every OS. Shims keep their state under the world root they run in
(``WORLD_ROOT_ENV``), so each clone records its own calls.

- ``claude`` / ``codex``: traps. APM must never run a native CLI; a call
  writes ``<world root>/<name>-was-run`` and exits 91.
- ``az``: the Azure CLI of the world. The runner's real ``az`` would send
  telemetry through the world proxy. The stub reports "not logged in" unless
  the world holds a token (``AZURE_CONFIG_DIR/lifecycle-access-token.json``)
  and appends every call to ``AZURE_CONFIG_DIR/lifecycle-az-calls.log``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

WORLD_ROOT_ENV = "APM_LIFECYCLE_WORLD_ROOT"
TRAP_EXIT = 91
TRAPPED = ("claude", "codex")
AZ_TOKEN_FILE = "lifecycle-access-token.json"
AZ_CALL_LOG = "lifecycle-az-calls.log"

_TRAP_SCRIPT = """\
import os
import sys
from pathlib import Path

root = Path(os.environ.get({env!r}) or {fallback!r})
marker = root / ({name!r} + "-was-run")
with marker.open("a", encoding="utf-8") as handle:
    handle.write(" ".join(sys.argv[1:]) + "\\n")
sys.exit({code})
"""

_AZ_SCRIPT = """\
import json
import os
import sys
from pathlib import Path

config = Path(os.environ.get("AZURE_CONFIG_DIR") or {fallback!r})
config.mkdir(parents=True, exist_ok=True)
args = sys.argv[1:]
with (config / {log!r}).open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(args) + "\\n")
token = config / {token!r}
if token.is_file() and args[:2] == ["account", "get-access-token"]:
    sys.stdout.write(token.read_text(encoding="utf-8"))
    sys.exit(0)
if token.is_file() and args[:2] == ["account", "show"]:
    sys.stdout.write(json.loads(token.read_text(encoding="utf-8"))["tenant"] + "\\n")
    sys.exit(0)
sys.stderr.write("ERROR: Please run 'az login' to setup account.\\n")
sys.exit(1)
"""


def _launcher(directory: Path, name: str, script: Path) -> Path:
    if os.name == "nt":
        launcher = directory / f"{name}.cmd"
        launcher.write_text(
            f'@"{sys.executable}" "{script}" %*\r\n@exit /b %ERRORLEVEL%\r\n', encoding="ascii"
        )
        return launcher
    launcher = directory / name
    launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="ascii")
    launcher.chmod(0o755)
    return launcher


def write_shim(directory: Path, name: str, source: str) -> Path:
    """Install shim *name* running Python *source*; return its launcher."""
    scripts = directory / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    script = scripts / f"{name}.py"
    script.write_text(source, encoding="ascii")
    return _launcher(directory, name, script)


def install_shims(root: Path, azure_config: Path) -> Path:
    """Write the world's shims under ``root/executables``; return that directory."""
    directory = root / "executables"
    directory.mkdir()
    for name in TRAPPED:
        source = _TRAP_SCRIPT.format(
            env=WORLD_ROOT_ENV, fallback=str(root), name=name, code=TRAP_EXIT
        )
        write_shim(directory, name, source)
    write_shim(
        directory,
        "az",
        _AZ_SCRIPT.format(fallback=str(azure_config), log=AZ_CALL_LOG, token=AZ_TOKEN_FILE),
    )
    return directory


def shim_environment(
    env: dict[str, str], root: Path, azure_config: Path, executables: Path
) -> dict[str, str]:
    """Put the shims first on PATH and keep Azure CLI state inside the world."""
    return {
        **env,
        WORLD_ROOT_ENV: str(root),
        "AZURE_CONFIG_DIR": str(azure_config),
        "AZURE_CORE_COLLECT_TELEMETRY": "no",
        "AZURE_CORE_ONLY_SHOW_ERRORS": "yes",
        "PATH": os.pathsep.join((str(executables), env.get("PATH", ""))),
    }
