"""Strict loading for the versioned canonical-owner registry."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

REGISTRY_VERSION = 1
INDEX_FILE = "index.json"
_INDEX_FIELDS = frozenset({"version", "shards"})
_SHARD_FIELDS = frozenset({"version", "owners"})
_OWNER_FIELDS = frozenset({"id", "decision", "owner", "selectors", "guards"})
_KEBAB_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_SHARD_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\.json")


class RegistryError(ValueError):
    """Raised when ownership registry data is incomplete or ambiguous."""


@dataclass(frozen=True)
class OwnerRecord:
    """One canonical durable-decision owner."""

    id: str
    decision: str
    owner: str
    selectors: tuple[str, ...]
    guards: tuple[str, ...]


@dataclass(frozen=True)
class OwnerRegistry:
    """The validated semantic union of every listed owner shard."""

    version: int
    shards: tuple[str, ...]
    owners: tuple[OwnerRecord, ...]
    semantic_sha256: str


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RegistryError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _load_json(content: bytes, label: str) -> Any:
    try:
        text = content.decode("ascii")
    except UnicodeDecodeError as exc:
        raise RegistryError(f"{label} must be printable ASCII") from exc
    if any(ord(char) < 32 and char not in "\t\n\r" for char in text):
        raise RegistryError(f"{label} must be printable ASCII")
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise RegistryError(f"{label} is malformed JSON: {exc.msg}") from exc


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RegistryError(f"{label} must be an object")
    return value


def _exact_fields(value: Mapping[str, Any], expected: frozenset[str], label: str) -> None:
    unknown = sorted(set(value) - expected)
    missing = sorted(expected - set(value))
    if unknown:
        raise RegistryError(f"{label} has unknown fields: {', '.join(unknown)}")
    if missing:
        raise RegistryError(f"{label} is missing fields: {', '.join(missing)}")


def _version(value: Any, label: str) -> None:
    if type(value) is not int or value != REGISTRY_VERSION:
        raise RegistryError(f"{label}.version must be {REGISTRY_VERSION}")


def _printable_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise RegistryError(f"{label} must be a non-empty trimmed string")
    if not value.isascii() or any(ord(char) < 32 or ord(char) > 126 for char in value):
        raise RegistryError(f"{label} must be printable ASCII")
    return value


def _string_array(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise RegistryError(f"{label} must be a non-empty array")
    items = tuple(_printable_string(item, f"{label}[]") for item in value)
    if len(set(items)) != len(items):
        raise RegistryError(f"{label} contains duplicates")
    return items


def _validate_id(value: str, label: str) -> None:
    if _KEBAB_ID.fullmatch(value) is None:
        raise RegistryError(f"{label} must be a stable kebab-case ID")


def validate_shard_name(value: str) -> None:
    """Reject shard names that are not flat kebab-case JSON filenames."""
    if _SHARD_NAME.fullmatch(value) is None or value == INDEX_FILE:
        raise RegistryError(f"invalid registry shard name: {value!r}")


def _validate_selector(selector: str) -> None:
    path = PurePosixPath(selector)
    if (
        path.is_absolute()
        or selector.startswith("./")
        or selector.endswith("/")
        or "\\" in selector
        or "." in path.parts
        or ".." in path.parts
        or "//" in selector
    ):
        raise RegistryError(f"unsafe POSIX owner selector: {selector!r}")


def _semantic_hash(owners: Iterable[OwnerRecord]) -> str:
    normalized = {
        "version": REGISTRY_VERSION,
        "owners": [
            {
                "id": owner.id,
                "decision": owner.decision,
                "owner": owner.owner,
                "selectors": sorted(owner.selectors),
                "guards": sorted(owner.guards),
            }
            for owner in sorted(owners, key=lambda item: item.id)
        ],
    }
    encoded = json.dumps(
        normalized,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _validate_selector_inventory(
    owners: Iterable[OwnerRecord],
    file_inventory: Iterable[str],
) -> None:
    """Require every selector to resolve and every file to have at most one owner."""
    owner_rows = tuple(owners)
    inventory = tuple(file_inventory)
    for owner in owner_rows:
        for selector in owner.selectors:
            if not any(fnmatch.fnmatchcase(path, selector) for path in inventory):
                raise RegistryError(f"owner selector matches no supplied file: {selector}")

    for path in inventory:
        matching_owner_ids = sorted(
            owner.id
            for owner in owner_rows
            if any(fnmatch.fnmatchcase(path, selector) for selector in owner.selectors)
        )
        if len(matching_owner_ids) > 1:
            raise RegistryError(
                "supplied file matches selectors from multiple owners: "
                f"{path} ({', '.join(matching_owner_ids)})"
            )


def load_registry_documents(
    index_content: bytes,
    shard_contents: Mapping[str, bytes],
    file_inventory: Iterable[str],
    *,
    known_guard_ids: Collection[str] | None = None,
    owner_specific_guard_ids: Collection[str] = (),
) -> OwnerRegistry:
    """Load a complete registry from supplied exact-snapshot documents."""
    index = _mapping(_load_json(index_content, INDEX_FILE), INDEX_FILE)
    _exact_fields(index, _INDEX_FIELDS, INDEX_FILE)
    _version(index.get("version"), INDEX_FILE)
    shards = _string_array(index.get("shards"), "index.json.shards")
    for shard in shards:
        validate_shard_name(shard)

    supplied = set(shard_contents)
    listed = set(shards)
    missing_shards = sorted(listed - supplied)
    unlisted_shards = sorted(supplied - listed)
    if missing_shards:
        raise RegistryError(f"missing registry shards: {', '.join(missing_shards)}")
    if unlisted_shards:
        raise RegistryError(f"unlisted registry shards: {', '.join(unlisted_shards)}")

    owners: list[OwnerRecord] = []
    ids: set[str] = set()
    decisions: set[str] = set()
    selectors: set[str] = set()
    guard_owners: dict[str, str] = {}
    for shard in shards:
        document = _mapping(_load_json(shard_contents[shard], shard), shard)
        _exact_fields(document, _SHARD_FIELDS, shard)
        _version(document.get("version"), shard)
        raw_owners = document.get("owners")
        if not isinstance(raw_owners, list) or not raw_owners:
            raise RegistryError(f"{shard}.owners must be a non-empty array")
        for index_number, raw_owner in enumerate(raw_owners):
            label = f"{shard}.owners[{index_number}]"
            item = _mapping(raw_owner, label)
            _exact_fields(item, _OWNER_FIELDS, label)
            owner_id = _printable_string(item.get("id"), f"{label}.id")
            _validate_id(owner_id, f"{label}.id")
            decision = _printable_string(item.get("decision"), f"{label}.decision")
            canonical_owner = _printable_string(item.get("owner"), f"{label}.owner")
            owner_selectors = _string_array(item.get("selectors"), f"{label}.selectors")
            guards = _string_array(item.get("guards"), f"{label}.guards")
            for guard in guards:
                _validate_id(guard, f"{label}.guards[]")
                assigned_owner = guard_owners.get(guard)
                if assigned_owner is not None and assigned_owner != owner_id:
                    raise RegistryError(
                        f"guard ID assigned to multiple owners: {guard} "
                        f"({assigned_owner}, {owner_id})"
                    )
                guard_owners[guard] = owner_id
            if owner_id in ids:
                raise RegistryError(f"duplicate canonical owner ID: {owner_id}")
            if decision in decisions:
                raise RegistryError(f"duplicate canonical owner decision: {decision}")
            for selector in owner_selectors:
                _validate_selector(selector)
                if selector in selectors:
                    raise RegistryError(f"duplicate canonical owner selector: {selector}")
                selectors.add(selector)
            ids.add(owner_id)
            decisions.add(decision)
            owners.append(
                OwnerRecord(
                    id=owner_id,
                    decision=decision,
                    owner=canonical_owner,
                    selectors=owner_selectors,
                    guards=guards,
                )
            )

    _validate_selector_inventory(owners, file_inventory)

    referenced_guards = {guard for owner in owners for guard in owner.guards}
    if known_guard_ids is not None:
        unknown_guards = sorted(referenced_guards - set(known_guard_ids))
        if unknown_guards:
            raise RegistryError(f"unknown owner guard IDs: {', '.join(unknown_guards)}")
    unreferenced_owner_guards = sorted(set(owner_specific_guard_ids) - referenced_guards)
    if unreferenced_owner_guards:
        raise RegistryError(
            "owner-specific guard IDs are not referenced: " + ", ".join(unreferenced_owner_guards)
        )

    normalized_owners = tuple(sorted(owners, key=lambda item: item.id))
    return OwnerRegistry(
        version=REGISTRY_VERSION,
        shards=shards,
        owners=normalized_owners,
        semantic_sha256=_semantic_hash(normalized_owners),
    )


def load_registry(
    registry_dir: Path,
    file_inventory: Iterable[str],
    *,
    known_guard_ids: Collection[str] | None = None,
    owner_specific_guard_ids: Collection[str] = (),
) -> OwnerRegistry:
    """Load a registry directory and reject missing or unlisted JSON shards."""
    index_path = registry_dir / INDEX_FILE
    artifacts = tuple(path for path in registry_dir.rglob("*") if path.is_file())
    if not index_path.is_file() and artifacts:
        raise RegistryError(
            f"registry artifacts exist without {index_path.relative_to(registry_dir).as_posix()}"
        )
    try:
        index_content = index_path.read_bytes()
    except OSError as exc:
        raise RegistryError(f"cannot read {index_path.as_posix()}: {exc}") from exc
    index = _mapping(_load_json(index_content, INDEX_FILE), INDEX_FILE)
    _exact_fields(index, _INDEX_FIELDS, INDEX_FILE)
    _version(index.get("version"), INDEX_FILE)
    listed_shards = _string_array(index.get("shards"), "index.json.shards")
    for shard in listed_shards:
        validate_shard_name(shard)
    listed_names = set(listed_shards)
    disk_names = {
        path.relative_to(registry_dir).as_posix()
        for path in artifacts
        if path.suffix.lower() == ".json"
        if path != index_path
    }
    documents: dict[str, bytes] = {}
    for name in sorted(listed_names | disk_names):
        path = registry_dir / name
        if path.is_file():
            try:
                documents[name] = path.read_bytes()
            except OSError as exc:
                raise RegistryError(f"cannot read {path.as_posix()}: {exc}") from exc
    return load_registry_documents(
        index_content,
        documents,
        file_inventory,
        known_guard_ids=known_guard_ids,
        owner_specific_guard_ids=owner_specific_guard_ids,
    )
