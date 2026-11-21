"""Hermetic Azure DevOps Services impersonation plus a logging credential helper.

``AdoOrgFixture`` answers ``dev.azure.com`` requests behind an Authorization
gate: Git smart HTTP (``/{org}/{project}/_git/{repo}``) through
``git http-backend`` and the Items REST API
(``/{org}/{project}/_apis/git/repositories/{repo}/items``). Unauthenticated
and rejected requests take one of the response shapes Azure DevOps uses for
"no accepted credential": a 302 to the ``*.vssps.visualstudio.com/_signin``
page, a 203 HTML sign-in page, or a 401.

``install_logging_credential_helper`` wires a real ``git credential`` helper
into the scenario's global Git config. It logs every request (fields and the
interactivity environment) and returns a fixed username and password, the
way Git Credential Manager answers for ``dev.azure.com`` with
``credential.useHttpPath``.
"""

from __future__ import annotations

import json
import shutil
import stat
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote

from tests.utils.local_scripted_https_proxy import (
    ScriptedRequest,
    ScriptedResponse,
    git_smart_http,
)

ADO_HOST = "dev.azure.com"
SIGNIN_HOST = "spsprodweu5.vssps.visualstudio.com"
SIGNIN_PAGE = (
    b"<!DOCTYPE html><html><head><title>Azure DevOps Services | Sign In</title>"
    b"</head><body>Sign in to your account</body></html>"
)

# Response shapes for a request that carries no accepted credential.
SIGNIN_REDIRECT = "signin-302"
SIGNIN_PAGE_203 = "signin-203"
UNAUTHORIZED_401 = "401"


@dataclass
class AdoOrgFixture:
    """Programmable ``dev.azure.com`` origin for one or more repositories."""

    accepted_authorizations: set[str] = field(default_factory=set)
    unauthenticated: str = SIGNIN_REDIRECT
    rejected: str = SIGNIN_PAGE_203
    git_unauthenticated: str = UNAUTHORIZED_401
    repositories: dict[tuple[str, str, str], Path] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def add_repository(self, org: str, project: str, repo: str, bare: Path) -> None:
        self.repositories[(org.lower(), project.lower(), repo.lower())] = bare

    def _repository(self, org: str, project: str, repo: str) -> Path | None:
        return self.repositories.get((org.lower(), project.lower(), repo.lower()))

    def _denied(self, request: ScriptedRequest, *, git: bool) -> ScriptedResponse:
        presented = request.header("Authorization") is not None
        shape = self.rejected if presented else self.unauthenticated
        if git:
            shape = self.git_unauthenticated if not presented else UNAUTHORIZED_401
        if shape == SIGNIN_REDIRECT:
            reply_to = quote(f"https://{ADO_HOST}{request.target}", safe="")
            return ScriptedResponse(
                302,
                {
                    "Location": (
                        f"https://{SIGNIN_HOST}/_signin?realm={ADO_HOST}"
                        f"&reply_to={reply_to}&redirect=1"
                    ),
                    "X-TFS-ProcessId": "00000000-0000-0000-0000-000000000000",
                },
            )
        if shape == SIGNIN_PAGE_203:
            return ScriptedResponse(203, {"Content-Type": "text/html; charset=utf-8"}, SIGNIN_PAGE)
        return ScriptedResponse(
            401,
            {
                "WWW-Authenticate": (
                    "Bearer authorization_uri=https://login.microsoftonline.com/common, "
                    'Basic realm="https://tfsprodweu5.app.visualstudio.com/"'
                ),
                "Content-Type": "text/plain",
            },
            b"TF400813: The user is not authorized to access this resource.",
        )

    def _authorized(self, request: ScriptedRequest) -> bool:
        return request.header("Authorization") in self.accepted_authorizations

    def handle(self, request: ScriptedRequest) -> ScriptedResponse:
        segments = [unquote(part) for part in request.path.strip("/").split("/")]
        if len(segments) >= 4 and segments[2] == "_git":
            org, project = segments[0], segments[1]
            repo = segments[3].removesuffix(".git")
            bare = self._repository(org, project, repo)
            if bare is None:
                return ScriptedResponse(404)
            if not self._authorized(request):
                return self._denied(request, git=True)
            rest = "/" + "/".join(segments[4:]) if len(segments) > 4 else ""
            return git_smart_http(bare, request, rest)
        if (
            len(segments) == 7
            and segments[2:5] == ["_apis", "git", "repositories"]
            and segments[6] == "items"
        ):
            if not self._authorized(request):
                return self._denied(request, git=False)
            bare = self._repository(segments[0], segments[1], segments[5])
            query = parse_qs(request.query)
            file_path = (query.get("path") or [""])[0].lstrip("/")
            ref = (query.get("versionDescriptor.version") or ["HEAD"])[0]
            blob = None
            if bare is not None and file_path:
                completed = subprocess.run(
                    (shutil.which("git") or "git", "--git-dir", str(bare), "cat-file", "blob",
                     f"{ref}:{file_path}"),
                    capture_output=True,
                    check=False,
                    timeout=30,
                )  # fmt: skip
                blob = completed.stdout if completed.returncode == 0 else None
            if blob is None:
                return ScriptedResponse(
                    404,
                    {"Content-Type": "application/json"},
                    json.dumps(
                        {"message": f"TF401174: The item '{file_path}' could not be found."}
                    ).encode(),
                )
            return ScriptedResponse(200, {"Content-Type": "application/octet-stream"}, blob)
        if not self._authorized(request):
            return self._denied(request, git=False)
        return ScriptedResponse(404)

    @staticmethod
    def signin(_request: ScriptedRequest) -> ScriptedResponse:
        return ScriptedResponse(200, {"Content-Type": "text/html; charset=utf-8"}, SIGNIN_PAGE)


