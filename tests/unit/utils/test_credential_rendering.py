"""Credential value rendering owner and the output sites routed through it (#4171)."""

from __future__ import annotations

import warnings
from types import SimpleNamespace

import pytest

from apm_cli.deps.registry.auth import remediation_message
from apm_cli.deps.registry.client import RegistryError, _format_error
from apm_cli.deps.registry.resolver import RegistryPackageResolver, RegistryResolutionError
from apm_cli.deps.registry_proxy import RegistryConfig
from apm_cli.utils.credential_rendering import (
    mask_known_values,
    redact_url_userinfo,
    render_config_value,
)

_SECRET = "Sentinel0a1b2c3d4e5f6a7b8c9d"
_URL = f"https://alice:{_SECRET}@reg.example.invalid/apm"


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        ("registry.corp.token", _SECRET, "******** (set)"),
        ("registry.corp.token", None, "Not set"),
        ("registry.corp.token", "", "Not set"),
        ("registry.corp.url", _URL, "https://reg.example.invalid/apm"),
        ("registry.corp.url", None, "Not set"),
        ("registry.corp.default", True, "true"),
        (
            "mcp-registry-url",
            f"{_URL}?token={_SECRET}",
            "https://reg.example.invalid/apm?[REDACTED]",
        ),
        ("temp-dir", "/tmp/apm", "/tmp/apm"),
        ("external.skillspector.args", f"--x {_URL}", "--x https://reg.example.invalid/apm"),
    ],
)
def test_render_config_value(key: str, value: object, expected: str) -> None:
    assert render_config_value(key, value) == expected


@pytest.mark.parametrize(
    ("url", "drop_query", "expected"),
    [
        (_URL, False, "https://reg.example.invalid/apm"),
        ("https://u:p@[::1]:8443/x?q=1#f", False, "https://[::1]:8443/x?q=1#f"),
        (f"https://h.invalid/x?token={_SECRET}#f", True, "https://h.invalid/x#f"),
        ("https://h.invalid/x", True, "https://h.invalid/x"),
        ("https://u:p@:99999/x", False, "<redacted-url>"),
    ],
)
def test_redact_url_userinfo(url: str, drop_query: bool, expected: str) -> None:
    assert redact_url_userinfo(url, drop_query=drop_query) == expected


def test_mask_known_values_masks_longest_first() -> None:
    text = "a=abcd1234 b=abcd1234efgh5678"
    assert mask_known_values(text, ["abcd1234", "abcd1234efgh5678"]) == (
        "a=[REDACTED] b=[REDACTED]"
    )


def test_proxy_plaintext_warning_drops_url_userinfo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROXY_REGISTRY_URL", f"http://alice:{_SECRET}@proxy.invalid/art/apm")
    monkeypatch.setenv("PROXY_REGISTRY_TOKEN", _SECRET)
    monkeypatch.delenv("PROXY_REGISTRY_ALLOW_HTTP", raising=False)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        RegistryConfig.from_env()
    messages = [str(item.message) for item in caught]
    assert any("http://proxy.invalid/art/apm" in message for message in messages), messages
    assert not any(_SECRET in message for message in messages)


def test_registry_error_messages_drop_url_userinfo() -> None:
    message = _format_error(500, {"title": "boom"}, f"{_URL}/v1/packages")
    assert message == "registry HTTP 500 from https://reg.example.invalid/apm/v1/packages: boom"
    assert _SECRET not in remediation_message(_URL)
    assert "https://reg.example.invalid/apm" in remediation_message(_URL)

    dep_ref = SimpleNamespace(registry_name="corp", repo_url="acme/pkg", reference="1.0.0")
    exc = RegistryError("not found", status=404, url=f"{_URL}/v1/acme/pkg")
    with pytest.raises(RegistryResolutionError) as raised:
        RegistryPackageResolver._raise_for_http(None, exc, dep_ref, _URL)
    assert _SECRET not in str(raised.value)
    assert "https://reg.example.invalid/apm/v1/acme/pkg" in str(raised.value)
