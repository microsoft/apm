"""The git-only TLS context refuses what git would refuse, and the listing falls back.

A local CONNECT proxy tunnels the in-process request to a local TLS server
presenting either a self-signed certificate or a certificate for another
host signed by the trusted CA. Both handshakes must be refused, and the
listing must return ``None`` so the caller runs git.
"""

from __future__ import annotations

import contextlib
import logging
import socket
import ssl
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from apm_cli.deps import anonymous_ref_listing as arl
from tests.utils.local_github_https_fixture import _mint_certificates, openssl_executable

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise("P-OWN-git-transport-selection"),
]

if openssl_executable() is None:
    pytest.skip("openssl CLI is required to mint certificates", allow_module_level=True)


def _serve(listener: socket.socket, handle) -> threading.Thread:
    def loop() -> None:
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            threading.Thread(target=handle, args=(conn,), daemon=True).start()

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return thread


def _pipe(src: socket.socket, dst: socket.socket) -> None:
    with contextlib.suppress(OSError):
        while chunk := src.recv(65536):
            dst.sendall(chunk)
    with contextlib.suppress(OSError):
        dst.shutdown(socket.SHUT_WR)


@contextlib.contextmanager
def _tls_origin_behind_proxy(certfile: Path, keyfile: Path) -> Iterator[tuple[str, int]]:
    """``(http:// proxy URL, origin port)``: every CONNECT reaches a TLS server with *certfile*."""
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(certfile=str(certfile), keyfile=str(keyfile))
    origin = socket.create_server(("127.0.0.1", 0))
    proxy = socket.create_server(("127.0.0.1", 0))
    origin_port = origin.getsockname()[1]

    def handshake(conn: socket.socket) -> None:
        with contextlib.suppress(OSError), conn:
            server_context.wrap_socket(conn, server_side=True).close()

    def tunnel(client: socket.socket) -> None:
        with contextlib.suppress(OSError), client:
            request = b""
            while b"\r\n\r\n" not in request:
                chunk = client.recv(4096)
                if not chunk:
                    return
                request += chunk
            client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            with socket.create_connection(("127.0.0.1", origin_port)) as upstream:
                back = threading.Thread(target=_pipe, args=(upstream, client), daemon=True)
                back.start()
                _pipe(client, upstream)
                back.join(5)

    _serve(origin, handshake)
    _serve(proxy, tunnel)
    try:
        yield f"http://127.0.0.1:{proxy.getsockname()[1]}", origin_port
    finally:
        origin.close()
        proxy.close()


def _listing_through(proxy_url: str, trusted_ca: Path, monkeypatch, caplog) -> str | None:
    child_env = {"HTTPS_PROXY": proxy_url, "GIT_SSL_CAINFO": str(trusted_ca)}
    monkeypatch.setitem(arl._config_cache, (), ({}, child_env, ()))
    with caplog.at_level(logging.DEBUG, logger="apm_cli.deps.anonymous_ref_listing"):
        return arl.list_refs_anonymously(
            "https://github.com/owner/repo", include_heads=False, env={}
        )


def test_a_self_signed_certificate_is_refused_and_git_runs(tmp_path, monkeypatch, caplog):
    trusted_ca, _leaf, _key = _mint_certificates(tmp_path / "trusted", ("github.com",))
    rogue_ca, _rogue_leaf, _rogue_key = _mint_certificates(tmp_path / "rogue", ("github.com",))

    with _tls_origin_behind_proxy(rogue_ca, tmp_path / "rogue" / "ca.key") as (proxy_url, _port):
        listed = _listing_through(proxy_url, trusted_ca, monkeypatch, caplog)

    assert listed is None
    assert "falls back to git: SSLError" in caplog.text


def test_a_certificate_for_another_host_is_refused_and_git_runs(tmp_path, monkeypatch, caplog):
    trusted_ca, wrong_leaf, wrong_key = _mint_certificates(
        tmp_path / "trusted", ("wrong.example.invalid",)
    )

    with _tls_origin_behind_proxy(wrong_leaf, wrong_key) as (proxy_url, _port):
        listed = _listing_through(proxy_url, trusted_ca, monkeypatch, caplog)

    assert listed is None
    assert "falls back to git: SSLError" in caplog.text


def test_a_ca_only_apm_trusts_is_refused_and_git_runs(tmp_path, monkeypatch, caplog):
    """APM's additive/OS trust accepts the server; git's CA set does not, so neither do we."""
    import sys

    import urllib3.util.ssl_ as urllib3_ssl

    from apm_cli.core import tls_trust

    apm_ca, leaf, key = _mint_certificates(tmp_path / "apm-only", ("github.com",))
    git_ca, _git_leaf, _git_key = _mint_certificates(tmp_path / "git", ("github.com",))
    monkeypatch.setitem(sys.modules, "truststore", None)
    recorded = (tls_trust._LAST_TLS_STATUS, tls_trust._LAST_TLS_SELECTION)
    state = tls_trust._capture_tls_publication_state()
    try:
        tls_trust.configure_tls_trust(env={"APM_EXTRA_CA_BUNDLE": str(apm_ca)})
        with _tls_origin_behind_proxy(leaf, key) as (proxy_url, origin_port):
            apm_context = urllib3_ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            with socket.create_connection(("127.0.0.1", origin_port), timeout=10) as raw:
                with apm_context.wrap_socket(raw, server_hostname="github.com") as tls:
                    accepted_by_apm = tls.getpeercert() is not None

            listed = _listing_through(proxy_url, git_ca, monkeypatch, caplog)
    finally:
        tls_trust._restore_tls_publication_state(state)
        tls_trust._LAST_TLS_STATUS, tls_trust._LAST_TLS_SELECTION = recorded

    assert accepted_by_apm
    assert listed is None
    assert "falls back to git: SSLError" in caplog.text
