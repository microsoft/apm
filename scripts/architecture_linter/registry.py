"""Strict loading for the versioned canonical-owner registry."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

REGISTRY_VERSION = 1
INDEX_FILE = "index.json"
_INDEX_FIELDS = frozenset({"version", "shards"})
_SHARD_FIELDS = frozenset({"version", "owners"})
_OWNER_FIELDS = frozenset({"id", "decision", "owner", "selectors", "guards"})
# Blast-radius overlay (#4117): optional on an owner, but all four or none.
BLAST_RADIUS_FIELDS = frozenset({"families", "state", "promises", "modules"})
STATE_VALUES = frozenset(
    {
        "approvals",
        "cache",
        "compiled-output",
        "credentials",
        "deployed-files",
        "egress",
        "lockfile",
        "manifest",
        "mcp-config",
        "user-scope",
    }
)
MODULES_ROOT = "src/apm_cli/"
FAMILIES_ROOT = "tests/integration/"
PROMISE_CATALOG = "tests/quality/promise-catalog.json"
LIFECYCLE_LEDGER = "tests/fixtures/lifecycle_bug_ledger.json"
_INVARIANT_PROMISES = frozenset(f"P-INV-I{number}" for number in range(1, 10))
_PROMISE_ID = re.compile(r"P-(?:INV-I[1-9]|LAW-[a-z0-9_]+(?:\.[a-z0-9_]+)+|OWN-[a-z0-9]+(?:-[a-z0-9]+)*)")
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
    families: tuple[str, ...] = ()
    state: tuple[str, ...] = ()
    promises: tuple[str, ...] = ()
    modules: tuple[str, ...] = ()


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


@dataclass(frozen=True)
class PromiseVocabulary:
    """Closed promise-id set; ledger fallback admits P-OWN ids of registered owners."""

    ids: frozenset[str]
    owner_ids_from_registry: bool


def promise_vocabulary(catalog: bytes | None, ledger: bytes | None) -> PromiseVocabulary | None:
    """Read the promise catalog, or derive the vocabulary from the ledger when it is absent."""
    if catalog is not None:
        document = _mapping(_load_json(catalog, PROMISE_CATALOG), PROMISE_CATALOG)
        promises = _mapping(document.get("promises"), f"{PROMISE_CATALOG}.promises")
        ids = frozenset(key for key in promises if _PROMISE_ID.fullmatch(key))
        return PromiseVocabulary(ids=ids, owner_ids_from_registry=False)
    if ledger is None:
        return None
    document = _mapping(_load_json(ledger, LIFECYCLE_LEDGER), LIFECYCLE_LEDGER)
    rows = document.get("property_catalog")
    if not isinstance(rows, list):
        raise RegistryError(f"{LIFECYCLE_LEDGER}.property_catalog must be an array")
    laws = {
        "P-LAW-" + _printable_string(_mapping(row, "property").get("id"), "property.id")
        for row in rows
    }
    return PromiseVocabulary(ids=_INVARIANT_PROMISES | laws, owner_ids_from_registry=True)


def _blast_radius_hash_fields(owner: OwnerRecord) -> dict[str, list[str]]:
    if not owner.modules:
        return {}
    return {
        "families": sorted(owner.families),
        "state": sorted(owner.state),
        "promises": sorted(owner.promises),
        "modules": sorted(owner.modules),
    }


def _rooted_globs(value: Any, label: str, root: str) -> tuple[str, ...]:
    items = _string_array(value, label)
    for item in items:
        _validate_selector(item)
        if not item.startswith(root):
            raise RegistryError(f"{label} entries must start with {root}: {item!r}")
    return items


def _sorted_vocabulary(value: Any, label: str, allowed: Callable[[str], bool]) -> tuple[str, ...]:
    items = _string_array(value, label)
    unknown = [item for item in items if not allowed(item)]
    if unknown:
        raise RegistryError(f"{label} has unknown values: {', '.join(unknown)}")
    if list(items) != sorted(items):
        raise RegistryError(f"{label} must be sorted")
    return items


def _blast_radius_block(item: Mapping[str, Any], label: str) -> dict[str, tuple[str, ...]]:
    """Validate the optional all-or-none blast-radius overlay of one owner."""
    present = BLAST_RADIUS_FIELDS & set(item)
    if not present:
        return {}
    if present != BLAST_RADIUS_FIELDS:
        missing = ", ".join(sorted(BLAST_RADIUS_FIELDS - present))
        raise RegistryError(f"{label} blast-radius fields are all-or-none; missing: {missing}")
    return {
        "families": _rooted_globs(item["families"], f"{label}.families", FAMILIES_ROOT),
        "state": _sorted_vocabulary(item["state"], f"{label}.state", STATE_VALUES.__contains__),
        "promises": _sorted_vocabulary(
            item["promises"], f"{label}.promises", lambda value: bool(_PROMISE_ID.fullmatch(value))
        ),
        "modules": _rooted_globs(item["modules"], f"{label}.modules", MODULES_ROOT),
    }


def _validate_promises(owners: Iterable[OwnerRecord], vocabulary: PromiseVocabulary | None) -> None:
    owner_rows = tuple(owners)
    owner_promises = {f"P-OWN-{owner.id}" for owner in owner_rows}
    for owner in owner_rows:
        for promise in owner.promises:
            if promise.startswith("P-OWN-") and promise not in owner_promises:
                raise RegistryError(f"{owner.id}.promises names an unknown owner: {promise}")
            if vocabulary is None:
                continue
            known = promise in vocabulary.ids or (
                vocabulary.owner_ids_from_registry and promise in owner_promises
            )
            if not known:
                raise RegistryError(f"{owner.id}.promises names an uncatalogued promise: {promise}")


def _validate_blast_radius_inventory(owners: Iterable[OwnerRecord], inventory: Sequence[str]) -> None:
    """Every module and family glob resolves; modules cover the owner's own src selectors."""
    for owner in owners:
        if not owner.modules:
            continue
        for module in owner.modules:
            if not any(fnmatch.fnmatchcase(path, module) for path in inventory):
                raise RegistryError(f"{owner.id}.modules matches no supplied file: {module}")
        for family in owner.families:
            if not any(
                fnmatch.fnmatchcase(path, family)
                and PurePosixPath(path).name.startswith("test_")
                and path.endswith(".py")
                for path in inventory
            ):
                raise RegistryError(f"{owner.id}.families matches no test module: {family}")
        for path in inventory:
            if not path.startswith(MODULES_ROOT):
                continue
            owned = any(fnmatch.fnmatchcase(path, selector) for selector in owner.selectors)
            if owned and not any(fnmatch.fnmatchcase(path, module) for module in owner.modules):
                raise RegistryError(f"{owner.id}.modules must cover its own selector file: {path}")


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
                **_blast_radius_hash_fields(owner),
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
    promise_ids: PromiseVocabulary | None = None,
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
            blast_radius = _blast_radius_block(item, label)
            _exact_fields(item, _OWNER_FIELDS | frozenset(blast_radius), label)
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
                    **blast_radius,
                )
            )

    inventory = tuple(file_inventory)
    _validate_selector_inventory(owners, inventory)
    _validate_blast_radius_inventory(owners, inventory)
    _validate_promises(owners, promise_ids)

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
    promise_ids: PromiseVocabulary | None = None,
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
        promise_ids=promise_ids,
    )
