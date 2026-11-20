"""Unit tests for WindsurfClientAdapter (filesystem, process and CLI boundary cases)."""

from pathlib import Path

import pytest

from apm_cli.adapters.client.windsurf import WindsurfClientAdapter

pytestmark = pytest.mark.component


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
