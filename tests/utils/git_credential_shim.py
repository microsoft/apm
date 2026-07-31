"""Cross-platform Git and gh shims for installed-binary auth tests."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

_GIT_SHIM = r"""#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import base64
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


def _config_entries():
    try:
        count = max(0, int(os.environ.get("GIT_CONFIG_COUNT", "0")))
    except ValueError:
        count = 0
    return [
        (
            os.environ.get(f"GIT_CONFIG_KEY_{index}", ""),
            os.environ.get(f"GIT_CONFIG_VALUE_{index}", ""),
        )
        for index in range(count)
    ]


def _append_event(event):
    path = Path(os.environ["APM_TEST_GIT_SHIM_LOG"])
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")


def _credential_request():
    body = sys.stdin.read()
    fields = {}
    for line in body.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            fields[key] = value
    entries = _config_entries()
    _append_event(
        {
            "event": "credential-fill",
            "host": fields.get("host"),
            "path": fields.get("path"),
            "protocol": fields.get("protocol"),
            "credential_interactive": [
                value for key, value in entries if key.lower() == "credential.interactive"
            ],
        }
    )
    token = os.environ["APM_TEST_GIT_CREDENTIAL"]
    sys.stdout.write(
        "protocol=https\n"
        f"host={fields.get('host', '')}\n"
        "username=x-access-token\n"
        f"password={token}\n\n"
    )
    return 0


def _rewrite_url(value):
    parsed = urlsplit(value)
    if parsed.hostname != "github.com":
        return value, None, None
    repository_path = parsed.path.lstrip("/").removesuffix(".git")
    mapping = json.loads(os.environ["APM_TEST_GIT_REMOTE_MAP"])
    target = mapping.get(repository_path)
    if target is None:
        return value, None, None
    local = urlsplit(target)
    rewritten = urlunsplit(
        (
            local.scheme,
            local.netloc,
            local.path,
            local.query,
            local.fragment,
        )
    )
    authorization = None
    if parsed.username is not None:
        raw = f"{parsed.username}:{parsed.password or ''}".encode("utf-8")
        authorization = "Authorization: Basic " + base64.b64encode(raw).decode("ascii")
    return rewritten, {
        "host": parsed.hostname,
        "path": repository_path,
        "authenticated_url": parsed.username is not None,
    }, authorization


def _strip_dumb_http_incompatible_options(args):
    stripped = []
    skip_next = False
    for value in args:
        if skip_next:
            skip_next = False
            continue
        if value in {"--depth", "--filter", "--shallow-since", "--shallow-exclude"}:
            skip_next = True
            continue
        if value.startswith(
            ("--depth=", "--filter=", "--shallow-since=", "--shallow-exclude=")
        ):
            continue
        stripped.append(value)
    return stripped


def main():
    args = sys.argv[1:]
    if args[:2] == ["credential", "fill"]:
        return _credential_request()

    rewritten_args = []
    remotes = []
    authorizations = []
    for value in args:
        rewritten, remote, authorization = _rewrite_url(value)
        rewritten_args.append(rewritten)
        if remote is not None:
            remotes.append(remote)
        if authorization is not None:
            authorizations.append(authorization)
    rewritten_args = _strip_dumb_http_incompatible_options(rewritten_args)
    for authorization in reversed(authorizations):
        rewritten_args[:0] = ["-c", f"http.extraHeader={authorization}"]

    entries = _config_entries()
    _append_event(
        {
            "event": "git",
            "command": args[0] if args else "",
            "remotes": remotes,
            "github_token_env": sorted(
                name
                for name in os.environ
                if name in {"GH_TOKEN", "GITHUB_APM_PAT", "GITHUB_TOKEN"}
                or name.startswith("GITHUB_APM_PAT_")
            ),
            "git_token_present": "GIT_TOKEN" in os.environ,
            "auth_config_present": any(
                (value and "extraheader" in key.lower())
                or value.strip().lower().startswith("authorization:")
                for key, value in entries
            ),
            "credential_helpers": [
                value for key, value in entries if key.lower() == "credential.helper"
            ],
            "credential_interactive": [
                value for key, value in entries if key.lower() == "credential.interactive"
            ],
        }
    )
    completed = subprocess.run(
        [os.environ["APM_TEST_REAL_GIT"], *rewritten_args],
        env=os.environ,
        check=False,
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
"""

_GH_SHIM = r"""#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

path = Path(os.environ["APM_TEST_GIT_SHIM_LOG"])
with path.open("a", encoding="utf-8", newline="\n") as handle:
    handle.write(
        json.dumps(
            {
                "event": "gh",
                "command": sys.argv[1:],
            },
            sort_keys=True,
        )
        + "\n"
    )
raise SystemExit(1)
"""


@dataclass(frozen=True)
class GitCredentialShim:
    """Paths and child environment for one credential-recording shim."""

    root: Path
    log_path: Path
    environment: dict[str, str]

    def events(self) -> tuple[dict[str, object], ...]:
        """Read all recorded shim events."""
        if not self.log_path.exists():
            return ()
        return tuple(
            json.loads(line)
            for line in self.log_path.read_text(encoding="utf-8").splitlines()
            if line
        )


class GitCredentialShimFactory:
    """Create a PATH-prepended Git/gh shim without replacing real Git."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def create(
        self,
        *,
        base_env: dict[str, str],
        real_git: Path,
        remote_map: dict[str, str],
        credential: str = "fixture-private-token",
    ) -> GitCredentialShim:
        """Write shims and return their complete subprocess environment."""
        self._root.mkdir(parents=True, exist_ok=False)
        log_path = self._root / "events.jsonl"
        git_script = self._root / "git-shim.py"
        gh_script = self._root / "gh-shim.py"
        git_script.write_text(_GIT_SHIM, encoding="ascii", newline="\n")
        gh_script.write_text(_GH_SHIM, encoding="ascii", newline="\n")
        git_script.chmod(git_script.stat().st_mode | stat.S_IXUSR)
        gh_script.chmod(gh_script.stat().st_mode | stat.S_IXUSR)

        if os.name == "nt":
            self._write_windows_launcher("git", git_script)
            self._write_windows_launcher("gh", gh_script)
        else:
            self._write_posix_launcher("git", git_script)
            self._write_posix_launcher("gh", gh_script)

        environment = dict(base_env)
        environment["PATH"] = os.pathsep.join((str(self._root), environment.get("PATH", "")))
        environment["APM_TEST_REAL_GIT"] = str(real_git)
        environment["APM_TEST_GIT_REMOTE_MAP"] = json.dumps(
            remote_map,
            sort_keys=True,
        )
        environment["APM_TEST_GIT_CREDENTIAL"] = credential
        environment["APM_TEST_GIT_SHIM_LOG"] = str(log_path)
        git_exec_path = subprocess.run(
            (str(real_git), "--exec-path"),
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
        if git_exec_path:
            environment["GIT_EXEC_PATH"] = git_exec_path
        return GitCredentialShim(
            root=self._root,
            log_path=log_path,
            environment=environment,
        )

    def _write_posix_launcher(self, name: str, script: Path) -> None:
        launcher = self._root / name
        launcher.write_text(
            f"#!{sys.executable}\n"
            "import runpy\n"
            f"runpy.run_path({str(script)!r}, run_name='__main__')\n",
            encoding="ascii",
            newline="\n",
        )
        launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR)

    def _write_windows_launcher(self, name: str, script: Path) -> None:
        launcher = self._root / f"{name}.cmd"
        launcher.write_text(
            f'@"{sys.executable}" "{script}" %*\r\n',
            encoding="ascii",
            newline="",
        )
