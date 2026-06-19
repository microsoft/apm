"""Tests for marketplace CLI commands using CliRunner."""

import json  # noqa: F401
import re
from unittest.mock import MagicMock, patch  # noqa: F401
from urllib.parse import urlparse

import pytest
from click.testing import CliRunner

from apm_cli.marketplace.models import (
    MarketplaceManifest,
    MarketplacePlugin,
    MarketplaceSource,
)


def _quoted_hosts(text: str) -> set[str]:
    """Extract host tokens from `Host '<host>'` patterns in error text.

    Each token is normalised through ``urllib.parse.urlparse`` so callers
    compare on parsed hostnames (set equality), not raw substrings -- which
    is what CodeQL's ``py/incomplete-url-substring-sanitization`` rule
    requires (see ``.github/instructions/tests.instructions.md``).
    """
    hosts: set[str] = set()
    for m in re.finditer(r"Host '([^']+)'", text, re.IGNORECASE):
        parsed = urlparse(f"https://{m.group(1)}")
        if parsed.hostname:
            hosts.add(parsed.hostname)
    return hosts


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path, monkeypatch):
    """Isolate filesystem writes."""
    config_dir = str(tmp_path / ".apm")
    monkeypatch.setattr("apm_cli.config.CONFIG_DIR", config_dir)
    monkeypatch.setattr("apm_cli.config.CONFIG_FILE", str(tmp_path / ".apm" / "config.json"))
    monkeypatch.setattr("apm_cli.config._config_cache", None)
    monkeypatch.setattr("apm_cli.marketplace.registry._registry_cache", None)


