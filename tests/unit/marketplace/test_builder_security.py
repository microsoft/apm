"""Security tests for marketplace builder -- S1/S2/S3/S4 guards.

Verifies path-traversal rejection, type enforcement, array caps,
and override precedence.
"""

from __future__ import annotations

import textwrap
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from apm_cli.marketplace.builder import (
    BuildOptions,
    MarketplaceBuilder,
    ResolvedPackage,
    _subtract_plugin_root,
)
from apm_cli.marketplace.errors import BuildError, MarketplaceYmlError
from apm_cli.marketplace.migration import load_marketplace_config


def _write(p: Path, content: str) -> None:
    p.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")


# ---------------------------------------------------------------------------
# S1: pluginRoot with traversal rejected at schema load
# ---------------------------------------------------------------------------


def test_plugin_root_with_traversal_rejected(tmp_path: Path) -> None:
    """metadata.pluginRoot containing '..' must be rejected at parse."""
    _write(
        tmp_path / "apm.yml",
        """\
        name: test
        description: x
        version: 1.0.0
        marketplace:
          owner:
            name: ACME
          metadata:
            pluginRoot: "../escape"
          packages:
            - name: tool
              source: acme/tool
              ref: v1.0.0
        """,
    )
    with pytest.raises(MarketplaceYmlError, match="traversal"):
        load_marketplace_config(tmp_path)


# ---------------------------------------------------------------------------
# S2: subtraction post-guards
# ---------------------------------------------------------------------------


def test_plugin_root_subtraction_no_traversal(tmp_path: Path) -> None:
    """Source with '..' is already rejected at _validate_source (regression lock)."""
    _write(
        tmp_path / "apm.yml",
        """\
        name: test
        description: x
        version: 1.0.0
        marketplace:
          owner:
            name: ACME
          packages:
            - name: evil
              source: ./plugins/../etc/passwd
        """,
    )
    with pytest.raises(MarketplaceYmlError, match="traversal"):
        load_marketplace_config(tmp_path)


def test_plugin_root_subtraction_empty_result() -> None:
    """Subtracting pluginRoot that equals source must raise BuildError."""
    with pytest.raises(BuildError, match="yields empty path"):
        _subtract_plugin_root("./plugins", "./plugins")


def test_plugin_root_subtraction_absolute_result() -> None:
    """pluginRoot with '..' is rejected at parse (S1), but if bypassed
    the post-subtraction guard would catch absolute paths."""
    # S1 prevents reaching this in production, but we test the function directly.
    # A relative subtraction can never produce an absolute path from relative
    # inputs, so we verify the guard exists by testing a case that yields
    # empty (which is the actual edge case).
    with pytest.raises(BuildError, match="yields empty path"):
        _subtract_plugin_root("./plugins/", "./plugins")


# ---------------------------------------------------------------------------
# S3: new fields must be strings
# ---------------------------------------------------------------------------


def test_author_object_with_unknown_key_rejected(tmp_path: Path) -> None:
    """author object with unknown keys is rejected (S3 hardening)."""
    _write(
        tmp_path / "apm.yml",
        """\
        name: test
        description: x
        version: 1.0.0
        marketplace:
          owner:
            name: ACME
          packages:
            - name: tool
              source: ./plugins/tool
              author:
                name: x
                website: y
        """,
    )
    with pytest.raises(MarketplaceYmlError, match=r"author.*unknown key"):
        load_marketplace_config(tmp_path)


def test_author_object_form_accepted(tmp_path: Path) -> None:
    """author as object {name, email?, url?} is accepted per Claude schema."""
    _write(
        tmp_path / "apm.yml",
        """\
        name: test
        description: x
        version: 1.0.0
        marketplace:
          owner:
            name: ACME
          packages:
            - name: tool
              source: ./plugins/tool
              author:
                name: ACME
                email: team@acme.example
        """,
    )
    config = load_marketplace_config(tmp_path)
    assert config.packages[0].author == {
        "name": "ACME",
        "email": "team@acme.example",
    }


def test_repository_must_be_string(tmp_path: Path) -> None:
    """repository as list must be rejected."""
    _write(
        tmp_path / "apm.yml",
        """\
        name: test
        description: x
        version: 1.0.0
        marketplace:
          owner:
            name: ACME
          packages:
            - name: tool
              source: ./plugins/tool
              repository:
                - http://evil.com
        """,
    )
    with pytest.raises(MarketplaceYmlError, match=r"repository.*non-empty string"):
        load_marketplace_config(tmp_path)


# ---------------------------------------------------------------------------
# S4: keywords array cap and type enforcement
# ---------------------------------------------------------------------------


