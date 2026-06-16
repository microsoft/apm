"""Hermetic e2e pin for ADO-hosted marketplace authoring -> consume (#1010).

The isolated pieces of Azure DevOps marketplace support are already proven
elsewhere:

- ``tests/unit/marketplace/test_marketplace_source_base.py`` -- an ADO
  ``sourceBase`` (``org/project/_git``) composes a relative repo and keeps the
  ``dev.azure.com`` host.
- ``tests/unit/marketplace/test_parser.py`` -- an ADO HTTPS URL classifies as
  ``git`` kind with the host preserved.
- ``tests/integration/test_ado_e2e.py`` -- install-as-dependency from a *live*
  ADO repo (gated on ``ADO_APM_PAT``).

What was NOT pinned is the *end-to-end marketplace* flow with no network: an
ADO ``sourceBase`` marketplace authored through the full ``MarketplaceBuilder``
build pipeline, then a dependency *consumed* from the emitted
``marketplace.json``. This module closes that gap hermetically (no
``ADO_APM_PAT``, no live network) so the authoring -> consume contract for ADO
cannot silently regress.

Per ``tests/instructions``: every URL assertion parses with ``urllib.parse``
and compares on a parsed component -- never a substring.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import yaml

from apm_cli.core.auth import AuthResolver
from apm_cli.marketplace.builder import BuildOptions, MarketplaceBuilder
from apm_cli.marketplace.migration import load_marketplace_config
from apm_cli.models.dependency.reference import DependencyReference

# A 40-char hex SHA so ref resolution never touches the network.
_SHA = "a" * 40

# ADO sourceBase under test: org=contoso, project=platform, 3-part _git base.
_ADO_SOURCE_BASE = "https://dev.azure.com/contoso/platform/_git"
_ADO_ORG = "contoso"
_ADO_HOST = "dev.azure.com"
_PACKAGE_SOURCE = "agent-skills"


class _RecordingAuthResolver:
    """Record ``AuthResolver`` calls while returning a token-bearing context.

    Mirrors the recording stub in ``test_marketplace_source_base.py`` so the
    auth-routing assertion stays consistent with the unit-level guard. The
    returned token is deliberately host-agnostic; the point of the test is to
    prove *which host* the resolver is asked about, never the token value.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def resolve(self, host: str, org: str | None = None):
        self.calls.append((host, org))
        return SimpleNamespace(token="ado-secret-token", source="ADO_APM_PAT")


def _write_ado_marketplace(tmp_path: Path) -> Path:
    """Author an ADO-hosted marketplace apm.yml and return its path."""
    apm_yml = tmp_path / "apm.yml"
    apm_yml.write_text(
        yaml.safe_dump(
            {
                "name": "ado-marketplace",
                "description": "ADO-hosted marketplace",
                "version": "1.0.0",
                "marketplace": {
                    "owner": {"name": "Contoso"},
                    "sourceBase": _ADO_SOURCE_BASE,
                    "claude": {"output": "marketplace.json"},
                    "packages": [
                        {
                            "name": _PACKAGE_SOURCE,
                            "source": _PACKAGE_SOURCE,
                            "ref": _SHA,
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    return apm_yml


class TestADOMarketplaceAuthoringToConsume:
    """End-to-end: author an ADO marketplace, then consume a dep from it."""

    def test_build_emits_host_preserving_ado_source_url(self, tmp_path: Path) -> None:
        """The build pipeline writes a ``dev.azure.com`` URL, not github.com."""
        apm_yml = _write_ado_marketplace(tmp_path)
        builder = MarketplaceBuilder(apm_yml, options=BuildOptions(offline=True))

        report = builder.build()

        assert report.primary_output.errors == ()
        output = json.loads((tmp_path / "marketplace.json").read_text(encoding="utf-8"))
        source = output["plugins"][0]["source"]
        # ADO is a non-default host -> emitted as an explicit URL source.
        assert source["source"] == "url"
        parsed = urlparse(source["url"])
        assert parsed.scheme == "https"
        assert parsed.hostname == _ADO_HOST
        assert parsed.path == "/contoso/platform/_git/agent-skills"
        assert source["ref"] == _SHA
        assert source["sha"] == _SHA

    def test_consumed_dependency_reference_is_ado_typed_and_host_preserving(
        self, tmp_path: Path
    ) -> None:
        """A consumer parsing the emitted URL gets an ADO-typed, host-stable ref."""
        apm_yml = _write_ado_marketplace(tmp_path)
        builder = MarketplaceBuilder(apm_yml, options=BuildOptions(offline=True))
        builder.build()

        output = json.loads((tmp_path / "marketplace.json").read_text(encoding="utf-8"))
        consumed_url = output["plugins"][0]["source"]["url"]

        # Consume side: the marketplace URL parses into a dependency reference.
        ref = DependencyReference.parse(consumed_url)
        assert ref.host == _ADO_HOST
        assert ref.is_azure_devops() is True
        # Host-preserving: the org/project/repo coordinate stays under contoso,
        # never rewritten onto github.com.
        assert ref.repo_url == "contoso/platform/agent-skills"

        # Auth context for the consumed host is ADO-typed.
        host_info = AuthResolver.classify_host(urlparse(consumed_url).hostname)
        assert host_info.kind == "ado"

    def test_resolve_routes_auth_to_ado_host_only(self, tmp_path: Path) -> None:
        """No cross-host token leak: auth is asked only about dev.azure.com.

        The ADO ``sourceBase`` must route credential resolution to the ADO host
        (with the ``contoso`` org hint) and never to github.com, so an ADO token
        can never be offered to a GitHub remote.
        """
        _write_ado_marketplace(tmp_path)
        config = load_marketplace_config(tmp_path)
        auth = _RecordingAuthResolver()
        builder = MarketplaceBuilder.from_config(
            config,
            tmp_path,
            BuildOptions(offline=False),
            auth_resolver=auth,
        )

        resolved = builder._resolve_entry(config.packages[0])

        assert resolved.source_repo == "contoso/platform/_git/agent-skills"
        assert resolved.host == _ADO_HOST
        # Every auth resolution targeted the ADO host + org; github.com never seen.
        assert auth.calls == [(_ADO_HOST, _ADO_ORG)]
        resolved_hosts = {host for host, _org in auth.calls}
        assert resolved_hosts == {_ADO_HOST}
        assert "github.com" not in resolved_hosts

    def test_github_host_is_not_classified_as_ado(self) -> None:
        """Sibling guard: github.com must not borrow ADO auth typing."""
        assert AuthResolver.classify_host("github.com").kind == "github"
        assert AuthResolver.classify_host(_ADO_HOST).kind == "ado"
