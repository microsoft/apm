"""Direct unit tests for the resolve-and-pin SSRF connect guard.

Round-2 finding ``r2-http-1``: the up-front :func:`_ssrf_block_reason`
classifies a hostname's resolution, but ``requests``/``urllib3`` then
re-resolve the SAME name independently at connect time. A low-TTL name
can therefore answer a public address to the guard and an internal one
(``169.254.169.254``) to the socket -- a DNS-rebinding TOCTOU.

:func:`_ssrf_safe_connect` closes the window: it resolves ONCE, refuses
any internal result, and connects to the very address it validated.
These tests exercise that function directly (the dispatch-path tests
mock ``requests.post`` and so cannot reach the connection layer).
"""

from __future__ import annotations

import socket
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.core import script_executors
from apm_cli.core.script_executors import _ssrf_safe_connect, _SSRFConnectError

_GAI = "apm_cli.core.script_executors.socket.getaddrinfo"
_SOCK = "apm_cli.core.script_executors.socket.socket"


def _addrinfo(ip: str, port: int) -> list:
    """One IPv4 getaddrinfo tuple resolving to *ip*:*port*."""
    return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, port))]


def test_internal_resolution_raises_before_any_connect() -> None:
    """A name that resolves only to a link-local metadata IP is refused
    and NO socket is ever connected."""
    fake_sock = MagicMock(name="socket-instance")
    factory = MagicMock(name="socket-factory", return_value=fake_sock)

    with (
        patch(_GAI, return_value=_addrinfo("169.254.169.254", 80)),
        patch(_SOCK, factory),
    ):
        with pytest.raises(_SSRFConnectError):
            _ssrf_safe_connect(("metadata.rebind.example", 80))

    factory.assert_not_called()
    fake_sock.connect.assert_not_called()


def test_private_rfc1918_resolution_is_refused() -> None:
    """An RFC1918 answer at connect time is blocked, no connect attempted."""
    fake_sock = MagicMock()
    factory = MagicMock(return_value=fake_sock)

    with (
        patch(_GAI, return_value=_addrinfo("10.0.0.5", 443)),
        patch(_SOCK, factory),
    ):
        with pytest.raises(_SSRFConnectError):
            _ssrf_safe_connect(("internal.rebind.example", 443))

    fake_sock.connect.assert_not_called()


def test_public_resolution_connects_to_the_validated_sockaddr() -> None:
    """A public answer connects to EXACTLY the address that was validated."""
    fake_sock = MagicMock()
    factory = MagicMock(return_value=fake_sock)

    with (
        patch(_GAI, return_value=_addrinfo("93.184.216.34", 443)),
        patch(_SOCK, factory),
    ):
        result = _ssrf_safe_connect(("public.example.com", 443))

    assert result is fake_sock
    fake_sock.connect.assert_called_once_with(("93.184.216.34", 443))


def test_resolution_happens_exactly_once() -> None:
    """The pin must resolve ONCE; the validated answer is the connected one
    (a second independent resolution is the rebinding hole)."""
    fake_sock = MagicMock()
    factory = MagicMock(return_value=fake_sock)

    with (
        patch(_GAI, return_value=_addrinfo("93.184.216.34", 80)) as gai,
        patch(_SOCK, factory),
    ):
        _ssrf_safe_connect(("once.example.com", 80))

    assert gai.call_count == 1


def test_ssrf_connect_error_is_oserror_subclass() -> None:
    """``requests`` must see the refusal as an ordinary connection failure
    (logged status=error) rather than an unexpected exception type."""
    assert issubclass(_SSRFConnectError, OSError)


def test_timeout_is_applied_only_when_numeric() -> None:
    """A numeric timeout is set on the socket; a non-numeric sentinel is
    ignored (urllib3 may pass its global-default sentinel object)."""
    fake_sock = MagicMock()
    factory = MagicMock(return_value=fake_sock)

    with (
        patch(_GAI, return_value=_addrinfo("93.184.216.34", 443)),
        patch(_SOCK, factory),
    ):
        _ssrf_safe_connect(("public.example.com", 443), timeout=7.5)
    fake_sock.settimeout.assert_called_once_with(7.5)

    fake_sock2 = MagicMock()
    with (
        patch(_GAI, return_value=_addrinfo("93.184.216.34", 443)),
        patch(_SOCK, MagicMock(return_value=fake_sock2)),
    ):
        _ssrf_safe_connect(("public.example.com", 443), timeout=object())
    fake_sock2.settimeout.assert_not_called()


def test_guarded_session_builds_or_degrades_to_none() -> None:
    """``_get_guarded_session`` returns a usable session or ``None`` (the
    caller then falls back to bare ``requests.post``); it never raises."""
    session = script_executors._get_guarded_session()
    assert session is None or hasattr(session, "post")
