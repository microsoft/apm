"""Schema-fidelity integration tests for the Claude Code MCP adapter.

These tests assert that ``ClaudeClientAdapter`` writes on-disk JSON
that is BYTE-EQUIVALENT to what the upstream ``claude`` CLI emits
when an end-user runs ``claude mcp add``.  The reference fixtures
were captured live from Claude Code 2.1.126; see
``tests/integration/fixtures/claude_mcp_golden/README.md`` for the
exact probe commands.

Why the golden-fixture approach (no ``claude`` CLI dependency in CI):
  * APM cannot take a runtime dependency on the ``claude`` binary in
    integration tests -- contributors must be able to run the suite
    without installing Claude Code.
  * Capturing the schema as static fixtures freezes the contract at
    a known Claude Code version.  When Claude Code ships a new
    schema, re-run the probe locally and refresh the fixtures.

Coverage matrix:
  * PROJECT scope, HTTP transport
  * PROJECT scope, SSE transport
  * PROJECT scope, stdio transport (env + args)
  * PROJECT scope, HTTP with auth headers
  * USER scope, HTTP transport
  * USER scope, stdio transport
  * LOCAL scope is intentionally NOT implemented; a guard test
    asserts that the adapter never writes under
    ``projects.<path>.mcpServers`` so we cannot regress into it.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from apm_cli.adapters.client.claude import ClaudeClientAdapter

FIXTURES = Path(__file__).parent / "fixtures" / "claude_mcp_golden"


def _load_golden(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.mark.integration
class TestClaudeProjectScopeSchemaFidelity:
    """``.mcp.json`` at project root, top-level ``mcpServers``."""

    def setup_method(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / ".claude").mkdir()
        self.adapter = ClaudeClientAdapter(project_root=str(self.root), user_scope=False)

    def teardown_method(self):
        self._tmp.cleanup()

    def test_http_transport_matches_claude_cli_output(self):
        golden = _load_golden("project_mcp.json")
        entry = golden["mcpServers"]["p-http"]
        ok = self.adapter.update_config({"p-http": entry})
        assert ok is True
        on_disk = json.loads((self.root / ".mcp.json").read_text(encoding="utf-8"))
        assert on_disk["mcpServers"]["p-http"] == entry

    def test_sse_transport_matches_claude_cli_output(self):
        golden = _load_golden("project_mcp.json")
        entry = golden["mcpServers"]["p-sse"]
        ok = self.adapter.update_config({"p-sse": entry})
        assert ok is True
        on_disk = json.loads((self.root / ".mcp.json").read_text(encoding="utf-8"))
        assert on_disk["mcpServers"]["p-sse"] == entry

    def test_stdio_transport_matches_claude_cli_output(self):
        golden = _load_golden("project_mcp.json")
        entry = golden["mcpServers"]["p-stdio"]
        ok = self.adapter.update_config({"p-stdio": entry})
        assert ok is True
        on_disk = json.loads((self.root / ".mcp.json").read_text(encoding="utf-8"))
        assert on_disk["mcpServers"]["p-stdio"] == entry

    def test_http_with_auth_headers_matches_claude_cli_output(self):
        golden = _load_golden("project_mcp.json")
        entry = golden["mcpServers"]["p-http-auth"]
        ok = self.adapter.update_config({"p-http-auth": entry})
        assert ok is True
        on_disk = json.loads((self.root / ".mcp.json").read_text(encoding="utf-8"))
        assert on_disk["mcpServers"]["p-http-auth"] == entry

    def test_full_golden_project_file_round_trips_byte_equivalent(self):
        """Write the entire golden fixture set; on-disk file must
        deep-equal the canonical Claude Code project config."""
        golden = _load_golden("project_mcp.json")
        ok = self.adapter.update_config(golden["mcpServers"])
        assert ok is True
        on_disk = json.loads((self.root / ".mcp.json").read_text(encoding="utf-8"))
        assert on_disk == golden


@pytest.mark.integration
class TestClaudeUserScopeSchemaFidelity:
    """``~/.claude.json`` top-level ``mcpServers``."""

    def setup_method(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.fake_home = self.root / "home"
        self.fake_home.mkdir()
        self._home_patch = mock.patch(
            "apm_cli.adapters.client.claude.Path.home",
            return_value=self.fake_home,
        )
        self._home_patch.start()
        self.adapter = ClaudeClientAdapter(project_root=str(self.root), user_scope=True)

    def teardown_method(self):
        self._home_patch.stop()
        self._tmp.cleanup()

    def test_http_transport_matches_claude_cli_output(self):
        golden = _load_golden("user_claude_mcpServers.json")
        entry = golden["mcpServers"]["u-http"]
        ok = self.adapter.update_config({"u-http": entry})
        assert ok is True
        on_disk = json.loads((self.fake_home / ".claude.json").read_text(encoding="utf-8"))
        assert on_disk["mcpServers"]["u-http"] == entry

    def test_stdio_transport_matches_claude_cli_output(self):
        golden = _load_golden("user_claude_mcpServers.json")
        entry = golden["mcpServers"]["u-stdio"]
        ok = self.adapter.update_config({"u-stdio": entry})
        assert ok is True
        on_disk = json.loads((self.fake_home / ".claude.json").read_text(encoding="utf-8"))
        assert on_disk["mcpServers"]["u-stdio"] == entry

    def test_full_golden_user_file_round_trips_byte_equivalent(self):
        """Write the full golden user-scope set; on-disk top-level
        ``mcpServers`` must deep-equal canonical Claude Code output."""
        golden = _load_golden("user_claude_mcpServers.json")
        ok = self.adapter.update_config(golden["mcpServers"])
        assert ok is True
        on_disk = json.loads((self.fake_home / ".claude.json").read_text(encoding="utf-8"))
        assert on_disk["mcpServers"] == golden["mcpServers"]

    def test_user_scope_writes_at_top_level_not_under_projects(self):
        """Regression guard: the LOCAL scope (default for ``claude
        mcp add``) writes under ``projects.<abs_path>.mcpServers``;
        APM intentionally targets USER scope (cross-project) when
        ``user_scope=True`` is requested.  Confirm we never collide
        with the LOCAL scope key path."""
        self.adapter.update_config(
            {"u-http": {"type": "http", "url": "https://example.invalid/mcp"}}
        )
        data = json.loads((self.fake_home / ".claude.json").read_text(encoding="utf-8"))
        assert "mcpServers" in data, "USER scope must write at top-level mcpServers"
        assert "projects" not in data, (
            "USER scope must not synthesize a `projects` key (that is the LOCAL scope key path)"
        )

    def test_user_scope_preserves_unrelated_top_level_keys(self):
        """``~/.claude.json`` is shared with Claude Code's full user
        config (auth tokens, project list, settings).  An APM write
        must not clobber those keys."""
        path = self.fake_home / ".claude.json"
        path.write_text(
            json.dumps(
                {
                    "userID": "abc-123",
                    "oauthAccount": {"token": "do-not-touch"},
                    "projects": {"/some/path": {"mcpServers": {"keep": {"command": "k"}}}},
                }
            ),
            encoding="utf-8",
        )
        ok = self.adapter.update_config(
            {"new": {"type": "http", "url": "https://example.invalid/mcp"}}
        )
        assert ok is True
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["userID"] == "abc-123"
        assert data["oauthAccount"] == {"token": "do-not-touch"}
        assert data["projects"]["/some/path"]["mcpServers"]["keep"] == {"command": "k"}
        assert data["mcpServers"]["new"] == {"type": "http", "url": "https://example.invalid/mcp"}


@pytest.mark.integration
class TestClaudeLocalScopeNotImplemented:
    """The LOCAL scope (Claude's default ``claude mcp add`` scope)
    writes per-project private config under
    ``~/.claude.json -> projects.<abs_path>.mcpServers``.  APM does
    NOT implement LOCAL scope -- APM packages are designed to be
    reproducible across teammates, which aligns with PROJECT (VCS)
    and USER (cross-project), not LOCAL (per-project private).

    These tests document the intentional omission so a future
    refactor cannot silently introduce a third scope mode.
    """

    def setup_method(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def teardown_method(self):
        self._tmp.cleanup()

    def test_no_local_scope_constructor_flag(self):
        """``ClaudeClientAdapter`` exposes ``user_scope`` only --
        no ``local_scope`` parameter, no third mode."""
        adapter = ClaudeClientAdapter(project_root=str(self.root), user_scope=False)
        assert hasattr(adapter, "user_scope")
        assert not hasattr(adapter, "local_scope")

    def test_default_construction_targets_project_scope(self):
        """Default ``user_scope=False`` resolves to project-scope
        (``.mcp.json``), NOT to local-scope under ``projects.<path>``
        in ``~/.claude.json``."""
        adapter = ClaudeClientAdapter(project_root=str(self.root), user_scope=False)
        cfg_path = Path(adapter.get_config_path())
        assert cfg_path.name == ".mcp.json"
        assert cfg_path.parent == self.root
        assert ".claude.json" not in str(cfg_path)
