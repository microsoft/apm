"""Hermetic end-to-end coverage for ADO marketplace source resolution."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from urllib.parse import urlparse

from click.testing import CliRunner

from apm_cli.commands.marketplace import marketplace
from apm_cli.core.auth import AuthResolver


def test_marketplace_check_uses_ado_url_and_bearer_auth(monkeypatch, tmp_path) -> None:
    """The CLI checks an ADO sourceBase with its exact URL and bearer header."""
    bearer = "test-ado-bearer"
    sha = "a" * 40
    (tmp_path / "apm.yml").write_text(
        """\
name: ado-marketplace
description: ADO marketplace regression
version: 1.0.0
marketplace:
  owner:
    name: Contoso
  sourceBase: https://dev.azure.com/contoso/platform/_git
  packages:
    - name: my-package
      source: my-package
      ref: main
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        AuthResolver,
        "resolve",
        lambda _self, host, org=None: SimpleNamespace(
            token=bearer,
            source="AAD_BEARER_AZ_CLI",
            auth_scheme="bearer",
        ),
    )

    def fake_git(command, **kwargs):
        parsed = urlparse(command[-1])
        assert parsed.scheme == "https"
        assert parsed.hostname == "dev.azure.com"
        assert parsed.path == "/contoso/platform/_git/my-package"
        assert parsed.username is None
        env = kwargs["env"]
        assert env["GIT_CONFIG_COUNT"] == "1"
        assert env["GIT_CONFIG_KEY_0"] == "http.extraheader"
        assert env["GIT_CONFIG_VALUE_0"] == f"Authorization: Bearer {bearer}"
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=f"{sha}\trefs/heads/main\n",
            stderr="",
        )

    monkeypatch.setattr("apm_cli.marketplace.ref_resolver.subprocess.run", fake_git)

    result = CliRunner().invoke(marketplace, ["check", "--verbose"])

    assert result.exit_code == 0, result.output
    printed_urls = [
        urlparse(token.rstrip(":"))
        for token in result.output.split()
        if token.startswith("https://")
    ]
    assert [(url.scheme, url.hostname, url.path) for url in printed_urls] == [
        ("https", "dev.azure.com", "/contoso/platform/_git/my-package")
    ]
