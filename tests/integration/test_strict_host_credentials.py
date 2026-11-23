"""Strict host credentials keep GitHub-class env tokens off other hosts (#4324).

Each case resolves in a fresh Python process (real AuthResolver, real
``gh`` and ``git credential fill`` children) against stubs that record every
inherited variable name and, like real gh, hand back ``GH_TOKEN`` or
``GITHUB_TOKEN`` when present.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise("req-sc-005"),
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell stubs"),
]

_ENV_TOKEN = "ghp_" + "E" * 36
_HELPER_TOKEN = "inert-helper-credential-for-acme"
_SCRUBBED = {
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
    "GITHUB_APM_PAT",
    "GITHUB_APM_PAT_ACME",
}


def _script(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body, encoding="ascii")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture
def stubbed_children(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    for name in list(os.environ):
        if name.startswith(("GIT_", "GCM_", "GITHUB_", "GH_", "APM_")):
            monkeypatch.delenv(name, raising=False)
    records = {"gh": tmp_path / "gh-env.txt", "helper": tmp_path / "helper-env.txt"}
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _script(
        bin_dir / "gh",
        f'env | cut -d= -f1 >> "{records["gh"]}"\n'
        'if [ -n "$GH_TOKEN" ]; then echo "$GH_TOKEN"; exit 0; fi\n'
        'if [ -n "$GITHUB_TOKEN" ]; then echo "$GITHUB_TOKEN"; exit 0; fi\n'
        "exit 1\n",
    )
    helper = _script(
        tmp_path / "helper.sh",
        f'env | cut -d= -f1 >> "{records["helper"]}"\n'
        'if [ "$1" = get ]; then\n'
        "  cat >/dev/null\n"
        '  printf "username=x-access-token\\n"\n'
        '  if [ -n "$GH_TOKEN" ]; then printf "password=%s\\n" "$GH_TOKEN";\n'
        f'  else printf "password={_HELPER_TOKEN}\\n"; fi\n'
        "fi\n",
    )
    git_config = tmp_path / "gitconfig"
    git_config.write_text(f"[credential]\n\thelper = !{helper}\n", encoding="ascii")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(git_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    for name in sorted(_SCRUBBED):
        monkeypatch.setenv(name, _ENV_TOKEN)
    monkeypatch.setenv("APM_STRICT_HOST_CREDENTIALS", "true")
    return records


_RESOLVE = (
    "import json, sys\n"
    "from apm_cli.core.auth import AuthResolver\n"
    "resolver = AuthResolver()\n"
    "ctx = resolver.resolve(sys.argv[1], org=sys.argv[2] or None)\n"
    "tried = resolver.describe_credential_tried(sys.argv[1], sys.argv[2] or None)\n"
    "print(json.dumps({'token': ctx.token, 'source': ctx.source, 'tried': tried}))\n"
)


def _resolve(host: str, org: str = "") -> subprocess.CompletedProcess[str]:
    """Resolve *host* in a fresh process; the last stdout line is the context.

    The child runs in the fixture's HOME: a cwd inherited from an earlier
    in-process test may no longer exist, and gh lookup resolves the cwd.
    """
    return subprocess.run(
        [sys.executable, "-c", _RESOLVE, host, org],
        cwd=os.environ["HOME"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )


def _context(stdout: str) -> tuple[str | None, str]:
    payload = json.loads(stdout.strip().splitlines()[-1])
    return payload["token"], payload["source"]


def _names(record: Path) -> set[str]:
    return set(record.read_text(encoding="ascii").split()) if record.exists() else set()


def test_strict_mode_scrubs_env_tokens_from_gh_and_credential_fill(
    stubbed_children: dict[str, Path],
) -> None:
    result = _resolve("acme.ghe.com", "acme")
    assert result.returncode == 0, result.stderr
    token, source = _context(result.stdout)
    warning = result.stdout + result.stderr

    assert token == _HELPER_TOKEN
    assert source == "git-credential-fill"
    gh_names = _names(stubbed_children["gh"])
    helper_names = _names(stubbed_children["helper"])
    assert gh_names, "gh auth token child never ran"
    assert helper_names, "git credential helper child never ran"
    assert not gh_names & _SCRUBBED
    assert not helper_names & _SCRUBBED

    assert "withheld the environment token from acme.ghe.com;" in warning
    assert "gh auth login --hostname acme.ghe.com" in warning
    assert "(APM_STRICT_HOST_CREDENTIALS)" in warning
    # A helper credential that the host then rejects still repeats the notice.
    tried = json.loads(result.stdout.strip().splitlines()[-1])["tried"]
    assert tried.startswith("credential tried: git-credential-fill. "), tried
    assert "withheld the environment token from acme.ghe.com;" in tried
    assert _ENV_TOKEN not in warning


def test_strict_mode_keeps_the_env_token_for_the_configured_host(
    stubbed_children: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    default_host = _resolve("github.com")
    assert _context(default_host.stdout) == (_ENV_TOKEN, "GITHUB_APM_PAT")

    monkeypatch.setenv("GITHUB_HOST", "acme.ghe.com")
    configured = _resolve("acme.ghe.com", "acme")
    assert _context(configured.stdout) == (_ENV_TOKEN, "GITHUB_APM_PAT_ACME")
    assert "withheld" not in configured.stdout + configured.stderr


@pytest.mark.parametrize(
    ("value", "strict", "warned"),
    [
        (None, False, False),
        ("", False, False),
        ("0", False, False),
        ("Off", False, False),
        ("no", False, False),
        ("false", False, False),
        ("1", True, False),
        ("TRUE", True, False),
        ("yes", True, False),
        ("on", True, False),
        ("enabled", True, True),
    ],
)
def test_strict_setting_values(
    stubbed_children: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    value: str | None,
    strict: bool,
    warned: bool,
) -> None:
    if value is None:
        monkeypatch.delenv("APM_STRICT_HOST_CREDENTIALS")
    else:
        monkeypatch.setenv("APM_STRICT_HOST_CREDENTIALS", value)

    result = _resolve("acme.ghe.com")
    token, _source = _context(result.stdout)

    assert (token != _ENV_TOKEN) is strict
    assert ("is not recognised" in result.stdout + result.stderr) is warned
