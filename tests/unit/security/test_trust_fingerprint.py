"""Fingerprint owner contract (#3902): one hasher, pinned inputs, full length.

On-disk hook and plugin fingerprint cases live in test_trust_admission.py.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from apm_cli.models.dependency.mcp import MCPDependency
from apm_cli.security.trust_fingerprint import (
    UnfingerprintableError,
    canonical_digest,
    definition_fingerprint,
    evidence_fingerprint,
    is_fingerprint,
    mcp_definition_fingerprint,
    short_fingerprint,
)

pytestmark = pytest.mark.unit

_SERVER = {
    "name": "search-index",
    "registry": False,
    "transport": "stdio",
    "command": "npx",
    "args": ["-y", "@acme/search-index@2.1.0"],
    "env": {"ACME_TOKEN": "${ACME_TOKEN}"},
}


def _server(**overrides) -> MCPDependency:
    return MCPDependency.from_dict({**_SERVER, **overrides})


def test_mcp_fingerprint_is_golden() -> None:
    """Changing the pinned field list re-gates every approval, so it is frozen."""
    assert mcp_definition_fingerprint(_server()) == (
        "sha256:dda4c5f6612b40c5deb8acfe43dd3c6861c1926a0db0de02c6abf2152ef11c89"
    )


@pytest.mark.parametrize(
    "change",
    [
        {"command": "uvx"},
        {"args": ["-y", "@acme/search-index@2.2.0"]},
        {"env": {"ACME_TOKEN": "literal-value"}},
        {"env": {"ACME_TOKEN": "${ACME_TOKEN}", "NODE_OPTIONS": "--require ./x.js"}},
        {"tools": ["read"]},
        {"cwd": "/tmp"},
    ],
    ids=["command", "args", "env-value", "env-key", "tools", "cwd"],
)
def test_every_definition_field_changes_the_fingerprint(change: dict) -> None:
    assert mcp_definition_fingerprint(_server(**change)) != mcp_definition_fingerprint(_server())


def test_remote_server_binds_url_and_headers() -> None:
    remote = {"name": "r", "registry": False, "transport": "http", "url": "https://a/mcp"}
    base = mcp_definition_fingerprint(MCPDependency.from_dict(remote))
    assert base != mcp_definition_fingerprint(
        MCPDependency.from_dict({**remote, "url": "https://b/mcp"})
    )
    assert base != mcp_definition_fingerprint(
        MCPDependency.from_dict({**remote, "headers": {"Authorization": "Bearer ${T}"}})
    )


def test_registry_entry_is_part_of_the_binding() -> None:
    overlay = MCPDependency.from_dict({"name": "io.acme/search", "env": {"K": "v"}})
    one = mcp_definition_fingerprint(overlay, registry_entry={"name": "s", "version": "1"})
    two = mcp_definition_fingerprint(overlay, registry_entry={"name": "s", "version": "2"})
    assert one != two != mcp_definition_fingerprint(overlay)


def test_kinds_and_items_are_domain_separated() -> None:
    assert definition_fingerprint("mcp", "x", {"a": 1}) != definition_fingerprint(
        "hooks", "x", {"a": 1}
    )
    assert definition_fingerprint("mcp", "x", {"a": 1}) != definition_fingerprint(
        "mcp", "y", {"a": 1}
    )


def test_fingerprints_are_full_length_and_display_is_a_prefix() -> None:
    digest = mcp_definition_fingerprint(_server())
    assert is_fingerprint(digest)
    assert len(digest) == len("sha256:") + 64
    assert short_fingerprint(digest) == digest[7:15]
    assert not is_fingerprint(digest[:-1])
    assert not is_fingerprint(True)


def test_non_canonical_definition_cannot_be_fingerprinted() -> None:
    with pytest.raises(UnfingerprintableError):
        canonical_digest({"value": object()})


def test_evidence_fingerprint_is_byte_identical_to_0_36() -> None:
    payload = {"dependency": "acme/x", "commit": "abc", "depth": 2, "declaration": _SERVER}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    expected = "sha256:" + hashlib.sha256(encoded.encode("ascii")).hexdigest()
    assert evidence_fingerprint(payload) == expected
