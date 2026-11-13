"""Tests for marketplace CLI commands using CliRunner."""

import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import pytest
from click.testing import CliRunner

from apm_cli.marketplace.errors import MarketplaceNotFoundError
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
    @patch("apm_cli.marketplace.client._auto_detect_path_with_payload")
    def test_add_uses_manifest_name_when_available(self, mock_detect, mock_fetch, mock_add, runner):
        """Manifest's `name` field becomes the registered alias."""
        from apm_cli.commands.marketplace import marketplace

        mock_detect.return_value = (".claude-plugin/marketplace.json", None)
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
    @patch("apm_cli.marketplace.client._auto_detect_path_with_payload")
    def test_add_cli_name_overrides_manifest(self, mock_detect, mock_fetch, mock_add, runner):
        """An explicit --name flag wins over the manifest's name."""
        from apm_cli.commands.marketplace import marketplace

        mock_detect.return_value = ("marketplace.json", None)
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
    @patch("apm_cli.marketplace.client._auto_detect_path_with_payload")
    def test_add_falls_back_when_manifest_name_invalid(
        self, mock_detect, mock_fetch, mock_add, runner
    ):
        """Invalid manifest.name triggers a soft fallback to the repo name."""
        from apm_cli.commands.marketplace import marketplace

        mock_detect.return_value = ("marketplace.json", None)
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
    @patch("apm_cli.marketplace.client._auto_detect_path_with_payload")
    def test_add_falls_back_when_manifest_name_missing(
        self, mock_detect, mock_fetch, mock_add, runner
    ):
        """Empty manifest.name silently falls back to the repo name."""
        from apm_cli.commands.marketplace import marketplace

        mock_detect.return_value = ("marketplace.json", None)
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
    @patch("apm_cli.marketplace.client._auto_detect_path_with_payload")
    def test_add_awesome_copilot_pattern_unchanged(self, mock_detect, mock_fetch, mock_add, runner):
        """Regression: github/awesome-copilot manifest name == repo name -> no behaviour change."""
        from apm_cli.commands.marketplace import marketplace

        mock_detect.return_value = (".github/plugin/marketplace.json", None)
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
    @patch("apm_cli.marketplace.client._auto_detect_path_with_payload")
    def test_add_verbose_shows_alias_source(self, mock_detect, mock_fetch, runner):
        """Verbose mode reports which precedence tier picked the alias."""
        from apm_cli.commands.marketplace import marketplace

        mock_detect.return_value = ("marketplace.json", None)
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
    @patch("apm_cli.marketplace.client._auto_detect_path_with_payload")
    def test_successful_add(self, mock_detect, mock_fetch, runner):
        from apm_cli.commands.marketplace import marketplace

        mock_detect.return_value = ("marketplace.json", None)
        mock_fetch.return_value = MarketplaceManifest(
            name="Test",
            plugins=(MarketplacePlugin(name="p1"),),
        )

        result = runner.invoke(marketplace, ["add", "acme-org/plugins"])
        assert result.exit_code == 0
        assert "registered" in result.output.lower() or "1 plugin" in result.output

    @patch("apm_cli.marketplace.client.fetch_marketplace")
    @patch("apm_cli.marketplace.client._auto_detect_path_with_payload")
    def test_add_respects_github_host(self, mock_detect, mock_fetch, runner, monkeypatch):
        from apm_cli.commands.marketplace import marketplace

        monkeypatch.setenv("GITHUB_HOST", "ghe.corp.example.com")
        mock_detect.return_value = ("marketplace.json", None)
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

    @patch("apm_cli.marketplace.client._auto_detect_path_with_payload")
    def test_no_marketplace_json_found(self, mock_detect, runner):
        from apm_cli.commands.marketplace import marketplace

        mock_detect.return_value = (None, None)
        result = runner.invoke(marketplace, ["add", "acme-org/empty-repo"])
        assert result.exit_code != 0
        assert "marketplace.json" in result.output

    # ------------------------------------------------------------------
    # Issue #1027: full HTTPS URLs and nested HOST/group/sub/.../REPO
    # shorthand. Registration uses AuthResolver.classify_host so GitHub
    # credentials are not forwarded to unsupported hosts (generic / ADO).
    # ------------------------------------------------------------------

    @patch("apm_cli.marketplace.client.fetch_marketplace")
    @patch("apm_cli.marketplace.client._auto_detect_path_with_payload")
    def test_add_accepts_full_https_url(self, mock_detect, mock_fetch, runner):
        """`apm marketplace add https://github.com/org/repo` parses as github.com/org/repo."""
        from apm_cli.commands.marketplace import marketplace

        mock_detect.return_value = ("marketplace.json", None)
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
    @patch("apm_cli.marketplace.client._auto_detect_path_with_payload")
    def test_add_git_url_fragment_sets_ref_and_canonical_url(self, mock_detect, mock_fetch, runner):
        """Anthropic git URL shape accepts #ref and stores the URL without the fragment."""
        from apm_cli.commands.marketplace import marketplace

        mock_detect.return_value = ("marketplace.json", None)
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
    @patch("apm_cli.marketplace.client._auto_detect_path_with_payload")
    def test_add_unpinned_git_url_warns_about_mutable_ref(self, mock_detect, mock_fetch, runner):
        """Git URL sources without #ref get an actionable pinning warning."""
        from apm_cli.commands.marketplace import marketplace

        mock_detect.return_value = ("marketplace.json", None)
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

    @patch("apm_cli.marketplace.registry.add_marketplace")
    @patch("apm_cli.marketplace.client.fetch_marketplace")
    @patch("apm_cli.marketplace.client._auto_detect_path_with_payload")
    def test_add_mixed_case_ssh_url_warns_host_flag_is_ignored(
        self, mock_detect, mock_fetch, mock_add, runner
    ):
        from apm_cli.commands.marketplace import marketplace

        mock_detect.return_value = ("marketplace.json", None)
        mock_fetch.return_value = MarketplaceManifest(
            name="m",
            plugins=(MarketplacePlugin(name="p1"),),
        )
        result = runner.invoke(
            marketplace,
            [
                "add",
                "SSH://git@gitea.example.com:2222/org/repo.git",
                "--host",
                "other.example.com",
            ],
        )

        assert result.exit_code == 0, result.output
        assert "--host is ignored when SOURCE is a full URL" in result.output
        registered_url = urlparse(mock_add.call_args[0][0].url)
        assert registered_url.scheme == "ssh"
        assert registered_url.hostname == "gitea.example.com"
        assert registered_url.port == 2222

    @patch("apm_cli.marketplace.registry.add_marketplace")
    @patch("apm_cli.marketplace.client.fetch_marketplace")
    @patch("apm_cli.marketplace.client._auto_detect_path_with_payload")
    def test_add_unpinned_ssh_url_warns_about_mutable_ref(
        self, mock_detect, mock_fetch, mock_add, runner
    ):
        from apm_cli.commands.marketplace import marketplace

        mock_detect.return_value = ("marketplace.json", None)
        mock_fetch.return_value = MarketplaceManifest(name="m", plugins=())
        result = runner.invoke(
            marketplace,
            ["add", "ssh://git@gitea.example.com:2222/org/repo.git"],
        )

        assert result.exit_code == 0, result.output
        assert "Pin this git marketplace" in result.output
        assert "--ref v1.0.0" in result.output
        assert "#v1.0.0" not in result.output

    @patch("apm_cli.marketplace.client.fetch_marketplace")
    @patch("apm_cli.marketplace.client._auto_detect_path_with_payload")
    def test_add_strips_dot_git_suffix(self, mock_detect, mock_fetch, runner):
        from apm_cli.commands.marketplace import marketplace

        mock_detect.return_value = ("marketplace.json", None)
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
    @patch("apm_cli.marketplace.client._auto_detect_path_with_payload")
    def test_add_accepts_nested_subpath_on_github_host(
        self, mock_detect, mock_fetch, runner, monkeypatch
    ):
        """N>=4 segments with FQDN first parse as host + multi-segment owner + repo."""
        from apm_cli.commands.marketplace import marketplace

        # Pretend ghes.corp.example.com is the configured GHES host so the
        # trusted-host gate accepts it. This is the realistic shape on which
        # nested sub-paths actually appear.
        monkeypatch.setenv("GITHUB_HOST", "ghes.corp.example.com")
        mock_detect.return_value = ("marketplace.json", None)
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
    @patch("apm_cli.marketplace.client._auto_detect_path_with_payload")
    def test_add_accepts_gitlab_com_https_url(self, mock_detect, mock_fetch, runner):
        """GitLab SaaS HTTPS URL passes registration and probes the GitLab host."""
        from apm_cli.commands.marketplace import marketplace

        mock_detect.return_value = ("marketplace.json", None)
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
    @patch("apm_cli.marketplace.client._auto_detect_path_with_payload")
    def test_add_accepts_gitlab_com_host_shorthand(self, mock_detect, mock_fetch, runner):
        """Host-qualified shorthand for gitlab.com passes the registration gate."""
        from apm_cli.commands.marketplace import marketplace

        mock_detect.return_value = ("marketplace.json", None)
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
    @patch("apm_cli.marketplace.client._auto_detect_path_with_payload")
    def test_add_accepts_self_managed_gitlab_with_gitlab_host_env(
        self, mock_detect, mock_fetch, runner, monkeypatch
    ):
        """Self-managed GitLab FQDN is accepted when GITLAB_HOST classifies it."""
        from apm_cli.commands.marketplace import marketplace

        monkeypatch.setenv("GITLAB_HOST", "git.epam.com")
        mock_detect.return_value = ("marketplace.json", None)
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
    @patch("apm_cli.marketplace.client._auto_detect_path_with_payload")
    def test_add_accepts_generic_host_via_git_fetcher(self, mock_detect, mock_fetch, runner):
        """Unknown FQDN is now accepted: kind=git routes through GitCache.

        Before this change, an unknown host was rejected with an explicit
        "not supported" error and a GHES/GitLab export hint. The trust
        gate now applies only to kinds that would forward an APM token
        (github/gitlab); the subprocess git path uses no APM credentials
        unless AuthResolver.classify_host recognises the host.
        """
        from apm_cli.commands.marketplace import marketplace

        mock_detect.return_value = ("marketplace.json", None)
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
    @patch("apm_cli.marketplace.client._auto_detect_path_with_payload")
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

        mock_detect.return_value = ("marketplace.json", None)
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
    @patch("apm_cli.marketplace.client._auto_detect_path_with_payload")
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

        mock_detect.return_value = ("marketplace.json", None)
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

    def test_json_sources_are_safe(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Sources cannot disclose URL secrets or internal cache locations."""
        from apm_cli.cli import cli
        from apm_cli.config import CONFIG_DIR

        monkeypatch.setattr("apm_cli.cli._check_and_notify_updates", lambda: None)
        cache = tmp_path / "download-cache"
        monkeypatch.setenv("APM_CACHE_DIR", str(cache))
        local = Path.home() / "catalog"
        monkeypatch.setattr(
            "apm_cli.marketplace.registry.get_registered_marketplaces",
            lambda: [
                SimpleNamespace(
                    name="remote",
                    url="https://person:secret@catalog.example.com/marketplace.json?token=hidden#private",
                    path="",
                    ref="",
                    kind="url",
                ),
                MarketplaceSource(name="local", url=str(local)),
                MarketplaceSource(name="file", url=local.as_uri()),
                SimpleNamespace(
                    name="ssh",
                    kind="git",
                    ref="",
                    path="",
                    url="ssh://person:secret@git.example.com/org/repo?token=hidden#private",
                ),
                SimpleNamespace(
                    name="scp",
                    kind="git",
                    ref="",
                    path="",
                    url="person@git.example.com:org/repo?token=hidden#private",
                ),
                SimpleNamespace(name="invalid", kind="local", url="file://[", ref="", path=""),
                MarketplaceSource(name="cached", url=str(cache / "checkout")),
                MarketplaceSource(
                    name="legacy", url=str(Path(CONFIG_DIR) / "cache" / "marketplace")
                ),
            ],
        )
        result = runner.invoke(cli, ["marketplace", "list", "-fjson"])
        assert result.exit_code == 0, result.output
        sources = {row["name"]: row["source"] for row in json.loads(result.stdout)["data"]}
        assert sources["local"] == "~/catalog"
        assert sources["file"] == "~/catalog"
        assert sources["cached"] is None
        assert sources["legacy"] is None
        assert sources["invalid"] is None
        remote = urlparse(sources["remote"])
        assert (remote.scheme, remote.hostname, remote.path) == (
            "https",
            "catalog.example.com",
            "/marketplace.json",
        )
        assert (remote.username, remote.password, remote.query, remote.fragment) == (
            None,
            None,
            "",
            "",
        )
        for key in ("ssh", "scp"):
            parsed = urlparse(sources[key])
            assert (parsed.scheme, parsed.hostname, parsed.path) == (
                "ssh",
                "git.example.com",
                "/org/repo",
            )
            assert (parsed.username, parsed.password, parsed.query, parsed.fragment) == (
                None,
                None,
                "",
                "",
            )
        assert result.stderr == ""
        assert not cache.exists()

    def test_json_unresolvable_source_is_null(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A local path that cannot be checked for cache containment fails closed."""
        from apm_cli.cli import cli

        original_resolve = Path.resolve
        denied = tmp_path / "unreadable"

        def resolve(path: Path, strict: bool = False) -> Path:
            if path == denied:
                raise OSError("permission denied")
            return original_resolve(path, strict=strict)

        monkeypatch.setattr("apm_cli.cli._check_and_notify_updates", lambda: None)
        monkeypatch.setattr(Path, "resolve", resolve)
        monkeypatch.setattr(
            "apm_cli.marketplace.registry.get_registered_marketplaces",
            lambda: [
                SimpleNamespace(name="denied", kind="local", url=str(denied), ref="", path="")
            ],
        )
        result = runner.invoke(cli, ["marketplace", "list", "-f", "json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["data"] == [
            {"name": "denied", "source": None, "ref": None, "path": None}
        ]
        assert result.stderr == ""

    def test_json_list_schema_and_order(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """JSON reports only the public fields, sorted without case sensitivity."""
        from apm_cli.cli import cli

        monkeypatch.setattr("apm_cli.cli._check_and_notify_updates", lambda: None)
        monkeypatch.setattr(
            "apm_cli.marketplace.registry.get_registered_marketplaces",
            lambda: [
                MarketplaceSource(name="Zebra", owner="acme", repo="plugins"),
                MarketplaceSource(name="alpha", ref="", path=""),
            ],
        )
        result = runner.invoke(cli, ["marketplace", "list", "--format", "json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout) == {
            "schema_version": 1,
            "data": [
                {"name": "alpha", "source": None, "ref": None, "path": None},
                {
                    "name": "Zebra",
                    "source": "acme/plugins",
                    "ref": "main",
                    "path": "marketplace.json",
                },
            ],
        }
        assert result.stderr == ""

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

    def test_json_browse_schema_and_stderr(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Browse emits full metadata without progress or notices on stdout."""
        from apm_cli.cli import cli
        from apm_cli.core.command_logger import CommandLogger

        monkeypatch.setattr(
            "apm_cli.cli._check_and_notify_updates",
            lambda: CommandLogger("update").warning("A newer version is available"),
        )
        monkeypatch.setattr(
            "apm_cli.marketplace.registry.get_marketplace_by_name",
            lambda name: MarketplaceSource(name=name, owner="acme", repo="plugins"),
        )
        monkeypatch.setattr(
            "apm_cli.marketplace.client.fetch_marketplace",
            lambda *args, **kwargs: MarketplaceManifest(
                name="acme",
                plugins=(
                    MarketplacePlugin(name="Zebra"),
                    MarketplacePlugin(
                        name="alpha", description="Long " + "description " * 12, version="1.2"
                    ),
                ),
            ),
        )
        result = runner.invoke(cli, ["marketplace", "browse", "acme", "-f", "json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout) == {
            "schema_version": 1,
            "data": [
                {
                    "name": "alpha",
                    "description": "Long " + "description " * 12,
                    "version": "1.2",
                    "install": "alpha@acme",
                },
                {"name": "Zebra", "description": None, "version": None, "install": "Zebra@acme"},
            ],
        }
        assert "\x1b" not in result.stdout
        assert "newer version" in result.stderr

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


@pytest.mark.parametrize("command", [["search"], ["marketplace", "search"]])
def test_json_search_preserves_limit_selection(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, command: list[str]
) -> None:
    """Both entrypoints sort only the selected results and keep full descriptions."""
    from apm_cli.cli import cli

    monkeypatch.setattr("apm_cli.cli._check_and_notify_updates", lambda: None)
    monkeypatch.setattr(
        "apm_cli.marketplace.registry.get_marketplace_by_name",
        lambda name: MarketplaceSource(name=name, owner="acme", repo="plugins"),
    )
    monkeypatch.setattr(
        "apm_cli.marketplace.client.search_marketplace",
        lambda query, source: [
            MarketplacePlugin(name="Zebra", description="Useful " * 20),
            MarketplacePlugin(name="beta"),
            MarketplacePlugin(name="aardvark"),
        ],
    )
    result = runner.invoke(cli, [*command, "query@acme", "--limit", "2", "--format=json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "schema_version": 1,
        "data": [
            {"name": "beta", "description": None, "install": "beta@acme"},
            {"name": "Zebra", "description": "Useful " * 20, "install": "Zebra@acme"},
        ],
    }
    assert "\x1b" not in result.stdout
    assert "Searching" in result.stderr


@pytest.mark.parametrize("command", [["search"], ["marketplace", "search"]])
@pytest.mark.parametrize(
    ("expression", "code"),
    [
        ("invalid", "invalid_search_expression"),
        ("@acme", "invalid_search_expression"),
        ("query@", "invalid_search_expression"),
        ("query@missing", "marketplace_not_found"),
        ("query@acme", "marketplace_search_failed"),
    ],
)
def test_json_search_handled_errors(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    command: list[str],
    expression: str,
    code: str,
) -> None:
    """Handled search failures emit stable error documents only on stderr."""
    from apm_cli.cli import cli
    from apm_cli.marketplace.errors import MarketplaceNotFoundError

    def lookup(name: str) -> MarketplaceSource:
        if name == "missing":
            raise MarketplaceNotFoundError(name)
        return MarketplaceSource(name=name, owner="acme", repo="plugins")

    monkeypatch.setattr("apm_cli.cli._check_and_notify_updates", lambda: None)
    monkeypatch.setattr("apm_cli.marketplace.registry.get_marketplace_by_name", lookup)
    monkeypatch.setattr(
        "apm_cli.marketplace.client.search_marketplace",
        MagicMock(side_effect=RuntimeError("private diagnostic")),
    )
    result = runner.invoke(cli, [*command, expression, "-f", "json", "--verbose"])
    assert result.exit_code == 1
    assert result.stdout == ""
    document = json.loads(result.stderr.strip().splitlines()[-1])
    assert document == {"schema_version": 1, "error": code}
    assert "private diagnostic" not in result.stderr


@pytest.mark.parametrize(
    "command",
    [
        ["marketplace", "list"],
        ["marketplace", "browse", "acme"],
        ["search", "query@acme"],
        ["marketplace", "search", "query@acme"],
    ],
)
def test_json_empty_results(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, command: list[str]
) -> None:
    """Every discovery command treats an empty result as a successful JSON list."""
    from apm_cli.cli import cli

    monkeypatch.setattr("apm_cli.cli._check_and_notify_updates", lambda: None)
    monkeypatch.setattr("apm_cli.marketplace.registry.get_registered_marketplaces", lambda: [])
    monkeypatch.setattr(
        "apm_cli.marketplace.registry.get_marketplace_by_name",
        lambda name: MarketplaceSource(name=name, owner="acme", repo="plugins"),
    )
    monkeypatch.setattr(
        "apm_cli.marketplace.client.fetch_marketplace",
        lambda *args, **kwargs: MarketplaceManifest(name="acme"),
    )
    monkeypatch.setattr("apm_cli.marketplace.client.search_marketplace", lambda *args: [])
    result = runner.invoke(cli, [*command, "-f", "json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"schema_version": 1, "data": []}


@pytest.mark.parametrize(
    ("command", "target", "error", "code"),
    [
        (
            ["marketplace", "list"],
            "apm_cli.marketplace.registry.get_registered_marketplaces",
            RuntimeError("private diagnostic"),
            "marketplace_list_failed",
        ),
        (
            ["marketplace", "browse", "missing"],
            "apm_cli.marketplace.registry.get_marketplace_by_name",
            MarketplaceNotFoundError("missing"),
            "marketplace_not_found",
        ),
        (
            ["marketplace", "browse", "broken"],
            "apm_cli.marketplace.registry.get_marketplace_by_name",
            KeyError("private diagnostic"),
            "marketplace_fetch_failed",
        ),
        (
            ["marketplace", "browse", "acme"],
            "apm_cli.marketplace.client.fetch_marketplace",
            RuntimeError("private diagnostic"),
            "marketplace_fetch_failed",
        ),
    ],
)
def test_json_list_browse_handled_errors(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    command: list[str],
    target: str,
    error: Exception,
    code: str,
) -> None:
    """List/browse failures keep stdout empty and never serialize raw exceptions."""
    from apm_cli.cli import cli

    monkeypatch.setattr("apm_cli.cli._check_and_notify_updates", lambda: None)
    monkeypatch.setattr(
        "apm_cli.marketplace.registry.get_marketplace_by_name",
        lambda name: MarketplaceSource(name=name, owner="acme", repo="plugins"),
    )
    monkeypatch.setattr(target, MagicMock(side_effect=error))
    result = runner.invoke(cli, [*command, "--format", "json", "--verbose"])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert json.loads(result.stderr.strip().splitlines()[-1]) == {
        "schema_version": 1,
        "error": code,
    }
    assert "private diagnostic" not in result.stderr


@pytest.mark.parametrize(
    "command",
    [
        ["marketplace", "browse", "acme"],
        ["search", "query@acme"],
        ["marketplace", "search", "query@acme"],
    ],
)
def test_json_metadata_sanitization(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, command: list[str], tmp_path: Path
) -> None:
    """Untrusted optional metadata cannot bypass source sanitization or the allowlist."""
    from apm_cli.cli import cli

    cache = tmp_path / "cache"
    monkeypatch.setenv("APM_CACHE_DIR", str(cache))
    description = (
        "See https://person:secret@docs.example.com/guide?token=hidden#private "
        f"and {cache / 'checkout'}"
    )
    plugins = (
        SimpleNamespace(
            name="plugin",
            description=description,
            version={"unexpected": "metadata"},
            secret="excluded",
        ),
        SimpleNamespace(name="missing", description=["unexpected"], version=None),
        SimpleNamespace(name="invalid", description="https://[broken", version=None),
        SimpleNamespace(name="embedded", description="See https://[broken please", version=None),
    )
    monkeypatch.setattr("apm_cli.cli._check_and_notify_updates", lambda: None)
    monkeypatch.setattr(
        "apm_cli.marketplace.registry.get_marketplace_by_name",
        lambda name: MarketplaceSource(name=name, owner="acme", repo="plugins"),
    )
    monkeypatch.setattr(
        "apm_cli.marketplace.client.fetch_marketplace",
        lambda *args, **kwargs: SimpleNamespace(plugins=plugins),
    )
    monkeypatch.setattr("apm_cli.marketplace.client.search_marketplace", lambda *args: plugins)
    result = runner.invoke(cli, [*command, "-f", "json"])
    assert result.exit_code == 0, result.output
    embedded, invalid, missing, plugin = json.loads(result.stdout)["data"]
    assert embedded["description"] == "See [redacted-url] please"
    assert invalid["description"] is None
    assert missing["description"] is None
    assert set(plugin) == (
        {"name", "description", "install", "version"}
        if "browse" in command
        else {"name", "description", "install"}
    )
    assert plugin["description"].endswith("and [internal-cache]")
    parsed = urlparse(plugin["description"].split()[1])
    assert (
        parsed.hostname,
        parsed.path,
        parsed.username,
        parsed.password,
        parsed.query,
        parsed.fragment,
    ) == ("docs.example.com", "/guide", None, None, "", "")
    if "browse" in command:
        assert plugin["version"] is None


@pytest.mark.parametrize(
    ("command", "headline"),
    [
        (["marketplace", "list"], "Registered Marketplaces"),
        (["marketplace", "browse", "acme"], "Plugins in 'acme'"),
        (["search", "query@acme"], "Search Results:"),
        (["marketplace", "search", "query@acme"], "Search Results:"),
    ],
)
@pytest.mark.parametrize("rich_output", [True, False])
def test_text_output_is_unchanged(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    command: list[str],
    headline: str,
    rich_output: bool,
) -> None:
    """The opt-in format option does not change default text tables or selection."""
    from apm_cli.cli import cli

    source = MarketplaceSource(name="acme", owner="acme", repo="plugins")
    plugins = (
        MarketplacePlugin(name="zebra", description="Useful"),
        MarketplacePlugin(name="Alpha"),
    )
    monkeypatch.setattr("apm_cli.cli._check_and_notify_updates", lambda: None)
    if not rich_output:
        monkeypatch.setattr("apm_cli.commands.marketplace._get_console", lambda: None)
    monkeypatch.setattr(
        "apm_cli.marketplace.registry.get_registered_marketplaces", lambda: [source]
    )
    monkeypatch.setattr("apm_cli.marketplace.registry.get_marketplace_by_name", lambda name: source)
    monkeypatch.setattr(
        "apm_cli.marketplace.client.fetch_marketplace",
        lambda *args, **kwargs: MarketplaceManifest(name="acme", plugins=plugins),
    )
    monkeypatch.setattr("apm_cli.marketplace.client.search_marketplace", lambda *args: plugins)
    default = runner.invoke(cli, command)
    explicit = runner.invoke(cli, [*command, "--format", "text"])
    assert default.exit_code == explicit.exit_code == 0
    assert default.stdout == explicit.stdout
    assert default.stderr == explicit.stderr == ""
    assert (headline if rich_output else "acme") in default.stdout
    if "list" not in command:
        assert default.stdout.index("zebra") < default.stdout.index("Alpha")


@pytest.mark.parametrize(
    "command",
    [
        ["marketplace", "list", "-f", "xml"],
        ["marketplace", "browse", "-f", "json"],
        ["search", "-f", "json"],
        ["marketplace", "search", "-f", "json"],
    ],
)
def test_json_parser_errors_remain_click_usage_errors(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, command: list[str]
) -> None:
    """Pre-callback parser errors retain Click's existing non-JSON boundary."""
    from apm_cli.cli import cli

    monkeypatch.setattr("apm_cli.cli._check_and_notify_updates", lambda: None)
    result = runner.invoke(cli, command)
    assert result.exit_code == 2
    assert result.stdout == ""
    assert "Usage:" in result.stderr
    assert "Error:" in result.stderr
    assert "schema_version" not in result.stderr


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