class TestMarketplaceAdd:
    """marketplace add OWNER/REPO."""

    def test_invalid_format_no_slash(self, runner):
        from apm_cli.commands.marketplace import marketplace

        result = runner.invoke(marketplace, ["add", "just-a-name"])
        assert result.exit_code != 0
        assert "OWNER/REPO" in result.output

    @patch("apm_cli.marketplace.registry.add_marketplace")
    @patch("apm_cli.marketplace.client.fetch_marketplace")
    @patch("apm_cli.marketplace.client._auto_detect_path")
    def test_add_uses_manifest_name_when_available(self, mock_detect, mock_fetch, mock_add, runner):
        """Manifest's `name` field becomes the registered alias."""
        from apm_cli.commands.marketplace import marketplace

        mock_detect.return_value = ".claude-plugin/marketplace.json"
        mock_fetch.return_value = MarketplaceManifest(
            name="addy-agent-skills",
            plugins=(MarketplacePlugin(name="agent-skills"),),
        )

        result = runner.invoke(marketplace, ["add", "addyosmani/agent-skills"])
        assert result.exit_code == 0
        # Registered source carries the manifest's name, not the repo name.
        registered_source = mock_add.call_args[0][0]
        assert registered_source.name == "addy-agent-skills"
        assert registered_source.repo == "agent-skills"
        # Install hint surfaces the alias the user must use next.
        assert "apm install <plugin>@addy-agent-skills" in result.output

    @patch("apm_cli.marketplace.registry.add_marketplace")
    @patch("apm_cli.marketplace.client.fetch_marketplace")
    @patch("apm_cli.marketplace.client._auto_detect_path")
    def test_add_cli_name_overrides_manifest(self, mock_detect, mock_fetch, mock_add, runner):
        """An explicit --name flag wins over the manifest's name."""
        from apm_cli.commands.marketplace import marketplace

        mock_detect.return_value = "marketplace.json"
        mock_fetch.return_value = MarketplaceManifest(
            name="manifest-alias",
            plugins=(MarketplacePlugin(name="p1"),),
        )

        result = runner.invoke(marketplace, ["add", "acme/plugins", "--name", "custom-alias"])
        assert result.exit_code == 0
        registered_source = mock_add.call_args[0][0]
        assert registered_source.name == "custom-alias"
        # No install hint when the user explicitly chose the alias.
        assert "Install plugins with" not in result.output

    @patch("apm_cli.marketplace.registry.add_marketplace")
    @patch("apm_cli.marketplace.client.fetch_marketplace")
    @patch("apm_cli.marketplace.client._auto_detect_path")
    def test_add_falls_back_when_manifest_name_invalid(
        self, mock_detect, mock_fetch, mock_add, runner
    ):
        """Invalid manifest.name triggers a soft fallback to the repo name."""
        from apm_cli.commands.marketplace import marketplace

        mock_detect.return_value = "marketplace.json"
        mock_fetch.return_value = MarketplaceManifest(
            name="has spaces!",
            plugins=(MarketplacePlugin(name="p1"),),
        )

        result = runner.invoke(marketplace, ["add", "acme/plugins"])
        # Soft fallback: the command still succeeds.
        assert result.exit_code == 0
        registered_source = mock_add.call_args[0][0]
        assert registered_source.name == "plugins"
        # User sees a warning quoting the offending value.
        assert "has spaces!" in result.output
        assert "Falling back to repo name" in result.output

    @patch("apm_cli.marketplace.registry.add_marketplace")
    @patch("apm_cli.marketplace.client.fetch_marketplace")
    @patch("apm_cli.marketplace.client._auto_detect_path")
    def test_add_falls_back_when_manifest_name_missing(
        self, mock_detect, mock_fetch, mock_add, runner
    ):
        """Empty manifest.name silently falls back to the repo name."""
        from apm_cli.commands.marketplace import marketplace

        mock_detect.return_value = "marketplace.json"
        mock_fetch.return_value = MarketplaceManifest(
            name="",
            plugins=(MarketplacePlugin(name="p1"),),
        )

        result = runner.invoke(marketplace, ["add", "acme/plugins"])
        assert result.exit_code == 0
        registered_source = mock_add.call_args[0][0]
        assert registered_source.name == "plugins"
        # No warning when the publisher simply omitted the field.
        assert "Falling back" not in result.output
        # No install hint either: alias matches the repo name -- predictable.
        assert "Install plugins with" not in result.output

    def test_add_rejects_invalid_cli_name(self, runner):
        """An invalid --name flag is a user error and hard-fails."""
        from apm_cli.commands.marketplace import marketplace

        result = runner.invoke(marketplace, ["add", "acme/plugins", "--name", "bad name"])
        assert result.exit_code != 0
        assert "Invalid marketplace name" in result.output

    @patch("apm_cli.marketplace.registry.add_marketplace")
    @patch("apm_cli.marketplace.client.fetch_marketplace")
    @patch("apm_cli.marketplace.client._auto_detect_path")
    def test_add_awesome_copilot_pattern_unchanged(self, mock_detect, mock_fetch, mock_add, runner):
        """Regression: github/awesome-copilot manifest name == repo name -> no behaviour change."""
        from apm_cli.commands.marketplace import marketplace

        mock_detect.return_value = ".github/plugin/marketplace.json"
        mock_fetch.return_value = MarketplaceManifest(
            name="awesome-copilot",
            plugins=(MarketplacePlugin(name="azure-cloud-development"),),
        )

        result = runner.invoke(marketplace, ["add", "github/awesome-copilot"])
        assert result.exit_code == 0
        registered_source = mock_add.call_args[0][0]
        assert registered_source.name == "awesome-copilot"
        # Alias matches the repo name, so the install hint is suppressed.
        assert "Install plugins with" not in result.output

    @patch("apm_cli.marketplace.client.fetch_marketplace")
    @patch("apm_cli.marketplace.client._auto_detect_path")
    def test_add_verbose_shows_alias_source(self, mock_detect, mock_fetch, runner):
        """Verbose mode reports which precedence tier picked the alias."""
        from apm_cli.commands.marketplace import marketplace

        mock_detect.return_value = "marketplace.json"
        mock_fetch.return_value = MarketplaceManifest(
            name="acme-tools",
            plugins=(MarketplacePlugin(name="p1"),),
        )

        result = runner.invoke(marketplace, ["add", "acme/plugins", "--verbose"])
        assert result.exit_code == 0
        assert "Alias source: manifest.name" in result.output

    def test_remote_url_default_alias_includes_nearest_path_segment(self):
        """Hosted JSON aliases should not collapse every path on a host to the same name."""
        from apm_cli.commands.marketplace import _default_alias_from_remote_url

        assert (
            _default_alias_from_remote_url("https://catalog.example.com/teams/sec/marketplace.json")
            == "catalog.example.com-sec"
        )

    @patch("apm_cli.marketplace.client.fetch_marketplace")
    @patch("apm_cli.marketplace.client._auto_detect_path")
    def test_successful_add(self, mock_detect, mock_fetch, runner):
        from apm_cli.commands.marketplace import marketplace

        mock_detect.return_value = "marketplace.json"
        mock_fetch.return_value = MarketplaceManifest(
            name="Test",
            plugins=(MarketplacePlugin(name="p1"),),
        )

        result = runner.invoke(marketplace, ["add", "acme-org/plugins"])
        assert result.exit_code == 0
        assert "registered" in result.output.lower() or "1 plugin" in result.output

    @patch("apm_cli.marketplace.client.fetch_marketplace")
    @patch("apm_cli.marketplace.client._auto_detect_path")
    def test_add_respects_github_host(self, mock_detect, mock_fetch, runner, monkeypatch):
        from apm_cli.commands.marketplace import marketplace

        monkeypatch.setenv("GITHUB_HOST", "ghe.corp.example.com")
        mock_detect.return_value = "marketplace.json"
        mock_fetch.return_value = MarketplaceManifest(
            name="Test",
            plugins=(MarketplacePlugin(name="p1"),),
        )

        result = runner.invoke(marketplace, ["add", "acme-org/plugins"])
        assert result.exit_code == 0

        # The probe source passed to _auto_detect_path should carry the GHE host
        probe_source = mock_detect.call_args[0][0]
        assert probe_source.host == "ghe.corp.example.com"

        # The final source passed to fetch_marketplace should also carry it
        final_source = mock_fetch.call_args[0][0]
        assert final_source.host == "ghe.corp.example.com"

    @patch("apm_cli.marketplace.client._auto_detect_path")
    def test_no_marketplace_json_found(self, mock_detect, runner):
        from apm_cli.commands.marketplace import marketplace

        mock_detect.return_value = None
        result = runner.invoke(marketplace, ["add", "acme-org/empty-repo"])
        assert result.exit_code != 0
        assert "marketplace.json" in result.output

    # ------------------------------------------------------------------
    # Issue #1027: full HTTPS URLs and nested HOST/group/sub/.../REPO
    # shorthand. Registration uses AuthResolver.classify_host so GitHub
    # credentials are not forwarded to unsupported hosts (generic / ADO).
    # ------------------------------------------------------------------

    @patch("apm_cli.marketplace.client.fetch_marketplace")
    @patch("apm_cli.marketplace.client._auto_detect_path")
    def test_add_accepts_full_https_url(self, mock_detect, mock_fetch, runner):
        """`apm marketplace add https://github.com/org/repo` parses as github.com/org/repo."""
        from apm_cli.commands.marketplace import marketplace

        mock_detect.return_value = "marketplace.json"
        mock_fetch.return_value = MarketplaceManifest(
            name="m", plugins=(MarketplacePlugin(name="p1"),)
        )

        result = runner.invoke(
            marketplace, ["add", "https://github.com/acme-org/plugin-marketplace"]
        )
        assert result.exit_code == 0, result.output
        probe_source = mock_detect.call_args[0][0]
        assert probe_source.host == "github.com"
        assert probe_source.owner == "acme-org"
        assert probe_source.repo == "plugin-marketplace"

    @patch("apm_cli.marketplace.client.fetch_marketplace")
    @patch("apm_cli.marketplace.client._auto_detect_path")
    def test_add_git_url_fragment_sets_ref_and_canonical_url(self, mock_detect, mock_fetch, runner):
        """Anthropic git URL shape accepts #ref and stores the URL without the fragment."""
        from apm_cli.commands.marketplace import marketplace

        mock_detect.return_value = "marketplace.json"
        mock_fetch.return_value = MarketplaceManifest(
            name="m", plugins=(MarketplacePlugin(name="p1"),)
        )

        result = runner.invoke(
            marketplace,
            ["add", "https://gitlab.com/acme/team/plugin-marketplace.git#v1.2.3"],
        )

        assert result.exit_code == 0, result.output
        probe_source = mock_detect.call_args[0][0]
        final_source = mock_fetch.call_args[0][0]
        assert probe_source.url == "https://gitlab.com/acme/team/plugin-marketplace.git"
        assert probe_source.ref == "v1.2.3"
        assert final_source.url == "https://gitlab.com/acme/team/plugin-marketplace.git"
        assert final_source.ref == "v1.2.3"

    @patch("apm_cli.marketplace.client.fetch_marketplace")
    @patch("apm_cli.marketplace.client._auto_detect_path")
    def test_add_unpinned_git_url_warns_about_mutable_ref(self, mock_detect, mock_fetch, runner):
        """Git URL sources without #ref get an actionable pinning warning."""
        from apm_cli.commands.marketplace import marketplace

        mock_detect.return_value = "marketplace.json"
        mock_fetch.return_value = MarketplaceManifest(
            name="m", plugins=(MarketplacePlugin(name="p1"),)
        )

        result = runner.invoke(
            marketplace, ["add", "https://gitlab.com/acme/team/plugin-marketplace.git"]
        )

        assert result.exit_code == 0, result.output
        assert "Pin this git marketplace" in result.output
        assert "#v1.0.0" in result.output

    @patch("apm_cli.marketplace.registry.add_marketplace")
    def test_add_local_marketplace_json_file(self, mock_add, runner, tmp_path):
        """Local file sources register through apm marketplace add."""
        from apm_cli.commands.marketplace import marketplace

        manifest = tmp_path / "marketplace.json"
        manifest.write_text(
            '{"name":"local-catalog","plugins":[{"name":"tool","repository":"acme/tool"}]}'
        )

        result = runner.invoke(marketplace, ["add", str(manifest), "--name", "local-catalog"])

        assert result.exit_code == 0, result.output
        registered = mock_add.call_args[0][0]
        assert registered.name == "local-catalog"
        assert registered.path == ""
        assert registered.kind == "local"

    @patch("apm_cli.marketplace.registry.add_marketplace")
    def test_add_remote_marketplace_json_url_fetches_directly(self, mock_add, runner, monkeypatch):
        """Remote marketplace.json URLs are direct JSON sources, not git repositories."""
        from apm_cli.commands.marketplace import marketplace

        raw = b'{"name":"catalog","plugins":[{"name":"tool","repository":"acme/tool"}]}'

        class Response:
            status_code = 200
            url = "https://catalog.example.com/marketplace.json"

            def __init__(self):
                self.headers = {"ETag": "abc"}

            def iter_content(self, chunk_size):
                yield raw[:chunk_size]
                yield raw[chunk_size:]

            def raise_for_status(self):
                return None

            def close(self):
                return None

        requests_seen: list[str] = []

        class Session:
            def get(self, url, headers=None, timeout=None, stream=False):
                assert stream is True
                requests_seen.append(url)
                return Response()

        monkeypatch.setattr("apm_cli.marketplace.client._HTTP_SESSION", Session())

        result = runner.invoke(
            marketplace,
            ["add", "https://catalog.example.com/marketplace.json", "--name", "catalog"],
        )

        assert result.exit_code == 0, result.output
        assert len(requests_seen) == 1
        requested = urlparse(requests_seen[0])
        assert (requested.scheme, requested.hostname, requested.path) == (
            "https",
            "catalog.example.com",
            "/marketplace.json",
        )
        registered = mock_add.call_args[0][0]
        assert registered.name == "catalog"
        registered_url = urlparse(registered.url)
        assert (registered_url.scheme, registered_url.hostname, registered_url.path) == (
            "https",
            "catalog.example.com",
            "/marketplace.json",
        )
        assert registered.path == ""
        assert registered.kind == "url"

    @patch("apm_cli.marketplace.registry.add_marketplace")
    def test_add_remote_marketplace_json_url_warns_host_flag_is_ignored(
        self, mock_add, runner, monkeypatch
    ):
        """--host is a shorthand-only flag; hosted marketplace.json URLs carry their own host."""
        from apm_cli.commands.marketplace import marketplace

        raw = b'{"name":"catalog","plugins":[]}'

        class Response:
            status_code = 200
            url = "https://catalog.example.com/marketplace.json"

            def __init__(self):
                self.headers: dict[str, str] = {}

            def iter_content(self, chunk_size):
                yield raw

            def raise_for_status(self):
                return None

            def close(self):
                return None

        class Session:
            def get(self, url, headers=None, timeout=None, stream=False):
                assert stream is True
                return Response()

        monkeypatch.setattr("apm_cli.marketplace.client._HTTP_SESSION", Session())

        result = runner.invoke(
            marketplace,
            [
                "add",
                "https://catalog.example.com/marketplace.json",
                "--host",
                "catalog.example.com",
            ],
        )

        assert result.exit_code == 0, result.output
        assert "--host is ignored when SOURCE is a hosted marketplace.json URL" in result.output
        assert mock_add.call_args[0][0].name == "catalog"

    @patch("apm_cli.marketplace.client.fetch_marketplace")
    @patch("apm_cli.marketplace.client._auto_detect_path")
    def test_add_strips_dot_git_suffix(self, mock_detect, mock_fetch, runner):
        from apm_cli.commands.marketplace import marketplace

        mock_detect.return_value = "marketplace.json"
        mock_fetch.return_value = MarketplaceManifest(
            name="m", plugins=(MarketplacePlugin(name="p1"),)
        )

        result = runner.invoke(
            marketplace, ["add", "https://github.com/acme-org/plugin-marketplace.git"]
        )
        assert result.exit_code == 0, result.output
        probe_source = mock_detect.call_args[0][0]
        assert probe_source.repo == "plugin-marketplace"

    @patch("apm_cli.marketplace.client.fetch_marketplace")
    @patch("apm_cli.marketplace.client._auto_detect_path")
    def test_add_accepts_nested_subpath_on_github_host(
        self, mock_detect, mock_fetch, runner, monkeypatch
    ):
        """N>=4 segments with FQDN first parse as host + multi-segment owner + repo."""
        from apm_cli.commands.marketplace import marketplace

        # Pretend ghes.corp.example.com is the configured GHES host so the
        # trusted-host gate accepts it. This is the realistic shape on which
        # nested sub-paths actually appear.
        monkeypatch.setenv("GITHUB_HOST", "ghes.corp.example.com")
        mock_detect.return_value = "marketplace.json"
        mock_fetch.return_value = MarketplaceManifest(
            name="m", plugins=(MarketplacePlugin(name="p1"),)
        )

        result = runner.invoke(
            marketplace,
            ["add", "ghes.corp.example.com/acme/team/sub/plugin-marketplace"],
        )
        assert result.exit_code == 0, result.output
        probe_source = mock_detect.call_args[0][0]
        assert probe_source.host == "ghes.corp.example.com"
        assert probe_source.owner == "acme/team/sub"
        assert probe_source.repo == "plugin-marketplace"

    @patch("apm_cli.marketplace.client.fetch_marketplace")
    @patch("apm_cli.marketplace.client._auto_detect_path")
    def test_add_accepts_gitlab_com_https_url(self, mock_detect, mock_fetch, runner):
        """GitLab SaaS HTTPS URL passes registration and probes the GitLab host."""
        from apm_cli.commands.marketplace import marketplace

        mock_detect.return_value = "marketplace.json"
        mock_fetch.return_value = MarketplaceManifest(
            name="m", plugins=(MarketplacePlugin(name="p1"),)
        )

        result = runner.invoke(
            marketplace, ["add", "https://gitlab.com/acme/team/plugin-marketplace"]
        )
        assert result.exit_code == 0, result.output
        probe_source = mock_detect.call_args[0][0]
        assert probe_source.host == "gitlab.com"
        assert probe_source.owner == "acme/team"
        assert probe_source.repo == "plugin-marketplace"

    @patch("apm_cli.marketplace.client.fetch_marketplace")
    @patch("apm_cli.marketplace.client._auto_detect_path")
    def test_add_accepts_gitlab_com_host_shorthand(self, mock_detect, mock_fetch, runner):
        """Host-qualified shorthand for gitlab.com passes the registration gate."""
        from apm_cli.commands.marketplace import marketplace

        mock_detect.return_value = "marketplace.json"
        mock_fetch.return_value = MarketplaceManifest(
            name="m", plugins=(MarketplacePlugin(name="p1"),)
        )

        result = runner.invoke(marketplace, ["add", "gitlab.com/acme/team/plugin-marketplace"])
        assert result.exit_code == 0, result.output
        probe_source = mock_detect.call_args[0][0]
        assert probe_source.host == "gitlab.com"
        assert probe_source.owner == "acme/team"
        assert probe_source.repo == "plugin-marketplace"

    @patch("apm_cli.marketplace.client.fetch_marketplace")
    @patch("apm_cli.marketplace.client._auto_detect_path")
    def test_add_accepts_self_managed_gitlab_with_gitlab_host_env(
        self, mock_detect, mock_fetch, runner, monkeypatch
    ):
        """Self-managed GitLab FQDN is accepted when GITLAB_HOST classifies it."""
        from apm_cli.commands.marketplace import marketplace

        monkeypatch.setenv("GITLAB_HOST", "git.epam.com")
        mock_detect.return_value = "marketplace.json"
        mock_fetch.return_value = MarketplaceManifest(
            name="m", plugins=(MarketplacePlugin(name="p1"),)
        )

        result = runner.invoke(marketplace, ["add", "git.epam.com/epm-ease/apm-registry"])
        assert result.exit_code == 0, result.output
        probe_source = mock_detect.call_args[0][0]
        assert probe_source.host == "git.epam.com"
        assert probe_source.owner == "epm-ease"
        assert probe_source.repo == "apm-registry"

    @patch("apm_cli.marketplace.client.fetch_marketplace")
    @patch("apm_cli.marketplace.client._auto_detect_path")
    def test_add_accepts_generic_host_via_git_fetcher(self, mock_detect, mock_fetch, runner):
        """Unknown FQDN is now accepted: kind=git routes through GitCache.

        Before this change, an unknown host was rejected with an explicit
        "not supported" error and a GHES/GitLab export hint. The trust
        gate now applies only to kinds that would forward an APM token
        (github/gitlab); the subprocess git path uses no APM credentials
        unless AuthResolver.classify_host recognises the host.
        """
        from apm_cli.commands.marketplace import marketplace

        mock_detect.return_value = "marketplace.json"
        mock_fetch.return_value = MarketplaceManifest(
            name="m", plugins=(MarketplacePlugin(name="p1"),)
        )

        result = runner.invoke(
            marketplace, ["add", "https://git.example.com/acme/team/plugin-marketplace"]
        )
        assert result.exit_code == 0, result.output
        probe_source = mock_detect.call_args[0][0]
        assert probe_source.kind == "git"
        assert probe_source.host == "git.example.com"

    def test_add_rejects_http_url(self, runner):
        """Plain HTTP URLs are rejected -- no --allow-insecure escape hatch."""
        from apm_cli.commands.marketplace import marketplace

        result = runner.invoke(marketplace, ["add", "http://github.com/acme/plugin-marketplace"])
        assert result.exit_code != 0
        assert "http" in result.output.lower()

    def test_add_rejects_path_traversal_in_url(self, runner):
        """validate_path_segments rejects '..' in any segment."""
        from apm_cli.commands.marketplace import marketplace

        result = runner.invoke(
            marketplace, ["add", "https://github.com/acme/../evil/plugin-marketplace"]
        )
        assert result.exit_code != 0
        # Either the parse-time guard or the segment validator may surface;
        # both produce a clear actionable message.
        assert "traversal" in result.output.lower() or "invalid" in result.output.lower()

    def test_add_rejects_percent_encoded_traversal(self, runner):
        """Percent-encoded '..' must be unescaped before validation."""
        from apm_cli.commands.marketplace import marketplace

        result = runner.invoke(
            marketplace,
            ["add", "https://github.com/acme/%2E%2E/evil/plugin-marketplace"],
        )
        assert result.exit_code != 0
        assert "traversal" in result.output.lower() or "invalid" in result.output.lower()

    def test_add_rejects_double_percent_encoded_traversal(self, runner):
        """Round 4 panel (supply-chain required): doubly percent-encoded '..'
        ('%252E%252E') must not bypass the traversal guard. The guard inside
        validate_path_segments now iteratively unquotes each segment so
        multi-encoded markers are caught."""
        from apm_cli.commands.marketplace import marketplace

        result = runner.invoke(
            marketplace,
            ["add", "https://github.com/acme/%252E%252E/evil/plugin-marketplace"],
        )
        assert result.exit_code != 0
        assert "traversal" in result.output.lower() or "invalid" in result.output.lower()

    def test_add_rejects_conflicting_host_flag_with_url(self, runner):
        from apm_cli.commands.marketplace import marketplace

        result = runner.invoke(
            marketplace,
            [
                "add",
                "https://github.com/acme/plugin-marketplace",
                "--host",
                "ghes.corp.example.com",
            ],
        )
        assert result.exit_code != 0
        assert "conflicting host" in result.output.lower()

    def test_add_rejects_url_without_owner(self, runner):
        from apm_cli.commands.marketplace import marketplace

        result = runner.invoke(marketplace, ["add", "https://github.com/onlyone"])
        assert result.exit_code != 0
        assert "OWNER/REPO" in result.output or "Expected" in result.output

    def test_marketplace_host_classification_via_auth_resolver(self):
        """Round 3: _is_trusted_marketplace_host must not exist; classification
        routes through AuthResolver.classify_host (single source of truth)."""
        from apm_cli.commands import marketplace as marketplace_cmd_pkg

        assert not hasattr(marketplace_cmd_pkg, "_is_trusted_marketplace_host"), (
            "Round 3 panel: _is_trusted_marketplace_host must be removed; "
            "classification is owned by AuthResolver.classify_host"
        )

    @patch("apm_cli.marketplace.client.fetch_marketplace")
    @patch("apm_cli.marketplace.client._auto_detect_path")
    def test_unknown_host_no_longer_rejects_with_export_hint(self, mock_detect, mock_fetch, runner):
        """Replacement for the round-3 'untrusted host' regression trap.

        Before this change, an unknown host produced a rejection message
        leading with 'not supported' plus an ``export GITHUB_HOST=...``
        hint. With kind-aware routing, the same input is accepted and
        falls through to the subprocess-git fetcher (no APM token leak
        possible -- AuthResolver only emits credentials matching a
        classified host).
        """
        from apm_cli.commands.marketplace import marketplace

        mock_detect.return_value = "marketplace.json"
        mock_fetch.return_value = MarketplaceManifest(
            name="m", plugins=(MarketplacePlugin(name="p1"),)
        )

        result = runner.invoke(
            marketplace,
            ["add", "https://git.example.com/acme/team/plugin-marketplace"],
        )
        assert result.exit_code == 0, result.output
        normalized = " ".join(result.output.split())
        # No 'not supported' rejection in the success path.
        assert "not supported" not in result.output.lower()
        # No misleading 'export GITHUB_HOST=...' hint.
        assert "export GITHUB_HOST=" not in normalized

    @patch("apm_cli.marketplace.client.fetch_marketplace")
    @patch("apm_cli.marketplace.client._auto_detect_path")
    def test_ghes_shorthand_routes_through_git_fetcher_when_unclassified(
        self, mock_detect, mock_fetch, runner
    ):
        """Replacement for the round-4 'copyable export and rerun' trap.

        Before this change, an unconfigured GHES host produced a hard
        rejection plus a copy-paste GHES setup recipe. With kind-aware
        routing, the same input is accepted; the user can still set
        GITHUB_HOST explicitly if they want the Contents-API code path
        (faster) but generic git via GitCache is the safe default.
        """
        from apm_cli.commands.marketplace import marketplace

        mock_detect.return_value = "marketplace.json"
        mock_fetch.return_value = MarketplaceManifest(
            name="m", plugins=(MarketplacePlugin(name="p1"),)
        )

        result = runner.invoke(marketplace, ["add", "myghes.corp/org/repo"])
        assert result.exit_code == 0, result.output
        probe_source = mock_detect.call_args[0][0]
        assert probe_source.kind == "git"
        assert probe_source.host == "myghes.corp"

    def test_path_traversal_error_message_no_double_exception_text(self, runner):
        """Round 3 panel: PathTraversalError message must not embed the raw
        exception text mid-sentence (no double 'rejected: ... rejected' noise)."""
        from apm_cli.commands.marketplace import marketplace

        result = runner.invoke(
            marketplace, ["add", "https://github.com/acme/../evil/plugin-marketplace"]
        )
        assert result.exit_code != 0
        out = result.output.lower()
        # The phrase "path-traversal sequence" must appear at most once.
        assert out.count("path-traversal sequence") <= 1
        # Must not duplicate the "rejected" keyword (old form: "rejected: ...
        # rejected").
        assert out.count("rejected") <= 1

    def test_conflicting_host_error_includes_runnable_command(self, runner):
        """Round 3 panel: conflicting-host error must give a copy-pasteable next
        command (apm marketplace add <raw>) rather than abstract advice."""
        from apm_cli.commands.marketplace import marketplace

        result = runner.invoke(
            marketplace,
            [
                "add",
                "https://github.com/acme/plugin-marketplace",
                "--host",
                "ghes.corp.example.com",
            ],
        )
        assert result.exit_code != 0
        # Rich console may soft-wrap long lines; collapse whitespace before
        # asserting the runnable command appears intact.
        normalized = " ".join(result.output.split())
        assert "apm marketplace add https://github.com/acme/plugin-marketplace" in normalized


