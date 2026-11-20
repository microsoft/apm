"""APMLifecycle: credential values render masked on every output path (#4171).

PROMISE: credential-value-rendering -- a stored registry token, a credential
carried in a registry or proxy URL, and token environment variables never
reach command output or exported artifacts; presence is still reported
(source: .apm/architecture/owners/transport-auth-platform.json).
CELL: OP=config-set|config-get|config-list|config|doctor|lock-export|install SCOPE=user
NET=none
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.integration,
    pytest.mark.promise("P-OWN-credential-value-rendering", "P-LAW-auth.credentials_isolated"),
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
]

_TOKEN = "S12credSentinel9f3a7c2e81d4b6"
_MASK = "******** (set)"
_REGISTRY_URL = f"https://alice:{_TOKEN}@reg.example.invalid/apm"
_LOCKFILE = f"""\
lockfile_version: "2"
generated_at: "2024-01-01T00:00:00+00:00"
dependencies:
  - repo_url: github.com/acme/git-utils
    resolved_commit: def789ghi012
    resolved_url: https://alice:{_TOKEN}@reg.example.invalid/apm/git-utils.tgz?token={_TOKEN}
"""


def _evidence(result: CommandResult) -> str:
    return (
        f"command={result.command!r}\nexit={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


def _files_containing_token(root: Path, *, allowed: set[Path]) -> list[Path]:
    hits = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink() or path in allowed:
            continue
        if _TOKEN.encode() in path.read_bytes():
            hits.append(path)
    return hits


def test_credential_values_never_reach_command_output_or_artifacts(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "world", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    # Synthetic credentials on every channel a user can configure them; git
    # stays file-only (GIT_ALLOW_PROTOCOL) so nothing leaves the machine.
    env.update(
        {
            "GITHUB_APM_PAT": _TOKEN,
            "ADO_APM_PAT": _TOKEN,
            "APM_REGISTRY_TOKEN_CORP": _TOKEN,
            "PROXY_REGISTRY_URL": (
                f"http://alice:{_TOKEN}@proxy.example.invalid/art/apm?token={_TOKEN}"
            ),
            "PROXY_REGISTRY_TOKEN": _TOKEN,
            "APM_LOG_LEVEL": "DEBUG",
        }
    )
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=90)
    project = isolated.work_root / "project"
    project.mkdir()
    (project / "apm.yml").write_text("name: consumer\nversion: 1.0.0\n", encoding="ascii")
    (project / "apm.lock.yaml").write_text(_LOCKFILE, encoding="ascii")

    def apm(*args: str, cwd: Path = project) -> CommandResult:
        result = runner.run(args, cwd=cwd, env=env)
        for stream in (result.stdout, result.stderr):
            assert stream.isascii(), _evidence(result)
            assert _TOKEN not in stream, _evidence(result)
        return result

    def ok(*args: str) -> CommandResult:
        result = apm(*args)
        assert result.returncode == 0, _evidence(result)
        return result

    ok("experimental", "enable", "registries")
    ok("config", "set", "registry.corp.token", _TOKEN)
    ok("config", "set", "registry.corp.url", _REGISTRY_URL)
    ok("config", "set", "registry.corp.default", "true")
    config_json = isolated.config_root / "config.json"
    assert _TOKEN in config_json.read_text(encoding="utf-8"), "token must stay stored"

    # Single-key reads: fixed mask, presence reported, URL without userinfo.
    for verbose in ((), ("--verbose",)):
        token_read = ok(*verbose, "config", "get", "registry.corp.token")
        assert token_read.stdout.splitlines()[-1] == f"registry.corp.token: {_MASK}"
        url_read = ok(*verbose, "config", "get", "registry.corp.url")
        assert url_read.stdout.splitlines()[-1] == (
            "registry.corp.url: https://reg.example.invalid/apm"
        )

    # Show-all views report the registry entry with the token masked.
    for args in (("config", "get"), ("config", "list")):
        listing = ok(*args)
        assert f"registry.corp.token: {_MASK}" in listing.stdout, _evidence(listing)
        assert "registry.corp.url: https://reg.example.invalid/apm" in listing.stdout
    ok("config")

    # Free-text values: credential flags, URLs inside equals-form args,
    # username-only userinfo and query credentials render redacted.
    ok("experimental", "enable", "external-scanners")
    args_value = (
        f"--api-key {_TOKEN} --auth={_TOKEN} "
        f"--endpoint=https://{_TOKEN}@reg.example.invalid/apm "
        f"--report=https://u:a:{_TOKEN}@reg.example.invalid/r?sig={_TOKEN}"
    )
    ok("config", "set", "--", "external.skillspector.args", args_value)
    args_line = (
        "external.skillspector.args: --api-key [REDACTED] --auth=[REDACTED] "
        "--endpoint=https://reg.example.invalid/apm "
        "--report=https://reg.example.invalid/r?sig=********"
    )
    assert ok("config", "get", "external.skillspector.args").stdout.splitlines()[-1] == args_line
    for args in (("config", "get"), ("config", "list")):
        listing = ok(*args)
        assert args_line in listing.stdout, _evidence(listing)
    for url in (
        f"https://mcp.example.invalid/v0?token={_TOKEN}",
        f"//{_TOKEN}@h.invalid/x?token={_TOKEN}",
    ):
        rejected = apm("config", "set", "mcp-registry-url", url)
        assert rejected.returncode != 0, _evidence(rejected)

    unset_read = ok("config", "get", "registry.other.token")
    assert unset_read.stdout.splitlines()[-1] == "registry.other.token: Not set"

    # Diagnostics never echo a token, only its presence.
    apm("doctor", "--verbose")

    # SBOM export, to stdout and to a file artifact, in both formats.
    for fmt in ("cyclonedx", "spdx"):
        ok("lock", "export", "--format", fmt)
        ok("lock", "export", "--format", fmt, "-o", f"sbom-{fmt}.json")
        exported = (project / f"sbom-{fmt}.json").read_text(encoding="utf-8")
        assert "reg.example.invalid/apm/git-utils.tgz" in exported
        assert _TOKEN not in exported

    # Error message for a malformed registry URL in apm.yml.
    bad = project / "bad"
    bad.mkdir()
    (bad / "apm.yml").write_text(
        "name: bad\nversion: 1.0.0\nregistries:\n  corp:\n"
        f"    url: ftp://alice:{_TOKEN}@reg.example.invalid/apm?token={_TOKEN}\n",
        encoding="ascii",
    )
    failed = apm("install", cwd=bad)
    assert failed.returncode != 0, _evidence(failed)
    expected = "ftp://reg.example.invalid/apm?token=********"
    assert expected in failed.stdout + failed.stderr, _evidence(failed)

    # No artifact outside the credential store and the test inputs carries it.
    inputs = {config_json, project / "apm.lock.yaml", bad / "apm.yml"}
    leaked = _files_containing_token(isolated.root, allowed=inputs)
    assert leaked == [], leaked