_HELPER = r"""
import json
import os
import sys

action = sys.argv[1] if len(sys.argv) > 1 else ""
fields = {}
for line in sys.stdin.read().splitlines():
    if "=" in line:
        key, value = line.split("=", 1)
        fields[key] = value
event = {
    "action": action,
    "fields": fields,
    "env": {
        name: os.environ.get(name)
        for name in ("GCM_INTERACTIVE", "GIT_TERMINAL_PROMPT", "CI", "GCM_GUI_PROMPT")
    },
}
with open(os.environ.get("APM_TEST_HELPER_LOG") or LOG_PATH, "a", encoding="utf-8") as handle:
    handle.write(json.dumps(event, sort_keys=True) + "\n")
if action == "get" and RESPOND and fields.get("host", "").lower() in HOSTS:
    sys.stdout.write("username=" + USERNAME + "\npassword=" + PASSWORD + "\n")
"""


@dataclass(frozen=True)
class LoggingCredentialHelper:
    """A configured credential helper and its request log."""

    log_path: Path
    username: str
    password: str

    def events(self) -> tuple[dict[str, object], ...]:
        if not self.log_path.exists():
            return ()
        return tuple(
            json.loads(line)
            for line in self.log_path.read_text(encoding="utf-8").splitlines()
            if line
        )

    def gets(self) -> tuple[dict[str, object], ...]:
        return tuple(event for event in self.events() if event["action"] == "get")


def install_logging_credential_helper(
    git_config: Path,
    root: Path,
    *,
    username: str,
    password: str,
    hosts: tuple[str, ...] = (ADO_HOST,),
    respond: bool = True,
    use_http_path: bool = True,
) -> LoggingCredentialHelper:
    """Append a logging helper to *git_config* (the scenario's global config)."""
    root.mkdir(parents=True, exist_ok=True)
    log_path = root / "credential-helper.jsonl"
    script = root / "credential_helper.py"
    script.write_text(
        f"LOG_PATH = {str(log_path)!r}\n"
        f"RESPOND = {respond!r}\n"
        f"HOSTS = {tuple(h.lower() for h in hosts)!r}\n"
        f"USERNAME = {username!r}\n"
        f"PASSWORD = {password!r}\n" + _HELPER,
        encoding="ascii",
        newline="\n",
    )
    launcher = root / "credential-helper"
    launcher.write_text(
        f'#!/bin/sh\nexec {sys.executable!s} {str(script)!s} "$@"\n',
        encoding="ascii",
        newline="\n",
    )
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR)
    lines = ["[credential]", f"\thelper = {launcher}"]
    if use_http_path:
        for host in hosts:
            lines += [f'[credential "https://{host}"]', "\tuseHttpPath = true"]
    with git_config.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    return LoggingCredentialHelper(log_path=log_path, username=username, password=password)
