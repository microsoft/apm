"""Tests for indexed Git config auth-channel handling."""

from apm_cli.utils.git_env import (
    is_git_auth_channel_entry,
    retain_and_reindex_git_config,
)


def _indexed_entries(env: dict[str, str]) -> list[tuple[str, str]]:
    """Return indexed Git config entries in declared order."""
    return [
        (
            env[f"GIT_CONFIG_KEY_{index}"],
            env[f"GIT_CONFIG_VALUE_{index}"],
        )
        for index in range(int(env.get("GIT_CONFIG_COUNT", "0")))
    ]


def test_retain_and_reindex_git_config_preserves_safe_entries_and_drops_auth() -> None:
    """Only inherited auth channels are removed before entries are re-indexed."""
    env = {
        "GIT_CONFIG_COUNT": "4",
        "GIT_CONFIG_KEY_0": "http.sslCAInfo",
        "GIT_CONFIG_VALUE_0": "/authorization/corporate-ca.pem",
        "GIT_CONFIG_KEY_1": "http.extraHeader",
        "GIT_CONFIG_VALUE_1": "X-Harmless: value",
        "GIT_CONFIG_KEY_2": "custom.policy",
        "GIT_CONFIG_VALUE_2": "X-Custom: authorization=reviewed",
        "GIT_CONFIG_KEY_3": "custom.header",
        "GIT_CONFIG_VALUE_3": "Authorization: Bearer stale",
        "GIT_CONFIG_KEY_9": "stale.orphan",
        "GIT_CONFIG_VALUE_9": "must be removed",
    }

    retain_and_reindex_git_config(
        env,
        additional_entries=(("http.extraheader", "Authorization: Bearer fresh"),),
    )

    assert _indexed_entries(env) == [
        ("http.sslCAInfo", "/authorization/corporate-ca.pem"),
        ("custom.policy", "X-Custom: authorization=reviewed"),
        ("http.extraheader", "Authorization: Bearer fresh"),
    ]
    assert "GIT_CONFIG_KEY_9" not in env
    assert "GIT_CONFIG_VALUE_9" not in env


def test_retain_and_reindex_git_config_tolerates_invalid_count() -> None:
    """Blank, invalid, and negative counts do not leave indexed state behind."""
    for count in ("", "not-a-number", "-3"):
        env = {
            "GIT_CONFIG_COUNT": count,
            "GIT_CONFIG_KEY_0": "http.extraheader",
            "GIT_CONFIG_VALUE_0": "Authorization: Bearer stale",
        }

        retain_and_reindex_git_config(env)

        assert "GIT_CONFIG_COUNT" not in env
        assert "GIT_CONFIG_KEY_0" not in env
        assert "GIT_CONFIG_VALUE_0" not in env


def test_is_git_auth_channel_entry_uses_exact_auth_policy() -> None:
    """Authorization-like text in safe values must not be treated as auth."""
    assert is_git_auth_channel_entry("http.extraHeader", "X-Harmless: value")
    assert is_git_auth_channel_entry("custom.header", "Authorization: Bearer secret")
    assert not is_git_auth_channel_entry("http.sslCAInfo", "/authorization/corporate-ca.pem")
    assert not is_git_auth_channel_entry("custom.policy", "X-Custom: authorization=reviewed")
