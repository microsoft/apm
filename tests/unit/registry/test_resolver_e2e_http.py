"""End-to-end registry-resolver tests against a real local HTTP server.

Stronger guarantee than the MagicMock-based tests in ``test_resolver.py``:
this exercises the full ``RegistryClient`` -> network -> ``RegistryClient``
parsing path. We spin up a stdlib ``http.server.HTTPServer`` in a thread,
register fake routes, and run the resolver against ``http://localhost:<port>``.

Tested:
- Happy path with tar.gz archive
- Happy path with zip archive (Anthropic skills format)
- 404 surfaces a clear "no package" message
- 401 surfaces the §6.2 remediation (auth)
- Tarball with sha256 mismatch fails closed before any extraction
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import socket
import tarfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable, Dict

import pytest

from apm_cli.deps.registry.resolver import (
    RegistryPackageResolver,
    RegistryResolutionError,
)
from apm_cli.models.dependency.reference import DependencyReference


# ───────────────────────────────────────────────────────────────────────────
# Test HTTP server scaffolding
# ───────────────────────────────────────────────────────────────────────────


class _FakeRegistryHandler(BaseHTTPRequestHandler):
    """Per-request handler. Routes installed via class-level ``ROUTES``."""

    ROUTES: Dict[str, Callable] = {}

    # Suppress noisy default logging
    def log_message(self, format, *args):  # noqa: A002 — match stdlib signature
        pass

    def do_GET(self):  # noqa: N802 — stdlib naming
        route = self.ROUTES.get(self.path)
        if route is None:
            self.send_response(404)
            self.send_header("Content-Type", "application/problem+json")
            body = json.dumps(
                {"title": "Not Found", "detail": f"no route {self.path}"}
            ).encode()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        # Routes return (status, content_type, body_bytes)
        status, ctype, body = route(self)
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@contextlib.contextmanager
def _running_server(routes: Dict[str, Callable]):
    """Yield a (host, port) tuple for a running HTTP server with *routes* installed."""
    # Find a free port
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    # Patch routes onto the handler class. We use a unique handler subclass
    # per test to avoid cross-test contamination.
    handler_cls = type(
        "_PerTestHandler",
        (_FakeRegistryHandler,),
        {"ROUTES": dict(routes)},
    )
    server = HTTPServer(("127.0.0.1", port), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield ("127.0.0.1", port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ───────────────────────────────────────────────────────────────────────────
# Archive helpers
# ───────────────────────────────────────────────────────────────────────────


def _build_apm_tarball():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        apm_yml = b"name: acme-web\nversion: 1.0.0\ndescription: x\nauthor: a\n"
        ti = tarfile.TarInfo("apm.yml")
        ti.size = len(apm_yml)
        tar.addfile(ti, io.BytesIO(apm_yml))
        keep = tarfile.TarInfo(".apm/.keep")
        keep.size = 0
        tar.addfile(keep, io.BytesIO(b""))
    data = buf.getvalue()
    return data, hashlib.sha256(data).hexdigest()


def _build_apm_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "apm.yml", "name: acme-skill\nversion: 1.0.0\ndescription: x\nauthor: a\n"
        )
        zf.writestr(".apm/.keep", "")
    data = buf.getvalue()
    return data, hashlib.sha256(data).hexdigest()


def _make_dep(name: str = "acme/web") -> DependencyReference:
    return DependencyReference(
        repo_url=name,
        reference="^1.0",
        source="registry",
        registry_name="corp",
    )


def _versions_payload(name: str, version: str, digest_hex: str) -> bytes:
    return json.dumps(
        {
            "package": name,
            "versions": [
                {
                    "version": version,
                    "digest": f"sha256:{digest_hex}",
                    "published_at": "2026-01-01T00:00:00Z",
                }
            ],
        }
    ).encode()


# ───────────────────────────────────────────────────────────────────────────
# Tests
# ───────────────────────────────────────────────────────────────────────────


class TestE2EHappyPath:
    def test_install_tar_gz(self, tmp_path):
        data, digest = _build_apm_tarball()
        routes = {
            "/v1/packages/acme/web/versions": lambda h: (
                200,
                "application/json",
                _versions_payload("acme/web", "1.0.0", digest),
            ),
            "/v1/packages/acme/web/versions/1.0.0/download": lambda h: (
                200,
                "application/gzip",
                data,
            ),
        }
        with _running_server(routes) as (host, port):
            base = f"http://{host}:{port}"
            resolver = RegistryPackageResolver({"corp": base})
            target = tmp_path / "apm_modules" / "acme" / "web"
            info = resolver.download_package(_make_dep(), target)
            assert (target / "apm.yml").exists()
            assert (target / ".apm").is_dir()
            res = resolver.last_resolutions[_make_dep().get_unique_key()]
            assert res.version == "1.0.0"
            assert res.resolved_hash == f"sha256:{digest}"
            assert res.resolved_url.endswith("/versions/1.0.0/download")

    def test_install_zip(self, tmp_path):
        data, digest = _build_apm_zip()
        routes = {
            "/v1/packages/acme/skill/versions": lambda h: (
                200,
                "application/json",
                _versions_payload("acme/skill", "1.0.0", digest),
            ),
            "/v1/packages/acme/skill/versions/1.0.0/download": lambda h: (
                200,
                "application/zip",
                data,
            ),
        }
        with _running_server(routes) as (host, port):
            base = f"http://{host}:{port}"
            resolver = RegistryPackageResolver({"corp": base})
            target = tmp_path / "apm_modules" / "acme" / "skill"
            info = resolver.download_package(_make_dep("acme/skill"), target)
            assert (target / "apm.yml").exists()
            assert (target / ".apm").is_dir()


class TestE2EErrorPaths:
    def test_404_no_package(self, tmp_path):
        # No routes — every GET returns 404.
        with _running_server({}) as (host, port):
            base = f"http://{host}:{port}"
            resolver = RegistryPackageResolver({"corp": base})
            with pytest.raises(RegistryResolutionError, match="no package"):
                resolver.download_package(
                    _make_dep("acme/missing"), tmp_path / "p"
                )

    def test_401_surfaces_remediation(self, tmp_path):
        routes = {
            "/v1/packages/acme/web/versions": lambda h: (
                401,
                "application/problem+json",
                json.dumps(
                    {"title": "Unauthorized", "detail": "missing token"}
                ).encode(),
            )
        }
        with _running_server(routes) as (host, port):
            base = f"http://{host}:{port}"
            resolver = RegistryPackageResolver({"corp": base})
            with pytest.raises(RegistryResolutionError) as excinfo:
                resolver.download_package(_make_dep(), tmp_path / "p")
            msg = str(excinfo.value)
            assert "APM_REGISTRY_TOKEN_<NAME>" in msg
            assert base in msg

    def test_hash_mismatch_fails_closed(self, tmp_path):
        data, _real_digest = _build_apm_tarball()
        bogus = "0" * 64  # advertise a wrong digest
        routes = {
            "/v1/packages/acme/web/versions": lambda h: (
                200,
                "application/json",
                _versions_payload("acme/web", "1.0.0", bogus),
            ),
            "/v1/packages/acme/web/versions/1.0.0/download": lambda h: (
                200,
                "application/gzip",
                data,
            ),
        }
        with _running_server(routes) as (host, port):
            base = f"http://{host}:{port}"
            resolver = RegistryPackageResolver({"corp": base})
            with pytest.raises(Exception) as excinfo:
                resolver.download_package(_make_dep(), tmp_path / "p")
            # Either HashMismatchError directly or wrapped in RegistryResolutionError
            assert (
                "mismatch" in str(excinfo.value).lower()
                or "Hash" in type(excinfo.value).__name__
            )

    def test_no_matching_version(self, tmp_path):
        data, digest = _build_apm_tarball()
        routes = {
            # Only 2.x available; manifest asks for ^1.0
            "/v1/packages/acme/web/versions": lambda h: (
                200,
                "application/json",
                _versions_payload("acme/web", "2.0.0", digest),
            ),
        }
        with _running_server(routes) as (host, port):
            base = f"http://{host}:{port}"
            resolver = RegistryPackageResolver({"corp": base})
            with pytest.raises(RegistryResolutionError, match="no version"):
                resolver.download_package(_make_dep(), tmp_path / "p")


class TestE2EAuthHeader:
    """Confirm Bearer header is forwarded and observed by the server."""

    def test_token_sent_when_env_set(self, tmp_path, monkeypatch):
        monkeypatch.setenv("APM_REGISTRY_TOKEN_CORP", "secret-token-123")
        data, digest = _build_apm_tarball()
        seen_headers: dict = {}

        def versions_route(h):
            seen_headers["versions"] = dict(h.headers)
            return (
                200,
                "application/json",
                _versions_payload("acme/web", "1.0.0", digest),
            )

        def download_route(h):
            seen_headers["download"] = dict(h.headers)
            return (200, "application/gzip", data)

        routes = {
            "/v1/packages/acme/web/versions": versions_route,
            "/v1/packages/acme/web/versions/1.0.0/download": download_route,
        }
        with _running_server(routes) as (host, port):
            base = f"http://{host}:{port}"
            resolver = RegistryPackageResolver({"corp": base})
            resolver.download_package(_make_dep(), tmp_path / "x")
        assert (
            seen_headers["versions"].get("Authorization")
            == "Bearer secret-token-123"
        )
        assert (
            seen_headers["download"].get("Authorization")
            == "Bearer secret-token-123"
        )

    def test_anonymous_when_no_env(self, tmp_path, monkeypatch):
        monkeypatch.delenv("APM_REGISTRY_TOKEN_CORP", raising=False)
        data, digest = _build_apm_tarball()
        seen_headers: dict = {}

        def route(h):
            seen_headers["versions"] = dict(h.headers)
            return (
                200,
                "application/json",
                _versions_payload("acme/web", "1.0.0", digest),
            )

        routes = {
            "/v1/packages/acme/web/versions": route,
            "/v1/packages/acme/web/versions/1.0.0/download": lambda h: (
                200,
                "application/gzip",
                data,
            ),
        }
        with _running_server(routes) as (host, port):
            base = f"http://{host}:{port}"
            resolver = RegistryPackageResolver({"corp": base})
            resolver.download_package(_make_dep(), tmp_path / "x")
        assert seen_headers["versions"].get("Authorization") is None