class TestMarketplaceList:
    """marketplace list."""

    def test_empty_list(self, runner):
        from apm_cli.commands.marketplace import marketplace

        result = runner.invoke(marketplace, ["list"])
        assert result.exit_code == 0
        assert "no marketplace" in result.output.lower() or "add" in result.output.lower()

    @patch("apm_cli.marketplace.registry.get_registered_marketplaces")
    def test_list_with_entries(self, mock_get, runner):
        from apm_cli.commands.marketplace import marketplace

        mock_get.return_value = [
            MarketplaceSource(name="acme", owner="acme-org", repo="plugins"),
        ]
        result = runner.invoke(marketplace, ["list"])
        assert result.exit_code == 0
        assert "acme" in result.output


class TestMarketplaceBrowse:
    """marketplace browse NAME."""

    @patch("apm_cli.marketplace.client.fetch_marketplace")
    @patch("apm_cli.marketplace.registry.get_marketplace_by_name")
    def test_browse_shows_plugins(self, mock_get, mock_fetch, runner):
        from apm_cli.commands.marketplace import marketplace

        mock_get.return_value = MarketplaceSource(name="acme", owner="acme-org", repo="plugins")
        mock_fetch.return_value = MarketplaceManifest(
            name="Acme",
            plugins=(
                MarketplacePlugin(name="security-checks", description="Scans"),
                MarketplacePlugin(name="code-review", description="Reviews"),
            ),
        )
        result = runner.invoke(marketplace, ["browse", "acme"])
        assert result.exit_code == 0
        assert "security-checks" in result.output


