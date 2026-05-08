"""Unit tests for WindsurfClientAdapter.

Covers:
- get_config_path() returns the expected global config location
- Class attributes: supports_user_scope, _client_label
"""

from pathlib import Path

from apm_cli.adapters.client.windsurf import WindsurfClientAdapter


class TestWindsurfClientAdapterConfigPath:
    """WindsurfClientAdapter.get_config_path returns the global Codeium path."""

    def test_config_path_equals_codeium_windsurf(self, monkeypatch, tmp_path: Path) -> None:
        """Config path must be ~/.codeium/windsurf/mcp_config.json."""
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

        adapter = WindsurfClientAdapter(project_root=tmp_path)
        result = adapter.get_config_path()

        expected = str(fake_home / ".codeium" / "windsurf" / "mcp_config.json")
        assert result == expected

    def test_supports_user_scope_is_true(self) -> None:
        """Windsurf uses a global config path, so user-scope is supported."""
        assert WindsurfClientAdapter.supports_user_scope is True

    def test_client_label_is_windsurf(self) -> None:
        """The user-facing label should be 'Windsurf'."""
        assert WindsurfClientAdapter._client_label == "Windsurf"
