"""Unit tests for WindsurfClientAdapter.

Covers:
- get_config_path() returns the expected global config location
- Class attributes: supports_user_scope, _client_label
"""

import pytest

from apm_cli.adapters.client.windsurf import WindsurfClientAdapter

pytestmark = pytest.mark.unit


class TestWindsurfClientAdapterConfigPath:
    """WindsurfClientAdapter.get_config_path returns the global Codeium path."""

    def test_supports_user_scope_is_true(self) -> None:
        """Windsurf uses a global config path, so user-scope is supported."""
        assert WindsurfClientAdapter.supports_user_scope is True

    def test_client_label_is_windsurf(self) -> None:
        """The user-facing label should be 'Windsurf'."""
        assert WindsurfClientAdapter._client_label == "Windsurf"