class TestMarketplaceUpdate:
    """marketplace update [NAME]."""

    @patch("apm_cli.marketplace.client.fetch_marketplace")
    @patch("apm_cli.marketplace.client.clear_marketplace_cache")
    @patch("apm_cli.marketplace.registry.get_marketplace_by_name")
    def test_update_single(self, mock_get, mock_clear, mock_fetch, runner):
        from apm_cli.commands.marketplace import marketplace

        mock_get.return_value = MarketplaceSource(name="acme", owner="acme-org", repo="plugins")
        mock_fetch.return_value = MarketplaceManifest(
            name="Acme", plugins=(MarketplacePlugin(name="p1"),)
        )
        result = runner.invoke(marketplace, ["update", "acme"])
        assert result.exit_code == 0
        assert "updated" in result.output.lower() or "1 plugin" in result.output


class TestMarketplaceRemove:
    """marketplace remove NAME."""

    @patch("apm_cli.marketplace.client.clear_marketplace_cache")
    @patch("apm_cli.marketplace.registry.remove_marketplace")
    @patch("apm_cli.marketplace.registry.get_marketplace_by_name")
    def test_remove_with_confirm(self, mock_get, mock_remove, mock_clear, runner):
        from apm_cli.commands.marketplace import marketplace

        mock_get.return_value = MarketplaceSource(name="acme", owner="acme-org", repo="plugins")
        result = runner.invoke(marketplace, ["remove", "acme", "--yes"])
        assert result.exit_code == 0
        mock_remove.assert_called_once()
        assert "removed" in result.output.lower()


