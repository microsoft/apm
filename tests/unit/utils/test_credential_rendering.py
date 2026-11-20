"""Credential value rendering owner and the output sites routed through it (#4171)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from apm_cli.deps.registry.auth import remediation_message
from apm_cli.deps.registry.client import RegistryError, _format_error
from apm_cli.deps.registry.resolver import RegistryPackageResolver, RegistryResolutionError
from apm_cli.deps.registry_proxy import RegistryConfig
from apm_cli.models.apm_package import _parse_registries_block
from apm_cli.utils.credential_rendering import (
    mask_known_values,
    redact_secrets,
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
            "https://reg.example.invalid/apm?token=********",
        ),
        ("temp-dir", "/tmp/apm", "/tmp/apm"),
        (
            "registry.corp.url",
            f"https://{_SECRET}@reg.example.invalid/apm",
            "https://reg.example.invalid/apm",
        ),
        (
            "registry.corp.url",
            f"https://u:a:{_SECRET}@reg.example.invalid/apm",
            "https://reg.example.invalid/apm",
        ),
        (
            "registry.corp.url",
            f"https://reg.example.invalid/apm?sig={_SECRET}",
            "https://reg.example.invalid/apm?sig=********",
        ),
        (
            "registry.corp.url",
            f"https://reg.example.invalid/apm?%74oken={_SECRET}",
            "https://reg.example.invalid/apm?%74oken=********",
        ),
        (
            "external.skillspector.args",
            f"--endpoint={_URL}",
            "--endpoint=https://reg.example.invalid/apm",
        ),
        ("external.skillspector.args", f"--api-key {_SECRET}", "--api-key [REDACTED]"),
        ("external.skillspector.args", f"--api_key={_SECRET}", "--api_key=[REDACTED]"),
        ("external.skillspector.args", f"apikey: {_SECRET}", "[REDACTED]"),
        ("external.skillspector.args", f"--x {_URL}", "--x https://reg.example.invalid/apm"),
    ],
)
def test_render_config_value(key: str, value: object, expected: str) -> None:
    assert render_config_value(key, value) == expected


_SHORT = "pw12x"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (f"--api-key {_SHORT}", "--api-key [REDACTED]"),
        (f"--apikey={_SHORT}", "--apikey=[REDACTED]"),
        (f"--x-api-key {_SHORT} --verbose", "--x-api-key [REDACTED] --verbose"),
        (f"--token {_SHORT}", "--token [REDACTED]"),
        (f"--token={_SHORT}", "--token=[REDACTED]"),
        (f"--password {_SHORT}", "--password [REDACTED]"),
        (f"--password={_SHORT}", "--password=[REDACTED]"),
        (f"--secret {_SHORT}", "--secret [REDACTED]"),
        (f"--client-secret={_SHORT}", "--client-secret=[REDACTED]"),
        (f"--auth {_SHORT}", "--auth [REDACTED]"),
        (f"--auth={_SHORT}", "--auth=[REDACTED]"),
        (f'--token "{_SHORT}"', "--token [REDACTED]"),
        ("--author jane --no-auth --verbose", "--author jane --no-auth --verbose"),
    ],
)
def test_credential_flags_in_free_text_values_are_redacted(value: str, expected: str) -> None:
    rendered = render_config_value("external.skillspector.args", value)
    assert rendered == expected
    assert redact_secrets(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            f"https://h.invalid/x?access_token={_SHORT}&api_key={_SHORT}&key={_SHORT}",
            "https://h.invalid/x?access_token=********&api_key=********&key=********",
        ),
        (
            "https://h.invalid/x?token=%53%65%63ret%21&sig=%2Fabc",
            "https://h.invalid/x?token=********&sig=********",
        ),
        (
            f"--endpoint=https://h.invalid/x?sig={_SHORT} --mode fast",
            "--endpoint=https://h.invalid/x?sig=******** --mode fast",
        ),
    ],
)
def test_query_credentials_in_config_values_are_masked(value: str, expected: str) -> None:
    assert render_config_value("external.skillspector.args", value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            f"--endpoint=https://{_SECRET}@reg.example.invalid/apm",
            "--endpoint=https://reg.example.invalid/apm",
        ),
        (
            f"--endpoint=https://u:a:{_SHORT}@reg.example.invalid/apm --x",
            "--endpoint=https://reg.example.invalid/apm --x",
        ),
    ],
)
def test_userinfo_inside_equals_form_args_is_removed(value: str, expected: str) -> None:
    assert render_config_value("external.skillspector.args", value) == expected


@pytest.mark.parametrize(
    "text",
    [
        f"GET https://u:a:{_SHORT}@reg.example.invalid/apm failed",
        f"GET https://{_SECRET}@reg.example.invalid/apm failed",
    ],
)
def test_free_text_redaction_covers_colon_passwords_and_username_only_tokens(text: str) -> None:
    redacted = redact_secrets(text)
    assert _SHORT not in redacted
    assert _SECRET not in redacted
    assert "reg.example.invalid/apm failed" in redacted


@pytest.mark.parametrize(
    ("url", "drop_query", "expected"),
    [
        (_URL, False, "https://reg.example.invalid/apm"),
        ("https://u:p@[::1]:8443/x?q=1#f", False, "https://[::1]:8443/x?q=********#f"),
        (f"https://h.invalid/x?token={_SECRET}#f", True, "https://h.invalid/x#f"),
        (f"https://h.invalid/x?{_SECRET}", False, "https://h.invalid/x?********"),
        ("https://h.invalid/x", True, "https://h.invalid/x"),
        (
            f"https://h.invalid/cb#access_token={_SECRET}",
            False,
            "https://h.invalid/cb#access_token=********",
        ),
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
    from apm_cli.deps import registry_proxy

    monkeypatch.setenv("PROXY_REGISTRY_URL", f"http://alice:{_SECRET}@proxy.invalid/art/apm")
    monkeypatch.setenv("PROXY_REGISTRY_TOKEN", _SECRET)
    monkeypatch.delenv("PROXY_REGISTRY_ALLOW_HTTP", raising=False)
    monkeypatch.setattr(registry_proxy, "_emitted_warnings", set())
    messages: list[str] = []
    monkeypatch.setattr(
        "apm_cli.utils.console._rich_warning", lambda message, *a, **k: messages.append(message)
    )
    RegistryConfig.from_env()
    assert any("http://proxy.invalid/art/apm" in message for message in messages), messages
    assert not any(_SECRET in message for message in messages)


def test_proxy_plaintext_warning_masks_query_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    from apm_cli.deps import registry_proxy

    monkeypatch.setenv("PROXY_REGISTRY_URL", f"http://proxy.invalid/art/apm?token={_SECRET}")
    monkeypatch.setenv("PROXY_REGISTRY_TOKEN", _SECRET)
    monkeypatch.delenv("PROXY_REGISTRY_ALLOW_HTTP", raising=False)
    monkeypatch.setattr(registry_proxy, "_emitted_warnings", set())
    messages: list[str] = []
    monkeypatch.setattr(
        "apm_cli.utils.console._rich_warning", lambda message, *a, **k: messages.append(message)
    )
    RegistryConfig.from_env()
    assert any("http://proxy.invalid/art/apm?token=********" in message for message in messages), (
        messages
    )
    assert not any(_SECRET in message for message in messages)


def test_registry_and_manifest_errors_mask_query_credentials(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "apm_cli.deps.registry.feature_gate.require_package_registry_enabled",
        lambda *_args, **_kwargs: None,
    )
    message = _format_error(403, {"title": "denied"}, f"https://r.invalid/v1/x?sig={_SECRET}")
    assert message == "registry HTTP 403 from https://r.invalid/v1/x?sig=********: denied"
    assert _SECRET not in remediation_message(f"https://r.invalid/apm?access_token={_SECRET}")

    manifest = {"registries": {"corp": {"url": f"ftp://r.invalid/apm?token={_SECRET}"}}}
    with pytest.raises(ValueError) as raised:
        _parse_registries_block(manifest, tmp_path / "apm.yml", None)
    assert _SECRET not in str(raised.value)
    assert "ftp://r.invalid/apm?token=********" in str(raised.value)


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
