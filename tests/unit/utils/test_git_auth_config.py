"""Regression tests for the canonical Git auth-config retain/reindex owner."""

import pytest

from apm_cli.utils.git_env import retain_non_auth_git_config_entries

pytestmark = pytest.mark.unit


def test_retain_non_auth_git_config_entries_filters_and_reindexes() -> None:
    """Real auth channels are removed while unrelated entries retain order."""
    env = {
        "GIT_CONFIG_COUNT": "4",
        "GIT_CONFIG_KEY_0": "http.sslCAInfo",
        "GIT_CONFIG_VALUE_0": "/authorization/corporate-ca.pem",
        "GIT_CONFIG_KEY_1": "http.extraHeader",
        "GIT_CONFIG_VALUE_1": "X-Harmless: value",
        "GIT_CONFIG_KEY_2": "custom.header",
        "GIT_CONFIG_VALUE_2": "  Authorization: Bearer secret",
        "GIT_CONFIG_KEY_3": "credential.interactive",
        "GIT_CONFIG_VALUE_3": "never",
    }

    retained_count = retain_non_auth_git_config_entries(env)

    assert retained_count == 2
    assert env == {
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "http.sslCAInfo",
        "GIT_CONFIG_VALUE_0": "/authorization/corporate-ca.pem",
        "GIT_CONFIG_KEY_1": "credential.interactive",
        "GIT_CONFIG_VALUE_1": "never",
    }


@pytest.mark.parametrize("count", ["", "not-a-number", "-1"])
def test_retain_non_auth_git_config_entries_tolerates_invalid_count(count: str) -> None:
    """Invalid counts expose no entries and orphaned indexed values are scrubbed."""
    env = {
        "GIT_CONFIG_COUNT": count,
        "GIT_CONFIG_KEY_7": "http.extraheader",
        "GIT_CONFIG_VALUE_7": "Authorization: Bearer orphaned-secret",
        "OTHER": "preserved",
    }

    assert retain_non_auth_git_config_entries(env) == 0
    assert env == {"OTHER": "preserved"}
