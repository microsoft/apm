"""Hermetic command-to-auth coverage for default-host marketplace checks."""

from __future__ import annotations

import base64
import subprocess
from urllib.parse import urlparse

import pytest
from click.testing import CliRunner

from apm_cli.commands.marketplace import marketplace

pytestmark = pytest.mark.component


@pytest.mark.parametrize(
    ("configured_host", "expected_host"),
    [
        (None, "github.com"),
        ("", "github.com"),
        ("github.example.com", "github.example.com"),
    ],
)
def test_default_host_shorthand_uses_github_apm_pat(
    configured_host, expected_host, monkeypatch, tmp_path
) -> None:
    """Bare owner/repo sources carry default-host auth into git ls-remote."""
    token = "test-github-apm-pat"
    sha = "a" * 40
    (tmp_path / "apm.yml").write_text(
        """\
name: github-marketplace
description: GitHub marketplace regression
version: 1.0.0
marketplace:
  owner:
    name: Example
  packages:
    - name: private-package
      source: example-org/private-package
      ref: v1.0.0
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_APM_PAT", token)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    if configured_host is None:
        monkeypatch.delenv("GITHUB_HOST", raising=False)
    else:
        monkeypatch.setenv("GITHUB_HOST", configured_host)

    def fake_git(command, **kwargs):
        parsed = urlparse(command[-1])
        assert parsed.scheme == "https"
        assert parsed.hostname == expected_host
        assert parsed.path == "/example-org/private-package.git"
        assert parsed.username is None
        assert parsed.password is None
        assert token not in command[-1]
        env = kwargs["env"]
        headers = {
            env[f"GIT_CONFIG_KEY_{index}"]: value
            for index in range(int(env["GIT_CONFIG_COUNT"]))
            if "Authorization" in (value := env[f"GIT_CONFIG_VALUE_{index}"])
        }
        expected_auth = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        assert headers == {
            f"http.{command[-1]}.extraheader": f"Authorization: Basic {expected_auth}"
        }
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=f"{sha}\trefs/tags/v1.0.0\n",
            stderr="",
        )

    monkeypatch.setattr("apm_cli.marketplace.ref_resolver.subprocess.run", fake_git)

    result = CliRunner().invoke(marketplace, ["check", "--verbose"])

    assert result.exit_code == 0, result.output
    routing_line = next(
        line for line in result.output.splitlines() if "Resolving private-package via " in line
    )
    assert routing_line.split(" via ", 1)[1].split(":", 1)[0] == expected_host