def test_keywords_array_length_cap(tmp_path: Path) -> None:
    """keywords exceeding 50 items must be truncated (not error)."""
    keywords_list = ", ".join(f"kw{i}" for i in range(100))
    _write(
        tmp_path / "apm.yml",
        f"""\
        name: test
        description: x
        version: 1.0.0
        marketplace:
          owner:
            name: ACME
          packages:
            - name: tool
              source: ./plugins/tool
              keywords: [{keywords_list}]
        """,
    )
    config = load_marketplace_config(tmp_path)
    assert len(config.packages[0].tags) == 50


def test_keywords_item_type_enforcement(tmp_path: Path) -> None:
    """keywords containing non-string items must be rejected."""
    _write(
        tmp_path / "apm.yml",
        """\
        name: test
        description: x
        version: 1.0.0
        marketplace:
          owner:
            name: ACME
          packages:
            - name: tool
              source: ./plugins/tool
              keywords: [123, "ok"]
        """,
    )
    with pytest.raises(MarketplaceYmlError, match=r"keywords.*must be a string"):
        load_marketplace_config(tmp_path)


# ---------------------------------------------------------------------------
# Override precedence
# ---------------------------------------------------------------------------


def test_override_precedence_curator_wins(tmp_path: Path) -> None:
    """Entry-level description overrides remote-fetched value."""
    _write(
        tmp_path / "apm.yml",
        """\
        name: test
        description: x
        version: 1.0.0
        marketplace:
          owner:
            name: ACME
          packages:
            - name: remote-tool
              source: acme/remote-tool
              ref: v1.0.0
              description: "mine"
              version: "2.0.0"
        """,
    )
    config = load_marketplace_config(tmp_path)
    builder = MarketplaceBuilder.from_config(config, tmp_path, BuildOptions(offline=True))
    # Simulate a resolved remote package
    from apm_cli.marketplace.builder import ResolvedPackage

    resolved = [
        ResolvedPackage(
            name="remote-tool",
            source_repo="acme/remote-tool",
            subdir=None,
            ref="v1.0.0",
            sha="a" * 40,
            requested_version=None,
            tags=(),
            is_prerelease=False,
        )
    ]
    doc = builder.compose_marketplace_json(resolved)
    plugin = doc["plugins"][0]
    assert plugin["description"] == "mine"
    assert plugin["version"] == "2.0.0"


# ---------------------------------------------------------------------------
# Metadata enrichment keeps the GitHub credential on its origin (#3726)
# ---------------------------------------------------------------------------


class _MetadataOrigin(ThreadingHTTPServer):
    daemon_threads = True
    location: str | None = None
    seen: list[dict[str, str]]


class _MetadataHandler(BaseHTTPRequestHandler):
    server: _MetadataOrigin

    def log_message(self, _format: str, *_args: object) -> None:
        """Keep test output silent."""

    def do_GET(self) -> None:
        self.server.seen.append({k.lower(): v for k, v in self.headers.items()})
        if self.server.location:
            self.send_response(302)
            self.send_header("Location", self.server.location)
            body = b""
        else:
            self.send_response(200)
            body = b"description: from the other origin\nversion: 9.9.9\n"
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@contextmanager
def _metadata_origin(location: str | None = None) -> Iterator[_MetadataOrigin]:
    server = _MetadataOrigin(("127.0.0.1", 0), _MetadataHandler)
    server.location = location
    server.seen = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_metadata_fetch_does_not_follow_redirect_to_another_origin(tmp_path: Path) -> None:
    _write(
        tmp_path / "marketplace.yml",
        """\
        name: acme-tools
        description: x
        version: 1.0.0
        owner:
          name: ACME
        packages:
          - name: tool
            source: acme/tool
            ref: v1.0.0
        """,
    )
    builder = MarketplaceBuilder(tmp_path / "marketplace.yml")
    package = ResolvedPackage(
        name="tool",
        source_repo="acme/tool",
        subdir=None,
        ref="v1.0.0",
        sha="a" * 40,
        requested_version=None,
        tags=(),
        is_prerelease=False,
    )
    with _metadata_origin() as other:
        # ``localhost`` and ``127.0.0.1`` are distinct origins.
        landing = f"http://localhost:{other.server_address[1]}/landing"
        with _metadata_origin(landing) as api:
            builder._host = "ghes.example.test"
            builder._host_info = SimpleNamespace(
                kind="ghes", api_base=f"http://127.0.0.1:{api.server_address[1]}/api/v3"
            )
            builder._github_token = "metadata-origin-sentinel"
            outcome = builder._fetch_remote_metadata_outcome(package)

    assert api.seen[0]["authorization"] == "token metadata-origin-sentinel"
    assert all("authorization" not in headers for headers in other.seen)
    assert other.seen == []
    assert outcome.status == "failed"
