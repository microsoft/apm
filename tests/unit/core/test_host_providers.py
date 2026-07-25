"""Credential-scope regressions for manifest host-type hints."""

from __future__ import annotations

import os
from unittest.mock import patch

from apm_cli.core.auth import AuthResolver
from apm_cli.core.host_providers import classify_host_provider


def test_gitlab_hint_does_not_route_global_pat_to_untrusted_host() -> None:
    sentinel = "gitlab-global-sentinel"
    with patch.dict(os.environ, {"GITLAB_APM_PAT": sentinel}, clear=True):
        context = AuthResolver(allow_external_fallback=False).resolve(
            "packages.attacker.example",
            host_type="gitlab",
        )

    assert context.host_info.kind == "gitlab"
    assert context.token is None
    assert context.source == "none"


def test_canonical_gitlab_host_still_uses_global_pat() -> None:
    sentinel = "gitlab-global-sentinel"
    with patch.dict(os.environ, {"GITLAB_APM_PAT": sentinel}, clear=True):
        context = AuthResolver(allow_external_fallback=False).resolve("gitlab.com")

    assert context.token == sentinel
    assert context.source == "GITLAB_APM_PAT"


def test_user_trusted_gitlab_host_uses_global_pat() -> None:
    sentinel = "gitlab-global-sentinel"
    env = {
        "APM_GITLAB_HOSTS": "gitlab.corp.example",
        "GITLAB_APM_PAT": sentinel,
    }
    with patch.dict(os.environ, env, clear=True):
        context = AuthResolver(allow_external_fallback=False).resolve(
            "gitlab.corp.example",
            host_type="gitlab",
        )

    assert context.token == sentinel
    assert context.source == "GITLAB_APM_PAT"


class TestCustomAdoServerHostClassification:
    """Regression tests for #2338 -- on-prem ADO Server must not be misclassified as GHES."""

    def test_custom_ado_host_via_ado_host_env_is_classified_as_ado(self) -> None:
        env = {"ADO_HOST": "ado.corp.example.com"}
        with patch.dict(os.environ, env, clear=True):
            provider = classify_host_provider("ado.corp.example.com")
        assert provider.kind == "ado"
        assert provider.credential_purpose == "ado_modules"

    def test_custom_ado_host_via_apm_ado_hosts_env_is_classified_as_ado(self) -> None:
        env = {"APM_ADO_HOSTS": "ado1.corp.example.com, ado2.corp.example.com"}
        with patch.dict(os.environ, env, clear=True):
            provider = classify_host_provider("ado1.corp.example.com")
        assert provider.kind == "ado"
        assert provider.credential_purpose == "ado_modules"

    def test_github_host_set_to_ado_server_not_misrouted_to_ghes(self) -> None:
        """The critical regression: GITHUB_HOST=ado-server must NOT produce kind=ghes."""
        env = {
            "GITHUB_HOST": "ado.corp.example.com",
            "ADO_HOST": "ado.corp.example.com",
        }
        with patch.dict(os.environ, env, clear=True):
            provider = classify_host_provider("ado.corp.example.com")
        assert provider.kind == "ado"
        assert provider.credential_purpose == "ado_modules"

    def test_github_host_without_ado_host_still_routes_as_ghes(self) -> None:
        """GITHUB_HOST alone (no ADO_HOST) keeps routing to GHES for genuine GHE users."""
        env = {"GITHUB_HOST": "ghe.corp.example.com"}
        with patch.dict(os.environ, env, clear=True):
            provider = classify_host_provider("ghe.corp.example.com")
        assert provider.kind == "ghes"
        assert provider.credential_purpose == "modules"

    def test_custom_ado_host_auth_resolver_uses_ado_pat(self) -> None:
        """AuthResolver must supply ADO_APM_PAT for a custom ADO Server host."""
        sentinel = "ado-pat-sentinel"
        env = {
            "ADO_HOST": "ado.corp.example.com",
            "ADO_APM_PAT": sentinel,
        }
        with patch.dict(os.environ, env, clear=True):
            context = AuthResolver(allow_external_fallback=False).resolve("ado.corp.example.com")
        assert context.host_info.kind == "ado"
        assert context.token == sentinel
        assert context.source == "ADO_APM_PAT"

    def test_ado_api_base_for_cloud_host_is_dev_azure_com(self) -> None:
        provider = classify_host_provider("dev.azure.com")
        assert provider.api_base("dev.azure.com") == "https://dev.azure.com"

    def test_ado_api_base_for_custom_host_uses_custom_host(self) -> None:
        env = {"ADO_HOST": "ado.corp.example.com"}
        with patch.dict(os.environ, env, clear=True):
            provider = classify_host_provider("ado.corp.example.com")
        assert provider.api_base("ado.corp.example.com") == "https://ado.corp.example.com"
