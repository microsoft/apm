"""Strict host credentials keep GitHub-class env tokens off other hosts (#4324).

Real ``gh`` and ``git credential fill`` children run against stubs that
record every inherited variable name and, like real gh, hand back
``GH_TOKEN``/``GITHUB_TOKEN`` when present.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from apm_cli.core import auth as auth_mod
from apm_cli.core.auth import AuthResolver
from apm_cli.utils import git_env

pytestmark = [
    pytest.mark.integration,
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
    monkeypatch.setattr(git_env, "_gh_executable", None)
    monkeypatch.setattr(auth_mod, "_strict_warnings_emitted", set(), raising=False)
    for name in sorted(_SCRUBBED):
        monkeypatch.setenv(name, _ENV_TOKEN)
    monkeypatch.setenv("APM_STRICT_HOST_CREDENTIALS", "true")
    return records


def _names(record: Path) -> set[str]:
    return set(record.read_text(encoding="ascii").split()) if record.exists() else set()


def test_strict_mode_scrubs_env_tokens_from_gh_and_credential_fill(
    stubbed_children: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    context = AuthResolver().resolve("acme.ghe.com", org="acme")

    assert context.token == _HELPER_TOKEN
    assert context.source == "git-credential-fill"
    gh_names = _names(stubbed_children["gh"])
    helper_names = _names(stubbed_children["helper"])
    assert gh_names, "gh auth token child never ran"
    assert helper_names, "git credential helper child never ran"
    assert not gh_names & _SCRUBBED
    assert not helper_names & _SCRUBBED

    err = capsys.readouterr()
    warning = err.out + err.err
    assert "acme.ghe.com" in warning
    assert "APM_STRICT_HOST_CREDENTIALS" in warning
    assert _ENV_TOKEN not in warning


def test_strict_mode_keeps_the_env_token_for_the_configured_host(
    stubbed_children: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    resolver = AuthResolver()
    assert resolver.resolve("github.com").token == _ENV_TOKEN

    monkeypatch.setenv("GITHUB_HOST", "acme.ghe.com")
    configured = AuthResolver().resolve("acme.ghe.com", org="acme")
    assert (configured.token, configured.source) == (_ENV_TOKEN, "GITHUB_APM_PAT_ACME")


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
    capsys: pytest.CaptureFixture[str],
    value: str | None,
    strict: bool,
    warned: bool,
) -> None:
    if value is None:
        monkeypatch.delenv("APM_STRICT_HOST_CREDENTIALS")
    else:
        monkeypatch.setenv("APM_STRICT_HOST_CREDENTIALS", value)

    context = AuthResolver().resolve("acme.ghe.com")

    assert (context.token != _ENV_TOKEN) is strict
    captured = capsys.readouterr()
    assert ("is not recognised" in captured.out + captured.err) is warned
