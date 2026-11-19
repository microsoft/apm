"""Single owner of every executable-trust fingerprint (#3902).

An approval in ``apm.yml`` pins one executable item -- one MCP server or one
hook file -- to a SHA-256 digest of what actually runs. Lockfile evidence and
local-bundle grants hash through this module too, so APM has exactly one
hasher for trust decisions.

Digests are full-length ``sha256:<64 hex>``: the upstream author writes both
definitions, so the relevant attack is a collision, and truncation would make
one affordable. Prefixes are display-only and never matched.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any

FINGERPRINT_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
DISPLAY_LENGTH = 8

KIND_MCP = "mcp"
KIND_HOOK = "hooks"

# Pinned definition fields of an MCP server. Changing this list re-gates every
# approval, so it is golden-tested rather than derived from ``to_dict``.
_MCP_FIELDS = (
    "transport",
    "command",
    "args",
    "env",
    "url",
    "headers",
    "cwd",
    "registry",
    "version",
    "package",
    "tools",
)


class UnfingerprintableError(ValueError):
    """A definition that cannot be hashed canonically; callers park it."""


def canonical_digest(payload: Any) -> str:
    """Return ``sha256:<64 hex>`` over the canonical JSON form of *payload*."""
    try:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    except (TypeError, ValueError) as exc:
        raise UnfingerprintableError(f"definition is not canonical JSON: {exc}") from exc
    return "sha256:" + hashlib.sha256(encoded.encode("ascii")).hexdigest()


def is_fingerprint(value: object) -> bool:
    """Return whether *value* is a full-length fingerprint."""
    return isinstance(value, str) and FINGERPRINT_PATTERN.match(value) is not None


def short_fingerprint(value: str) -> str:
    """Return the display prefix of a fingerprint (never used for matching)."""
    return value.removeprefix("sha256:")[:DISPLAY_LENGTH]


def definition_fingerprint(kind: str, item: str, definition: Any) -> str:
    """Domain-separated fingerprint of one executable item's definition."""
    return canonical_digest({"v": 1, "kind": kind, "item": item, "def": definition})


def mcp_definition(dependency: Any) -> dict[str, Any]:
    """Return the pinned definition fields of one MCP server declaration."""
    definition: dict[str, Any] = {}
    for name in _MCP_FIELDS:
        value = getattr(dependency, name, None)
        if value is not None or (name == "registry" and value is False):
            definition[name] = value
    if getattr(dependency, "has_enabled", False):
        definition["enabled"] = dependency.enabled
    extra = getattr(dependency, "extra", None)
    if extra:
        definition["extra"] = dict(extra)
    return definition


def mcp_definition_fingerprint(
    dependency: Any,
    *,
    registry_entry: dict[str, Any] | None = None,
) -> str:
    """Fingerprint the exact MCP declaration that deploys.

    A registry-resolved server also binds the resolved registry entry, because
    the command that runs comes from the registry, not from the overlay.
    """
    definition = mcp_definition(dependency)
    if registry_entry is not None:
        definition["registry_entry"] = registry_entry
    return definition_fingerprint(KIND_MCP, str(dependency.name), definition)


def hook_item_key(package_path: Path, hook_file: Path) -> str:
    """Return the publisher-relative POSIX key that names one hook file.

    A normalized ``.apm/hooks/`` byte copy is named by the publisher file it
    copies, so ``apm approve`` on the source tree and install on APM's
    normalized cache agree on the key (#3290).
    """
    from apm_cli.deps.plugin_parser import normalized_hook_source

    root = package_path.resolve()
    relative = hook_file.resolve().relative_to(root).as_posix()
    return normalized_hook_source(relative, root) or relative


def hook_file_fingerprint(package_path: Path, hook_file: Path) -> str:
    """Fingerprint one hook file: its document plus every script it references."""
    from apm_cli.integration.hook_source_selection import (
        _parse_hook_json,
        _referenced_hook_source_files,
    )

    package_path = package_path.resolve()
    data = _parse_hook_json(hook_file, logger=logging.getLogger(__name__))
    if data is None:
        raise UnfingerprintableError(f"hook file {hook_file.name} is not a hook document")
    scripts = []
    for script in sorted(_referenced_hook_source_files(data, package_path, hook_file.parent)):
        digest = hashlib.sha256(script.read_bytes()).hexdigest()
        scripts.append([hook_item_key(package_path, script), digest])
    return definition_fingerprint(
        KIND_HOOK,
        hook_item_key(package_path, hook_file),
        {"document": data, "scripts": scripts},
    )


def native_plugin_fingerprint(plugin_root: Path) -> str:
    """Approval pin of a natively loaded plugin: path and bytes per file.

    Covers the native plugin file set (APM's root ``apm.yml`` and ``.apm/``
    normalization output excluded), so the approved source and the deployed
    copy compare equal. The exec bit stays out so an approval committed on
    one operating system holds on another; the per-machine integrity hash
    (``compute_native_plugin_hash``) still checks modes. Raises ``OSError`` or
    ``PathTraversalError`` for unreadable data or a symlink; callers treat
    that as "cannot verify".
    """
    from apm_cli.utils.content_hash import compute_native_plugin_pin

    return compute_native_plugin_pin(plugin_root)


def evidence_fingerprint(payload: dict[str, Any]) -> str:
    """Lockfile omission evidence digest (byte-identical to 0.36 evidence)."""
    return canonical_digest(payload)


def local_bundle_digest(source_dir: Path, lockfile: dict[str, Any] | None = None) -> str:
    """Digest every deployable byte of a local bundle (byte-identical to 0.36)."""
    hasher = hashlib.sha256()
    pack = lockfile.get("pack") if isinstance(lockfile, dict) else None
    bundle_files = pack.get("bundle_files") if isinstance(pack, dict) else None
    if isinstance(bundle_files, dict):
        for rel_path in sorted(bundle_files, key=str):
            digest = bundle_files[rel_path]
            encoded_path = str(rel_path).encode("utf-8")
            encoded_digest = str(digest).encode("utf-8")
            hasher.update(len(encoded_path).to_bytes(8, "big"))
            hasher.update(encoded_path)
            hasher.update(len(encoded_digest).to_bytes(8, "big"))
            hasher.update(encoded_digest)
    else:
        files: list[tuple[str, Path]] = []
        for item in source_dir.rglob("*"):
            rel_path = item.relative_to(source_dir).as_posix()
            if item.is_symlink():
                raise ValueError(f"Local bundle contains a symlink: {rel_path}")
            if item.is_dir():
                continue
            if not item.is_file():
                raise ValueError(f"Local bundle contains a special file: {rel_path}")
            files.append((rel_path, item))
        for rel_path, item in sorted(files):
            encoded_path = rel_path.encode("utf-8")
            content_length = item.stat().st_size
            hasher.update(len(encoded_path).to_bytes(8, "big"))
            hasher.update(encoded_path)
            hasher.update(content_length.to_bytes(8, "big"))
            with item.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    hasher.update(chunk)
    return f"sha256:{hasher.hexdigest()}"