class TestSearch:
    """Top-level search command -- requires QUERY@MARKETPLACE format."""

    def test_search_missing_at_symbol(self, runner):
        from apm_cli.commands.marketplace import search

        result = runner.invoke(search, ["security"])
        assert result.exit_code != 0
        assert "QUERY@MARKETPLACE" in result.output

    def test_search_empty_query(self, runner):
        from apm_cli.commands.marketplace import search

        result = runner.invoke(search, ["@skills"])
        assert result.exit_code != 0
        assert "QUERY" in result.output and "MARKETPLACE" in result.output

    def test_search_empty_marketplace(self, runner):
        from apm_cli.commands.marketplace import search

        result = runner.invoke(search, ["security@"])
        assert result.exit_code != 0
        assert "QUERY" in result.output and "MARKETPLACE" in result.output

    @patch("apm_cli.marketplace.registry.get_marketplace_by_name")
    def test_search_unknown_marketplace(self, mock_get, runner):
        from apm_cli.commands.marketplace import search
        from apm_cli.marketplace.errors import MarketplaceNotFoundError

        mock_get.side_effect = MarketplaceNotFoundError("nonexistent")
        result = runner.invoke(search, ["security@nonexistent"])
        assert result.exit_code != 0
        assert "not registered" in result.output.lower()

    @patch("apm_cli.marketplace.client.search_marketplace")
    @patch("apm_cli.marketplace.registry.get_marketplace_by_name")
    def test_search_finds_results(self, mock_get, mock_search, runner):
        from apm_cli.commands.marketplace import search

        mock_get.return_value = MarketplaceSource(
            name="skills",
            owner="anthropics",
            repo="anthropics/skills",
            path=".claude-plugin/marketplace.json",
        )
        mock_search.return_value = [
            MarketplacePlugin(
                name="security-scanner",
                description="Scans code",
                source_marketplace="skills",
            ),
        ]
        result = runner.invoke(search, ["security@skills"])
        assert result.exit_code == 0
        assert "security-scanner" in result.output

    @patch("apm_cli.marketplace.client.search_marketplace")
    @patch("apm_cli.marketplace.registry.get_marketplace_by_name")
    def test_search_no_results(self, mock_get, mock_search, runner):
        from apm_cli.commands.marketplace import search

        mock_get.return_value = MarketplaceSource(
            name="skills",
            owner="anthropics",
            repo="anthropics/skills",
            path=".claude-plugin/marketplace.json",
        )
        mock_search.return_value = []
        result = runner.invoke(search, ["zzz-nonexistent@skills"])
        assert result.exit_code == 0
        assert (
            "no plugin" in result.output.lower()
            or "not found" in result.output.lower()
            or "browse" in result.output.lower()
        )
