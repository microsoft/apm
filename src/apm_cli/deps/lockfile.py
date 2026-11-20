"""Lock file support for APM dependency resolution.

Provides deterministic, reproducible installs by capturing exact resolved versions.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from ..core.deployment_state import DeploymentLedger
from ..core.host_providers import accepted_host_types
from ..models.apm_package import DependencyReference
from ..models.dependency.identity import (
    build_port_blind_dependency_key,
    is_case_insensitive_package_identity,
    normalize_package_repo_url,
)
from ..models.dependency.object_fields import parse_alias_override
from ..models.dependency.reference import (
    build_canonical_dependency_string,
    build_dependency_unique_key,
)
from ..models.dependency.replacement import (
    LocalReplacementSource,
    ReplacementBinding,
    ReplacementConfigError,
    build_replacement_index,
    parse_replace_block,
    project_effective_reference,
)
from ..models.dependency.subsets import SkillTargetSelection, canonical_target_subset
from ..native_plugins.record import NativePluginRecord, NativePluginRecordError
from .carried_license import CarriedLicense, records_from_lock_data, records_to_lock_data

logger = logging.getLogger(__name__)

_SELF_KEY = "."
_ALLOWED_HOST_TYPES = set(accepted_host_types())
_ALLOWED_EXEC_STATUS = {"deployed", "gated_pending_approval", "denied", "absent"}
SUPPORTED_LOCKFILE_VERSIONS = frozenset({"1", "2", "3", "4"})
# Version 4 records package content hashes in the framed stream; 1-3 in the legacy one.
FRAMED_CONTENT_HASH_VERSION = "4"
_REPRODUCIBLE_EPOCH = "1970-01-01T00:00:00+00:00"


def lockfile_parent_identity(
    dependency: DependencyReference | LockedDependency,
    replacements: Sequence[object] | None,
) -> str:
    """Encode exact original parent keys for v3, retaining legacy repo-url parents.

    Legacy parents use the canonical repository identity, so a parent declared
    with different casing (``Owner/Repo``) still names its locked entry. A lock
    written before this rule self-heals on the next install that records it.
    """
    return (
        dependency.get_unique_key() if replacements is not None else dependency.canonical_repo_url
    )


def _copy_extension_map(value: object) -> dict[str, Any]:
    """Copy a vendor-extension mapping, or return {} for empty/invalid input.

    Non-mapping values (corrupt lockfiles) become an empty map so readers do
    not raise. String-coerced keys keep declaration order.
    """
    if not isinstance(value, dict) or not value:
        return {}
    return {str(key): item for key, item in value.items()}


def _copy_subset_pairings(value: object) -> Any:
    """Copy raw ``subset_pairings`` records without validating them.

    Malformed values survive the round trip so audit and frozen install can
    reject them through ``LockedDependency.selection()`` (req-rs-018).
    """
    if value is None or value == []:
        return None
    if isinstance(value, list):
        return [
            {key: list(item) if isinstance(item, list) else item for key, item in record.items()}
            if isinstance(record, dict)
            else record
            for record in value
        ]
    return value


def _replay_pairing(
    skill_subset: list[str], target_subset: list[str], records: Any
) -> SkillTargetSelection | None:
    """Return the exact pairing a lock entry replays, or ``None`` for a product.

    Invalid records degrade to the flat product (what earlier writers
    deployed); audit and frozen install reject them via ``selection()``.
    """
    try:
        pairing = SkillTargetSelection.from_lock_fields(skill_subset, target_subset, records)
    except ValueError:
        return None
    return None if pairing.is_product else pairing


class _ExistingLockfileUnset:
    """Sentinel for callers that have not already read the destination."""


_EXISTING_LOCKFILE_UNSET = _ExistingLockfileUnset()

# req-lk-006 requires a frozen-install mode "in which the lockfile is never
# written or rewritten".  An install reaches ``LockFile.write`` from eight
# call sites (dependency lockfile build, local bundles, target-contraction
# reconciliation, local-content persist, MCP and LSP integration, ...), so
# the suppression lives at the one chokepoint they all funnel through
# instead of as a flag threaded to each -- a new write site then inherits
# the guarantee rather than needing to remember it.
#
# Suppressed writes are *recorded*, not merely dropped: the caller compares
# them against the committed lockfile to decide whether the frozen install
# can honestly report success (see ``InstallService``).
#
# A ContextVar rather than a module global so the mode cannot leak across
# concurrent installs in one process (tests, programmatic callers).
_suppressed_writes: ContextVar[list[str] | None] = ContextVar(
    "apm_suppressed_lockfile_writes", default=None
)


@contextmanager
def suppress_lockfile_writes() -> Iterator[list[str]]:
    """Record and discard every :meth:`LockFile.write` in this context.

    Yields the list of serialised lockfiles that *would* have been written,
    in call order.  Serialised rather than held by reference so that later
    mutation of the same in-memory :class:`LockFile` cannot rewrite
    history: each entry is exactly the content that ``write`` withheld.
    """
    attempts: list[str] = []
    token = _suppressed_writes.set(attempts)
    try:
        yield attempts
    finally:
        _suppressed_writes.reset(token)


def installed_apm_version() -> str:
    """Return the running APM version stamped into lockfile metadata.

    Delegates to :func:`apm_cli.version.get_version` (build constant, package
    metadata, then source tree) so the stamp and the generator compatibility
    gate always agree on which APM this is.
    """
    try:
        from ..version import get_version

        return get_version()
    except Exception:
        return "unknown"


def _keeps_untranslated_mcp_stamp(lock: LockFile, existing: LockFile | None) -> bool:
    """Whether *lock* carries forward MCP claims whose meaning only its stamp records.

    Until the MCP phase translates a pre-0.37 lock's ``copilot``/``vscode``
    claims (and stamps the running version itself), restamping would make the
    retired-destination heal skip them.
    """
    if existing is None or lock.apm_version != existing.apm_version:
        return False
    from ..install.mcp.retired_destinations import lock_holds_untranslated_mcp_claims

    # Only claims carried forward keep the old meaning: removing the last one,
    # or writing new ones, hands the lock to this writer.
    carried = {
        runtime: set(names or ()) & set(lock.mcp_target_servers.get(runtime) or ())
        for runtime, names in existing.mcp_target_servers.items()
    }
    return lock_holds_untranslated_mcp_claims(existing.apm_version, carried)


def _same_document_on_disk(path: Path, serialized: str) -> bool:
    """Whether *path* already holds exactly the data *serialized* would write.

    Only formatting may differ; a legacy layout the writer would migrate is a
    different document and is rewritten.
    """
    from ..utils.yaml_io import load_yaml_str

    try:
        on_disk = load_yaml_str(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError, ValueError):
        return False
    return on_disk == load_yaml_str(serialized)


def recorded_generator(data: Mapping[str, Any]) -> object:
    """The ``apm_version`` a parsed lockfile records for the generator gate.

    An explicit ``apm_version: null`` is unverifiable, unlike an absent key, so
    both the project and the bundle reader warn on it the same way (#3963).
    """
    if "apm_version" in data and data["apm_version"] is None:
        return "null"
    return data.get("apm_version")


def resolve_reproducible_timestamp(
    explicit: str | None,
    lockfile_generated_at: str | None,
) -> str:
    """Resolve stable metadata time from explicit, environment, or legacy input."""
    if explicit:
        return explicit
    source_date_epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if source_date_epoch:
        try:
            return datetime.fromtimestamp(
                int(source_date_epoch),
                tz=UTC,
            ).isoformat()
        except (ValueError, OverflowError, OSError):
            pass
    return lockfile_generated_at or _REPRODUCIBLE_EPOCH


class LockfileFormatError(ValueError):
    """Raised when a lockfile container does not match its schema."""


class UnsupportedLockfileVersionError(LockfileFormatError):
    """Raised when a lockfile declares a version this client cannot read."""


class LockfileConflictError(LockfileFormatError):
    """Raised when a lockfile still contains git merge conflict markers."""

    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(
            f"{path} contains unresolved git merge conflict markers.\n"
            "Resolve the conflict in that file or restore a known-good lockfile, "
            "then retry your original command."
        )


class LockfileRefusal(Exception):
    """Marker for canonical refusals to write a lockfile.

    Its message is the user's next step; render sites show it verbatim
    instead of a generic write-failure hint.
    """


class LockfileSymlinkError(OSError, LockfileRefusal):
    """Raised instead of replacing a symlinked lockfile with a regular file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            self.target = os.readlink(path)
        except OSError:
            self.target = "<unreadable>"
        super().__init__(
            f"{path} is a symbolic link to {self.target}. APM does not write "
            "lockfiles through symbolic links, so the link and its target were "
            "left unchanged.\nReplace the link with a regular file (for example, "
            "copy the target over it), then retry your original command."
        )


def ensure_lockfile_writable(path: Path) -> None:
    """Raise when this CLI must not write *path*.

    Single owner of lockfile writability: never write through a symlinked
    lockfile (:class:`LockfileSymlinkError`), and never let an older CLI
    rewrite a lockfile a newer APM generated
    (:class:`~apm_cli.deps.lockfile_generator.LockfileGeneratorError`, #2334).
    :meth:`LockFile.write` enforces it, and commands that will rewrite the
    lockfile call it before any other project write so a refusal is atomic.
    """
    if path.is_symlink():
        raise LockfileSymlinkError(path)
    from .lockfile_generator import assert_writable

    assert_writable(path, action="rewrite", before_mutation=False)


def refuse_lockfile_before_mutation(
    lockfile: LockFile | None,
    lockfile_path: Path,
    *,
    would_mutate: bool,
) -> None:
    """Pre-mutation guard: refuse an unwritable lockfile before side effects.

    Commands that rewrite ``apm.lock.yaml`` (install --update, uninstall)
    must call this *before* scripts, file deletions, or manifest edits so
    that a symlinked lockfile stops the operation while it is still a no-op.

    Args:
        lockfile: The loaded lockfile, or ``None`` when no lockfile exists.
        lockfile_path: On-disk path to be rewritten.
        would_mutate: ``True`` when the operation will definitely change
            the lockfile (e.g. at least one locked key is being removed,
            or the update plan has changes).
    """
    if lockfile is None or not would_mutate:
        return
    ensure_lockfile_writable(lockfile_path)


# Git writes each marker at the start of a line, followed by a space and a label
# (or nothing, for a bare ``=======`` separator). A separator alone is not
# evidence of a conflict, so only the labelled markers are matched here.
_CONFLICT_MARKER_RE = re.compile(r"^(?:<{7}|>{7}|\|{7})(?: |$)", re.MULTILINE)


def has_conflict_markers(text: str) -> bool:
    """Return True when *text* contains a git merge conflict marker at line start."""
    return _CONFLICT_MARKER_RE.search(text) is not None


def records_framed_content_hashes(existing: LockFile | None) -> bool:
    """Stream an install records package hashes in: the existing lockfile's, framed if new."""
    return existing is None or existing.framed_content_hashes


def require_supported_lockfile_version(data: object) -> str:
    """Return a supported declared version or fail closed.

    An unquoted integer (``lockfile_version: 1``, as left by hand edits or
    YAML tools that drop quotes) denotes the same version as its string form.
    """
    if not isinstance(data, dict):
        raise LockfileFormatError("Lockfile root must be a mapping")
    version = data.get("lockfile_version", "1")
    if type(version) is int:
        version = str(version)
    if not isinstance(version, str) or version not in SUPPORTED_LOCKFILE_VERSIONS:
        supported = ", ".join(sorted(SUPPORTED_LOCKFILE_VERSIONS))
        raise UnsupportedLockfileVersionError(
            f"Unsupported lockfile version {version!r}; supported versions: {supported}"
        )
    return version


def _validate_lockfile_container(data: object) -> dict[str, Any]:
    """Validate version and top-level container shapes before construction."""
    if not isinstance(data, dict):
        raise LockfileFormatError("Lockfile root must be a mapping")
    data = dict(data)
    # Pre-versioned lockfiles are a supported legacy v1 migration input.
    # Explicit unknown/newer versions still fail closed here.
    data["lockfile_version"] = require_supported_lockfile_version(data)
    list_fields = (
        "dependencies",
        "deployments",
        "mcp_servers",
        "lsp_servers",
        "local_deployed_files",
        "local_staged_files",
    )
    mapping_fields = (
        "mcp_configs",
        "mcp_target_servers",
        "mcp_config_provenance",
        "mcp_omissions",
        "lsp_configs",
        "lsp_target_servers",
        "lsp_config_provenance",
        "local_deployed_file_hashes",
        "local_staged_file_hashes",
    )
    for field_name in list_fields:
        if field_name in data and data[field_name] is None:
            data[field_name] = []
        elif field_name in data and not isinstance(data[field_name], list):
            raise LockfileFormatError(f"Lockfile field {field_name!r} must be a list")
    for field_name in mapping_fields:
        if field_name in data and data[field_name] is None:
            data[field_name] = {}
        elif field_name in data and not isinstance(data[field_name], dict):
            raise LockfileFormatError(f"Lockfile field {field_name!r} must be a mapping")
    for index, dependency in enumerate(data.get("dependencies", [])):
        if not isinstance(dependency, dict):
            raise LockfileFormatError(f"Lockfile dependency at index {index} must be a mapping")
    if any(not isinstance(name, str) or not name for name in (data.get("mcp_omissions") or {})):
        raise LockfileFormatError(
            "MCP omission evidence has invalid server names; selection is unknown. "
            "Restore a known-good lockfile and repeat the intended install selection"
        )
    for field_name in ("mcp_target_servers", "lsp_target_servers"):
        for target, servers in (data.get(field_name) or {}).items():
            if not isinstance(target, str) or not target or not isinstance(servers, list):
                raise LockfileFormatError(
                    f"Lockfile {field_name} values must be string-to-list mappings"
                )
            if not all(isinstance(server, str) and bool(server) for server in servers):
                raise LockfileFormatError(f"Lockfile {field_name} entries must be strings")
    for server, provenance in (data.get("mcp_config_provenance") or {}).items():
        if not isinstance(server, str) or not (
            (isinstance(provenance, str) and bool(provenance))
            or (
                isinstance(provenance, list)
                and bool(provenance)
                and all(isinstance(owner, str) and bool(owner) for owner in provenance)
            )
        ):
            raise LockfileFormatError(
                "Lockfile mcp_config_provenance values must be strings or string lists"
            )
    if "deployments" in data:
        from ..core.deployment_ledger import DeploymentLedgerCodec

        try:
            DeploymentLedgerCodec.validate_rows(data["deployments"])
        except ValueError as exc:
            raise LockfileFormatError(str(exc)) from exc
    return data


def _normalized_mcp_provenance(
    provenance: dict[str, str | list[str]],
) -> dict[str, str | list[str]]:
    """Return deterministic MCP provenance with list-valued owners sorted."""
    return {
        server: sorted(owners) if isinstance(owners, list) else owners
        for server, owners in sorted(provenance.items())
    }


def _json_identity(value: Any) -> str:
    """Serialize *value* so JSON type changes (``1`` vs ``true``) compare unequal."""
    return json.dumps(value, sort_keys=True, default=str)


def _normalize_lockfile_host_type(raw: Any) -> str | None:
    """Validate and normalize the optional lockfile host_type field."""
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("lockfile host_type must be a non-empty string")
    value = raw.strip().lower()
    if value not in _ALLOWED_HOST_TYPES:
        raise ValueError(
            f"Unsupported lockfile host_type: {raw}. Supported values: "
            f"{', '.join(sorted(_ALLOWED_HOST_TYPES))}"
        )
    return value


def _normalize_exec_status(raw: Any) -> str | None:
    """Validate and normalize the optional executable-trust status."""
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("lockfile exec_status must be a non-empty string")
    value = raw.strip()
    if value not in _ALLOWED_EXEC_STATUS:
        raise ValueError(
            f"Unsupported lockfile exec_status: {raw}. Supported values: "
            f"{', '.join(sorted(_ALLOWED_EXEC_STATUS))}"
        )
    return value


def _normalize_exec_allowed_types(raw: Any) -> tuple[str, ...]:
    """Validate the executable types a partially gated row deployed (#3991)."""
    if raw is None:
        return ()
    from apm_cli.security.executables import ALL_EXEC_TYPES

    if not isinstance(raw, list) or any(item not in ALL_EXEC_TYPES for item in raw):
        raise ValueError(
            "lockfile exec_allowed_types must be a list of executable types: "
            f"{', '.join(ALL_EXEC_TYPES)}"
        )
    return tuple(sorted(set(raw)))


def _dedupe_preserving_order(values: list[str]) -> list[str]:
    """Return values without duplicates, preserving first-seen order."""
    return list(dict.fromkeys(values))


@dataclass
class LockedDependency:
    """A resolved dependency with exact commit/version information."""

    repo_url: str
    materialization_repo_url: str | None = None
    host: str | None = None
    host_type: str | None = None
    port: int | None = None  # Non-standard SSH/HTTPS port (e.g. 7999 for Bitbucket DC)
    registry_prefix: str | None = None  # Registry path prefix, e.g. "artifactory/github"
    resolved_commit: str | None = None
    resolved_ref: str | None = None
    version: str | None = None
    virtual_path: str | None = None
    is_virtual: bool = False
    depth: int = 1
    resolved_by: str | None = None
    package_type: str | None = None
    deployed_files: list[str] = field(default_factory=list)
    deployed_file_hashes: dict[str, str] = field(default_factory=dict)
    source: str | None = None  # "local" for local deps, None/absent for remote
    local_path: str | None = None  # Original local path. Direct deps: relative to
    # the project root (``./packages/foo``). Transitive deps: relative to the
    # package that declared them (``../sibling``), anchored via ``resolved_by``
    # (issue #857; see apm_cli.deps.path_anchoring).
    declaring_parent: str | None = None
    anchored_local_path: str | None = None
    content_hash: str | None = None  # SHA-256 of package file tree
    is_dev: bool = False  # True for devDependencies
    discovered_via: str | None = None  # Marketplace name (provenance)
    marketplace_plugin_name: str | None = None  # Plugin name in marketplace
    source_url: str | None = None  # Canonical marketplace source URL
    source_digest: str | None = None  # sha256 digest of the marketplace manifest
    is_insecure: bool = False  # True when the locked source was http://
    allow_insecure: bool = False  # True when the manifest explicitly allowed HTTP
    skill_subset: list[str] = field(default_factory=list)  # Sorted skill names for SKILL_BUNDLE
    target_subset: list[str] = field(default_factory=list)  # Audit-only consumer target subset
    # Exact per-edge skills x targets pairing (req-rs-018), recorded only when
    # skill_subset x target_subset would over-select. Decode via ``selection()``.
    subset_pairings: list[dict[str, list[str]]] | None = None

    # Registry resolver fields (design §6.1).
    # Populated when source == "registry"; absent otherwise. resolved_hash is
    # the sole non-negotiable trust anchor on every install — bytes are fetched
    # from resolved_url and re-verified against this digest.
    resolved_url: str | None = None
    resolved_hash: str | None = None

    # Git-source semver resolution fields (issue #1488).
    # Populated when a git-source dependency carried a semver range
    # (e.g. ``^1.2.0``) that the install pipeline resolved against the
    # remote's tags. Lockfile version stays at "2" -- these fields are
    # purely additive and forward-compatible with old readers (which
    # ignore unknown keys via the explicit ``from_dict`` allowlist).
    constraint: str | None = None
    resolved_tag: str | None = None
    resolved_at: str | None = None

    # Declared-license provenance (issue #1777, U6). The SPDX expression the
    # dependency's manifest DECLARED at resolved_commit (apm.yml ``license:``
    # or plugin.json ``license``). This is a passthrough of an author claim --
    # APM never reads the LICENSE file text or concludes a license. Absence
    # means "not declared" (unknown); it is OMITTED from the serialized entry
    # rather than stored as a sentinel, so absence stays distinguishable from
    # an explicit declaration.
    declared_license: str | None = None
    # Verbatim licence copies APM placed in the materialized package (#3556):
    # the nearest LICENSE/COPYING/NOTICE from the package path up to the
    # repository root. Each records the copy path, its repository source path
    # and its hash. The package content_hash excludes these copies. Omitted
    # when empty so packages that carry nothing stay byte-identical.
    carried_licenses: tuple[CarriedLicense, ...] = ()
    # Resolved executable-trust state (issue #1873). One of ``deployed`` |
    # ``gated_pending_approval`` | ``denied`` | ``absent``, mirroring the
    # resolver ``trust_state``. Absence (``None``) means the package declared
    # no executable primitive; it is OMITTED from the serialized entry so a
    # never-gated package stays distinguishable from an explicitly-cleared one.
    exec_status: str | None = None
    # Types a gated or denied package still deployed because they are approved
    # (#3991). ``exec_status`` is the worst case, so this is the only record of
    # a partial approval; omitted when no type was allowed.
    exec_allowed_types: tuple[str, ...] = ()
    # Package-declared name from the dependency's own apm.yml (issue #1888).
    # SELF-ASSERTED display/inventory metadata only -- NOT an identity anchor.
    # See to_dict/from_dict and the supply-chain boundary note in the lockfile
    # spec. Omitted from the serialized entry when absent.
    name: str | None = None
    alias: str | None = None
    # req-ext-003: vendor extension keys (x-*) from the dependency manifest,
    # in declaration order. Omitted from YAML when empty so lockfiles that
    # declare no extensions stay byte-identical. Values are not interpreted.
    extensions: dict[str, Any] = field(default_factory=dict)
    # Intact native plugin APM deployed and declared for this dependency; the
    # only ownership evidence for native settings keys and deployed roots.
    native_plugin: NativePluginRecord | None = None
    # Harnesses whose settings held APM's ``true`` for that plugin when this
    # lock was written: what APM last wrote, so it can tell a hand edit apart.
    native_declared: list[str] | None = None
    replacement: ReplacementBinding | None = field(default=None, repr=False)
    # Stream ``content_hash`` was recorded in; set from the owning lockfile's
    # version, never serialized per row (see ``FRAMED_CONTENT_HASH_VERSION``).
    content_hash_framed: bool = field(default=True, repr=False, compare=False)
    # Forward-compat carrier: keys we don't recognise are preserved
    # through a from_dict / to_dict round-trip so an older APM build
    # reading a lockfile written by a newer build doesn't silently drop
    # fields when it re-emits.
    _unknown_fields: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Separate canonical lock identity from materialization spelling."""
        try:
            self.alias = parse_alias_override(self.alias)
        except ValueError as exc:
            raise ValueError(
                f"Invalid lockfile dependency alias {self.alias!r}. Restore a known-good "
                "apm.lock.yaml from version control or correct this entry's alias "
                "to its intended placement. "
                "Do not delete content at a rejected alias destination."
            ) from exc
        original_repo_url = self.repo_url
        canonical_repo_url = normalize_package_repo_url(
            self.repo_url,
            host=self.host,
            source=self.source,
            registry_prefix=self.registry_prefix,
        )
        materialization_repo_url = self.materialization_repo_url or original_repo_url
        materialization_identity = normalize_package_repo_url(
            materialization_repo_url,
            host=self.host,
            source=self.source,
            registry_prefix=self.registry_prefix,
        )
        if materialization_identity != canonical_repo_url:
            raise ValueError(
                f"materialization_repo_url {materialization_repo_url!r} does not "
                f"identify the same package as repo_url {self.repo_url!r}"
            )
        self.repo_url = canonical_repo_url
        self.materialization_repo_url = (
            materialization_repo_url if materialization_repo_url != canonical_repo_url else None
        )

    @property
    def canonical_repo_url(self) -> str:
        """Return the repository identity; ``__post_init__`` already canonicalised it."""
        return self.repo_url

    def get_unique_key(self) -> str:
        """Returns unique key for this dependency."""
        return build_dependency_unique_key(
            self.repo_url,
            host=self.host,
            port=self.port,
            source=self.source,
            local_path=self.local_path,
            is_virtual=self.is_virtual,
            virtual_path=self.virtual_path,
            registry_prefix=self.registry_prefix,
            declaring_parent=self.declaring_parent,
            anchored_local_path=self.anchored_local_path,
        )

    def get_canonical_dependency_string(self) -> str:
        """Host-blind canonical key for filesystem / orphan-detection matching.

        Mirrors :meth:`DependencyReference.get_canonical_dependency_string`:
        returns the bare ``repo_url`` (+ ``virtual_path``), never host-qualified,
        so it matches the host-blind ``apm_modules/`` layout. Use
        :meth:`get_unique_key` for the host-qualified lockfile dedup key.
        """
        return build_canonical_dependency_string(
            self.repo_url,
            is_local=(self.source == "local"),
            local_path=self.local_path,
            is_virtual=self.is_virtual,
            virtual_path=self.virtual_path,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict for YAML output."""
        result: dict[str, Any] = {"repo_url": self.repo_url}
        if self.materialization_repo_url:
            result["materialization_repo_url"] = self.materialization_repo_url
        if self.alias is not None:
            result["alias"] = self.alias
        if self.name is not None:
            result["name"] = self.name
        if self.host:
            result["host"] = self.host
        if self.host_type:
            result["host_type"] = self.host_type
        if self.port:
            result["port"] = self.port
        if self.registry_prefix:
            result["registry_prefix"] = self.registry_prefix
        if self.resolved_commit:
            result["resolved_commit"] = self.resolved_commit
        if self.resolved_ref:
            result["resolved_ref"] = self.resolved_ref
        if self.version:
            result["version"] = self.version
        if self.virtual_path:
            result["virtual_path"] = self.virtual_path
        if self.is_virtual:
            result["is_virtual"] = self.is_virtual
        if self.depth != 1:
            result["depth"] = self.depth
        if self.resolved_by:
            result["resolved_by"] = self.resolved_by
        if self.package_type:
            result["package_type"] = self.package_type
        if self.deployed_files:
            result["deployed_files"] = sorted(_dedupe_preserving_order(self.deployed_files))
        if self.deployed_file_hashes:
            result["deployed_file_hashes"] = dict(sorted(self.deployed_file_hashes.items()))
        if self.source:
            result["source"] = self.source
        if self.local_path:
            result["local_path"] = self.local_path
        if self.declaring_parent:
            result["declaring_parent"] = self.declaring_parent
        if self.anchored_local_path:
            result["anchored_local_path"] = self.anchored_local_path
        if self.content_hash:
            result["content_hash"] = self.content_hash
        if self.is_dev:
            result["is_dev"] = True
        if self.discovered_via:
            result["discovered_via"] = self.discovered_via
        if self.marketplace_plugin_name:
            result["marketplace_plugin_name"] = self.marketplace_plugin_name
        if self.source_url:
            result["source_url"] = self.source_url
        if self.source_digest:
            result["source_digest"] = self.source_digest
        if self.is_insecure:
            result["is_insecure"] = True
        if self.allow_insecure:
            result["allow_insecure"] = True
        if self.skill_subset:
            result["skill_subset"] = sorted(self.skill_subset)
        if self.target_subset:
            result["target_subset"] = sorted(self.target_subset)
        if self.subset_pairings:
            result["subset_pairings"] = _copy_subset_pairings(self.subset_pairings)
        if self.resolved_url:
            result["resolved_url"] = self.resolved_url
        if self.resolved_hash:
            result["resolved_hash"] = self.resolved_hash
        if self.constraint:
            result["constraint"] = self.constraint
        if self.resolved_tag:
            result["resolved_tag"] = self.resolved_tag
        if self.resolved_at:
            result["resolved_at"] = self.resolved_at
        if self.declared_license:
            result["declared_license"] = self.declared_license
        if self.carried_licenses:
            result["carried_licenses"] = records_to_lock_data(self.carried_licenses)
        if self.extensions:
            result["extensions"] = dict(self.extensions)
        if self.exec_status:
            result["exec_status"] = self.exec_status
        if self.native_plugin is not None:
            result["native_plugin"] = self.native_plugin.to_lock_data()
            if self.native_declared is not None:
                result["native_declared"] = sorted(self.native_declared)
        if self.exec_allowed_types:
            result["exec_allowed_types"] = list(self.exec_allowed_types)
        if self.replacement is not None:
            result["replacement"] = self.replacement.to_dict()
        # Replay forward-compat unknown fields LAST so they never shadow a
        # known field that this build understands.
        for k, v in self._unknown_fields.items():
            result.setdefault(k, v)
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LockedDependency:
        """Deserialize from dict.

        Handles backwards compatibility:
        - Old ``deployed_skills`` lists are migrated to ``deployed_files``
          paths under ``.github/skills/`` and ``.claude/skills/``.
        """
        deployed_files = _dedupe_preserving_order(list(data.get("deployed_files", [])))

        # Migrate legacy deployed_skills -> deployed_files
        old_skills = data.get("deployed_skills", [])
        if old_skills and not deployed_files:
            for skill_name in old_skills:
                deployed_files.append(f".github/skills/{skill_name}/")
                deployed_files.append(f".claude/skills/{skill_name}/")

        # Defensive cast: reject non-numeric or out-of-range ports from tampered lockfiles.
        _p_raw = data.get("port")
        port: int | None = None
        if _p_raw is not None:
            try:
                _p_int = int(_p_raw)
            except (TypeError, ValueError):
                _p_int = None
            if _p_int is not None and 1 <= _p_int <= 65535:
                port = _p_int

        host_type = _normalize_lockfile_host_type(data.get("host_type"))
        exec_status = _normalize_exec_status(data.get("exec_status"))

        # Recognised keys this build knows about. Anything else is captured
        # as ``_unknown_fields`` so a re-emit preserves forward-introduced
        # fields rather than silently dropping them. ``deployed_skills`` is
        # the explicit legacy key handled above; do NOT consider it unknown.
        _known_keys = {
            "repo_url",
            "materialization_repo_url",
            "alias",
            "host",
            "host_type",
            "port",
            "registry_prefix",
            "resolved_commit",
            "resolved_ref",
            "version",
            "virtual_path",
            "is_virtual",
            "depth",
            "resolved_by",
            "package_type",
            "deployed_files",
            "deployed_file_hashes",
            "source",
            "local_path",
            "declaring_parent",
            "anchored_local_path",
            "content_hash",
            "is_dev",
            "discovered_via",
            "marketplace_plugin_name",
            "source_url",
            "source_digest",
            "is_insecure",
            "allow_insecure",
            "skill_subset",
            "target_subset",
            "subset_pairings",
            "resolved_url",
            "resolved_hash",
            "constraint",
            "resolved_tag",
            "resolved_at",
            "declared_license",
            "carried_licenses",
            "extensions",
            "exec_status",
            "exec_allowed_types",
            "name",
            "native_plugin",
            "native_declared",
            "replacement",
            # legacy migration key handled above
            "deployed_skills",
        }
        unknown_fields = {
            k: v
            for k, v in data.items()
            if k not in _known_keys and not DependencyReference.is_transient_provider_field(k)
        }

        locked = cls(
            repo_url=data["repo_url"],
            materialization_repo_url=data.get("materialization_repo_url"),
            alias=data.get("alias"),
            host=data.get("host"),
            host_type=host_type,
            port=port,
            registry_prefix=data.get("registry_prefix"),
            resolved_commit=data.get("resolved_commit"),
            resolved_ref=data.get("resolved_ref"),
            version=data.get("version"),
            virtual_path=data.get("virtual_path"),
            is_virtual=data.get("is_virtual", False),
            depth=data.get("depth", 1),
            resolved_by=data.get("resolved_by"),
            package_type=data.get("package_type"),
            deployed_files=deployed_files,
            deployed_file_hashes=dict(data.get("deployed_file_hashes") or {}),
            source=data.get("source"),
            local_path=data.get("local_path"),
            declaring_parent=data.get("declaring_parent"),
            anchored_local_path=data.get("anchored_local_path"),
            content_hash=data.get("content_hash"),
            is_dev=data.get("is_dev", False),
            discovered_via=data.get("discovered_via"),
            marketplace_plugin_name=data.get("marketplace_plugin_name"),
            source_url=data.get("source_url"),
            source_digest=data.get("source_digest"),
            is_insecure=data.get("is_insecure", False),
            allow_insecure=data.get("allow_insecure", False),
            skill_subset=list(data.get("skill_subset") or []),
            # Locks written before canonical recording may hold ``vscode``.
            target_subset=canonical_target_subset(data.get("target_subset")),
            subset_pairings=_copy_subset_pairings(data.get("subset_pairings")),
            resolved_url=data.get("resolved_url"),
            resolved_hash=data.get("resolved_hash"),
            constraint=data.get("constraint"),
            resolved_tag=data.get("resolved_tag"),
            resolved_at=data.get("resolved_at"),
            declared_license=data.get("declared_license"),
            carried_licenses=records_from_lock_data(data.get("carried_licenses")),
            exec_status=exec_status,
            exec_allowed_types=_normalize_exec_allowed_types(data.get("exec_allowed_types")),
            name=data.get("name"),
            extensions=_copy_extension_map(data.get("extensions")),
            _unknown_fields=unknown_fields,
        )
        if "native_plugin" in data:
            try:
                locked.native_plugin = NativePluginRecord.from_lock_data(data["native_plugin"])
            except NativePluginRecordError as exc:
                raise LockfileFormatError(f"Invalid native_plugin record: {exc}") from exc
            declared = data.get("native_declared")
            if isinstance(declared, list):
                locked.native_declared = sorted({h for h in declared if isinstance(h, str)})
        if "replacement" in data:
            try:
                locked.replacement = ReplacementBinding.from_dict(
                    data["replacement"], locked.to_dependency_ref()
                )
            except ReplacementConfigError as exc:
                raise LockfileFormatError(f"Invalid replacement evidence: {exc}") from exc
            if locked.to_dependency_ref().is_local and (
                locked.resolved_ref or locked.resolved_commit
            ):
                raise LockfileFormatError("Local replacement must not carry a Git ref or commit")
        return locked

    @classmethod
    def from_dependency_ref(
        cls,
        dep_ref: DependencyReference,
        resolved_commit: str | None,
        depth: int,
        resolved_by: str | None,
        is_dev: bool = False,
        registry_config=None,
        registry_resolution=None,
        git_semver_resolution=None,
        package_name: str | None = None,
        package_version: str | None = None,
        extensions: dict[str, Any] | None = None,
    ) -> LockedDependency:
        """Create from a DependencyReference with resolution info.

        Args:
            dep_ref: The resolved dependency reference.
            resolved_commit: Exact commit SHA that was installed, or ``None``.
            depth: Dependency tree depth.
            resolved_by: Parent repo URL, or ``None`` for direct dependencies.
            is_dev: Whether this is a dev-only dependency.
            registry_config: Optional :class:`~apm_cli.deps.registry_proxy.RegistryConfig`
                used for this download (Artifactory VCS proxy — pre-existing
                concept, distinct from the new dedicated-registry resolver).
                When provided, ``host`` is set to the pure FQDN and
                ``registry_prefix`` to the URL path prefix.
            registry_resolution: Optional :class:`~apm_cli.deps.registry.resolver.RegistryResolution`
                produced by the dedicated-registry resolver. When provided,
                ``source`` is set to ``"registry"`` and ``resolved_url`` /
                ``resolved_hash`` / ``version`` are populated from it (the
                trust anchor for re-installs per design §6.1).
            git_semver_resolution: Optional
                :class:`~apm_cli.deps.git_semver_resolver.GitSemverResolution`
                produced when a git-source dep had a semver range as ``ref:``.
                When provided, ``constraint`` / ``resolved_tag`` /
                ``resolved_at`` are populated and ``resolved_ref`` is set
                to the concrete tag (issue #1488). Mutually exclusive with
                ``registry_resolution``.

        Raises:
            ValueError: When both ``registry_resolution`` and
                ``git_semver_resolution`` are provided. The two resolution
                paths are mutually exclusive: a dependency is either
                registry-sourced (carries ``resolved_url`` / ``resolved_hash``)
                or git-source with a semver range (carries ``constraint`` /
                ``resolved_tag`` / ``resolved_at``). Combining both would
                produce an inconsistent lockfile entry (e.g. ``source=registry``
                while ``resolved_ref`` is overridden to a git tag).
        """
        if registry_resolution is not None and git_semver_resolution is not None:
            raise ValueError(
                "registry_resolution and git_semver_resolution are mutually "
                "exclusive: a dependency is either registry-sourced or a "
                "git-source semver resolution, not both."
            )
        binding = dep_ref.replacement
        if binding is not None:
            if registry_resolution is not None or registry_config is not None:
                raise ValueError("Replacement sources cannot use a registry resolution")
            locked = cls.from_dependency_ref(
                dep_ref=binding.original,
                resolved_commit=resolved_commit,
                depth=depth,
                resolved_by=resolved_by,
                is_dev=is_dev,
                git_semver_resolution=git_semver_resolution,
                package_name=package_name,
                package_version=package_version,
                extensions=extensions,
            )
            locked.replacement = ReplacementBinding.from_dict(binding.to_dict(), binding.original)
            locked.resolved_ref = (
                git_semver_resolution.resolved_tag
                if git_semver_resolution is not None
                else dep_ref.reference
            )
            return locked
        if registry_config is not None:
            host = registry_config.host
            registry_prefix = registry_config.prefix
        else:
            host = dep_ref.host
            registry_prefix = None

        # Determine source: explicit registry resolution wins; else local;
        # else inherit from dep_ref.source (which may be "git" or None).
        if registry_resolution is not None:
            source = "registry"
        elif dep_ref.is_local:
            source = "local"
        else:
            source = None

        # Prefer the concrete resolved identifier for ``resolved_ref`` so that
        # ``build_update_plan`` can detect real version changes by comparing
        # old_ref (locked concrete) vs new_ref (freshly resolved concrete).
        # Registry deps: store the resolved version (e.g. "1.0.3"), not the
        # range ("^1.0.0").  Git-semver deps: store the resolved tag.  Both
        # preserve the original selector in their dedicated fields
        # (``version`` / ``constraint`` respectively).
        if git_semver_resolution is not None:
            resolved_ref_val: str | None = git_semver_resolution.resolved_tag
        elif registry_resolution is not None:
            resolved_ref_val = registry_resolution.version
        else:
            resolved_ref_val = dep_ref.reference

        dep_ref.validate_provider_coordinates()
        if registry_resolution is not None:
            version_value = registry_resolution.version
        elif git_semver_resolution is not None:
            version_value = git_semver_resolution.resolved_version
        elif source != "registry":
            version_value = package_version
        else:
            version_value = None

        canonical_repo_url = normalize_package_repo_url(
            dep_ref.repo_url,
            host=dep_ref.host,
            source="local" if dep_ref.is_local else dep_ref.source,
            registry_prefix=dep_ref.artifactory_prefix,
            is_local=dep_ref.is_local,
            is_marketplace=dep_ref.is_marketplace,
        )
        return cls(
            repo_url=canonical_repo_url,
            alias=dep_ref.alias,
            materialization_repo_url=(
                dep_ref.repo_url if dep_ref.repo_url != canonical_repo_url else None
            ),
            host=host,
            host_type=dep_ref.host_type,
            port=dep_ref.port,
            registry_prefix=registry_prefix,
            resolved_commit=resolved_commit,
            resolved_ref=resolved_ref_val,
            version=version_value,
            virtual_path=dep_ref.virtual_path,
            is_virtual=dep_ref.is_virtual,
            depth=depth,
            resolved_by=resolved_by,
            source=source,
            local_path=dep_ref.local_path if dep_ref.is_local else None,
            declaring_parent=dep_ref.declaring_parent if dep_ref.is_local else None,
            anchored_local_path=dep_ref.anchored_local_path if dep_ref.is_local else None,
            is_dev=is_dev,
            is_insecure=dep_ref.is_insecure,
            allow_insecure=dep_ref.allow_insecure,
            skill_subset=sorted(dep_ref.skill_subset)
            if isinstance(getattr(dep_ref, "skill_subset", None), list)
            else [],
            target_subset=canonical_target_subset(dep_ref.target_subset)
            if isinstance(getattr(dep_ref, "target_subset", None), list)
            else [],
            subset_pairings=(
                dep_ref.subset_pairings.to_lock_records()
                if getattr(dep_ref, "subset_pairings", None) is not None
                else None
            ),
            resolved_url=(
                registry_resolution.resolved_url if registry_resolution is not None else None
            ),
            resolved_hash=(
                registry_resolution.resolved_hash if registry_resolution is not None else None
            ),
            constraint=(
                git_semver_resolution.constraint if git_semver_resolution is not None else None
            ),
            resolved_tag=(
                git_semver_resolution.resolved_tag if git_semver_resolution is not None else None
            ),
            resolved_at=(
                git_semver_resolution.resolved_at if git_semver_resolution is not None else None
            ),
            name=package_name,
            extensions=_copy_extension_map(extensions),
        )

    def to_dependency_ref(self, *, effective: bool = True) -> DependencyReference:
        """Reconstruct a DependencyReference from this locked dependency.

        Replacement rows return the selected source by default, including
        its host for transport and provider checks. ``effective=False``
        returns the original ownership coordinates instead.

        Registry-sourced deps come back with ``source="registry"`` so the
        install pipeline routes them to the registry resolver. The exact
        locked version is in ``reference`` (the registry resolver still calls
        /versions and the hash-check on download enforces the lockfile's
        intent).
        """
        # Registry deps: prefer the locked exact version over resolved_ref so
        # the resolver picks up the exact-version constraint, not the original
        # range (e.g. ``^1.2.0`` -> ``1.5.3``).
        is_registry = self.source == "registry"
        ref = self.version if (is_registry and self.version) else self.resolved_ref
        dep = DependencyReference(
            repo_url=self.materialization_repo_url or self.repo_url,
            alias=self.alias,
            host=self.host,
            host_type=self.host_type,
            port=self.port,
            reference=ref,
            virtual_path=self.virtual_path,
            is_virtual=self.is_virtual,
            artifactory_prefix=self.registry_prefix,
            is_local=(self.source == "local"),
            local_path=self.local_path,
            declaring_parent=self.declaring_parent,
            anchored_local_path=self.anchored_local_path,
            is_insecure=self.is_insecure,
            allow_insecure=self.allow_insecure,
            source=self.source,
            skill_subset=sorted(self.skill_subset) if self.skill_subset else None,
            target_subset=sorted(self.target_subset) if self.target_subset else None,
            subset_pairings=_replay_pairing(
                self.skill_subset, self.target_subset, self.subset_pairings
            ),
        ).with_derived_provider_coordinates()
        if not effective or self.replacement is None:
            return dep
        selected = project_effective_reference(self.replacement.original, self.replacement.rule)
        selected.replacement = self.replacement
        # The binding's original carries the merged flat selection; the exact
        # per-edge pairing comes from the lock entry (req-rs-018).
        selected.subset_pairings = dep.subset_pairings
        if not selected.is_local:
            selected.reference = self.resolved_ref or selected.reference
        return selected

    def to_edge_reference(self, edge: DependencyReference) -> DependencyReference:
        """Project this row's coordinates under a declaring edge's own selection.

        A marketplace edge stands for the locked coordinate it resolved to;
        its alias and skill/target selection stay the edge's, never the row's
        merged record.
        """
        return replace(
            self.to_dependency_ref(),
            alias=edge.alias,
            skill_subset=edge.skill_subset,
            target_subset=edge.target_subset,
            subset_pairings=edge.subset_pairings,
        )

    def selection(self) -> SkillTargetSelection:
        """Decode this entry's skills x targets selection through the owner.

        Raises ``ValueError`` for malformed ``subset_pairings`` or records
        that disagree with the flat fields (invalid lock state, req-rs-018).
        """
        return SkillTargetSelection.from_lock_fields(
            self.skill_subset, self.target_subset, self.subset_pairings
        )

    def matches_replacement(self, dep: DependencyReference) -> bool:
        """Whether this pin belongs to the same root-selected source contract."""
        binding = dep.replacement
        if binding is None or self.replacement is None:
            return binding is self.replacement
        return binding.rule == self.replacement.rule

    @property
    def parent_keys(self) -> tuple[str | None, ...]:
        """Return recorded incoming parents, including the root marker."""
        if self.replacement is None:
            return (self.resolved_by,)
        return tuple(dict.fromkeys(request.parent for request in self.replacement.requests))

    @property
    def is_local_source(self) -> bool:
        """Whether verification/replay uses local bytes, independent of ownership."""
        return (
            isinstance(self.replacement.rule.source, LocalReplacementSource)
            if self.replacement is not None
            else self.source == "local"
        )


def local_staging_slug(path: str) -> str | None:
    """Return the bundle slug when *path* is local staging under ``apm_modules/<slug>/.apm/``.

    Unsafe slugs (empty, ``.``, ``..``, separators or drive markers) are never a
    staging root; discovery also confines roots to ``apm_modules`` (defense in depth).
    """
    parts = Path(path).parts
    if len(parts) < 3 or parts[0] != "apm_modules" or parts[2] != ".apm":
        return None
    slug = parts[1]
    if slug in {"", ".", ".."} or any(sep in slug for sep in "/\\:"):
        return None
    return slug


@dataclass
class LockFile:
    """APM lock file for reproducible dependency resolution."""

    lockfile_version: str = "1"
    generated_at: str | None = None
    apm_version: str | None = None
    dependencies: dict[str, LockedDependency] = field(default_factory=dict)
    mcp_servers: list[str] = field(default_factory=list)
    mcp_configs: dict[str, dict] = field(default_factory=dict)
    mcp_target_servers: dict[str, list[str]] = field(default_factory=dict)
    # Provenance for transitively-contributed MCP servers: name -> declaring
    # package identity. Only servers NOT declared in the root manifest's mcp:
    # block appear here (absent == direct, mirroring the dependency-side
    # ``resolved_by is None`` convention). Kept OUT of ``mcp_configs`` values so
    # it never pollutes config comparisons. Consistency diagnostics use this as
    # ownership context only; provenance never exempts a lock-only server.
    mcp_config_provenance: dict[str, str | list[str]] = field(default_factory=dict)
    mcp_omissions: dict[str, Any] = field(default_factory=dict)
    lsp_servers: list[str] = field(default_factory=list)
    lsp_configs: dict[str, dict] = field(default_factory=dict)
    lsp_target_servers: dict[str, list[str]] = field(default_factory=dict)
    lsp_config_provenance: dict[str, str] = field(default_factory=dict)
    local_deployed_files: list[str] = field(default_factory=list)
    local_deployed_file_hashes: dict[str, str] = field(default_factory=dict)
    # Local bundle compile input under apm_modules/<slug>/.apm/ (#3705): proves
    # a staging root for ``apm compile`` without being a deployment claim.
    local_staged_files: list[str] = field(default_factory=list)
    # ``sha256:<hex>`` per staged path, from the deployed-file hash owner (#3718).
    local_staged_file_hashes: dict[str, str] = field(default_factory=dict)
    deployment_ledger: DeploymentLedger = field(
        default_factory=lambda: DeploymentLedger(records={})
    )
    _deployments_present: bool = field(default=False, repr=False, compare=False)
    _mcp_target_servers_present: bool = field(default=False, repr=False, compare=False)
    _lsp_target_servers_present: bool = field(default=False, repr=False, compare=False)
    _apm_version_null: bool = field(default=False, repr=False, compare=False)

    # Root replacement rules (issue #3180).  Stores the full normalized rule
    # table including dormant rules so frozen validation can compare declared
    # intent without reading unused sources.  ``None`` means no replacement
    # state; ``[]`` means explicitly empty (cleared rules).
    replacements: list[dict] | None = field(default=None, repr=False)
    # Whether package content hashes use the framed stream (lockfile version 4).
    framed_content_hashes: bool = field(default=True, repr=False, compare=False)

    def set_content_hash_stream(self, *, framed: bool) -> None:
        """Record which stream every package ``content_hash`` row uses."""
        self.framed_content_hashes = framed
        for dep in self.dependencies.values():
            dep.content_hash_framed = framed

    def add_dependency(self, dep: LockedDependency) -> None:
        """Add a dependency to the lock file.

        Adding a registry-sourced dep or a git-source dep with semver
        resolution fields promotes ``lockfile_version`` to ``"2"`` eagerly,
        keeping the in-memory state consistent with what ``to_yaml()``
        would emit (design section 6.1; issue #1488).
        """
        dep.deployed_files = _dedupe_preserving_order(dep.deployed_files)
        dep.content_hash_framed = self.framed_content_hashes
        self.dependencies[dep.get_unique_key()] = dep
        if dep.deployed_files or dep.deployed_file_hashes:
            from ..core.deployment_ledger import DeploymentLedgerCodec

            DeploymentLedgerCodec.invalidate_legacy_projection(self)
        if self.lockfile_version == "1" and (
            dep.source == "registry" or dep.constraint or dep.resolved_tag or dep.resolved_at
        ):
            self.lockfile_version = "2"

    def get_dependency(
        self,
        key: str,
        *,
        marketplace_index: dict[tuple[str, str], LockedDependency | None] | None = None,
    ) -> LockedDependency | None:
        """Look up a key, using a fresh provenance snapshot for marketplace keys.

        Batch consumers can supply one snapshot to keep lookup linear overall.
        No index is cached on this mutable lockfile.
        """
        if key.startswith("_marketplace/"):
            parts = key.split("/", 2)
            if len(parts) == 3:
                index = self.marketplace_index() if marketplace_index is None else marketplace_index
                return index.get((parts[1], parts[2]))
        return self.dependencies.get(key)

    def marketplace_index(self) -> dict[tuple[str, str], LockedDependency | None]:
        """Index recorded discovery labels, with None for ambiguous associations.

        These labels associate declarations with locked coordinates; they are
        not a catalog trust identity and never authorize a catalog refresh.
        """
        index: dict[tuple[str, str], LockedDependency | None] = {}
        for dep in self.dependencies.values():
            if dep.discovered_via and dep.marketplace_plugin_name:
                pair = (dep.discovered_via, dep.marketplace_plugin_name)
                index[pair] = None if pair in index else dep
        return index

    def get_dependency_by_marketplace(
        self, marketplace_name: str, plugin_name: str
    ) -> LockedDependency | None:
        """Find a locked dependency by its marketplace provenance.

        Fails closed (returns None) if no matching dependency is found or if
        multiple rows share the same marketplace provenance.
        """
        return self.marketplace_index().get((marketplace_name, plugin_name))

    def marketplace_rows(self, marketplace_name: str, plugin_name: str) -> list[LockedDependency]:
        """Return every row recorded for an alias/plugin pair, ambiguous ones included.

        Matching is case-insensitive, like the registry and catalog lookups the
        alias resolves through, so a recased alias cannot hide a locked row.
        """
        alias, plugin = marketplace_name.casefold(), plugin_name.casefold()
        return [
            dep
            for dep in self.dependencies.values()
            if (dep.discovered_via or "").casefold() == alias
            and (dep.marketplace_plugin_name or "").casefold() == plugin
        ]

    def rename_local_deployed_path(self, old_value: str, new_value: str) -> None:
        """Rename one locally deployed path and carry its content hash."""
        from ..core.deployment_ledger import DeploymentLedgerCodec

        DeploymentLedgerCodec.rename_local_deployed_path(self, old_value, new_value)

    def has_dependency(self, key: str) -> bool:
        """Check if a dependency exists."""
        return self.get_dependency(key) is not None

    def get_all_dependencies(self) -> list[LockedDependency]:
        """Get all dependencies sorted by depth then repo_url."""
        return sorted(self.dependencies.values(), key=lambda d: (d.depth, d.repo_url))

    def get_package_dependencies(self) -> list[LockedDependency]:
        """Get all dependencies excluding the virtual self-entry."""
        return [d for d in self.get_all_dependencies() if d.local_path != "."]

    def local_staging_slugs(self) -> list[str]:
        """Return bundle slugs whose ``apm_modules/<slug>/.apm/`` staging is lockfile-proven.

        Lockfiles written before #3705 recorded staging in
        ``local_deployed_files``; both views prove a root.
        """
        paths = (*self.local_staged_files, *self.local_deployed_files)
        return sorted({slug for path in paths if (slug := local_staging_slug(path))})

    def records_modules_content(self) -> bool:
        """Return whether this lockfile records content installed under ``apm_modules/``.

        Package dependencies and lockfile-proven local bundle staging both live
        there; the virtual self-entry and MCP/LSP servers do not (#3781).
        """
        return bool(self.get_package_dependencies() or self.local_staging_slugs())

    def absent_staging_slugs(self, modules_dir: Path) -> list[str]:
        """Return recorded staging slugs whose ``modules_dir/<slug>`` root no longer exists."""
        slugs = {slug for path in self.local_staged_files if (slug := local_staging_slug(path))}
        return sorted(slug for slug in slugs if not (modules_dir / slug).exists())

    def retire_local_staging(self, slugs: Collection[str]) -> None:
        """Forget staged compile input under removed ``apm_modules/<slug>`` roots."""
        removed = {("apm_modules", slug) for slug in slugs}
        self.local_staged_files = [
            path for path in self.local_staged_files if Path(path).parts[:2] not in removed
        ]
        self.retain_staged_hashes()

    def retain_staged_hashes(self) -> None:
        """Keep only hashes whose path is still recorded staging (#3718)."""
        staged = set(self.local_staged_files)
        self.local_staged_file_hashes = {
            path: digest for path, digest in self.local_staged_file_hashes.items() if path in staged
        }

    def carry_local_staging_from(self, other: LockFile) -> None:
        """Copy staging records and their hashes together from *other* (#3718)."""
        self.local_staged_files = list(other.local_staged_files)
        self.local_staged_file_hashes = dict(other.local_staged_file_hashes)
        self.retain_staged_hashes()

    def staged_hash_drift(self, project_root: Path) -> list[tuple[str, str, str]]:
        """Return ``(path, expected, actual)`` for staged files changed since install (#3718).

        Hashes come from the deployed-file owner (``compute_file_hash``). A
        path without a recorded hash (pre-#3718 lockfiles), outside a staging
        root, resolving outside ``apm_modules``, missing, or behind a symlinked
        component makes no drift claim: compile reads none.
        """
        from ..utils.content_hash import compute_file_hash
        from ..utils.path_security import (
            PathTraversalError,
            ensure_path_within,
            has_symlink_component,
        )

        drift: list[tuple[str, str, str]] = []
        modules_root = project_root / "apm_modules"
        for path in sorted(self.local_staged_files):
            expected = self.local_staged_file_hashes.get(path)
            if expected is None or local_staging_slug(path) is None or ".." in Path(path).parts:
                continue
            file_path = project_root / path
            try:
                ensure_path_within(file_path, modules_root)
            except PathTraversalError:
                continue
            # Compile discovery never follows links, so a linked component is not input.
            if has_symlink_component(project_root, file_path) or not file_path.is_file():
                continue
            actual = compute_file_hash(file_path)
            if actual != expected:
                drift.append((path, expected, actual))
        return drift

    def project_self_entry(self) -> LockedDependency:
        """Build the virtual self-entry for the project's own local content.

        The entry is an in-memory normalization of ``local_deployed_files``;
        it is never serialized as a dependency row.
        """
        return LockedDependency(
            repo_url="<self>",
            source="local",
            local_path=_SELF_KEY,
            is_dev=True,
            depth=0,
            deployed_files=list(self.local_deployed_files),
            deployed_file_hashes=dict(self.local_deployed_file_hashes),
        )

    def _needs_v2(self) -> bool:
        """Whether the resolved graph requires lockfile schema v2.

        Per design section 6.1 (and invariant 2.1.4): bump opportunistically --
        only when at least one dep is sourced from a dedicated registry, OR
        when at least one dep carries git-source semver resolution fields
        (``constraint`` / ``resolved_tag`` / ``resolved_at`` -- issue #1488).
        A project that uses neither feature keeps v1 forever, even on a
        newer client.
        """
        for d in self.dependencies.values():
            if d.source == "registry":
                return True
            if d.constraint or d.resolved_tag or d.resolved_at:
                return True
        return False

    def _needs_v3(self) -> bool:
        """Whether replacement state requires lockfile schema v3 (issue #3180).

        The version bump is content-driven: only when replacement rules exist
        (including an explicitly empty list) does the lockfile require v3.
        Verified v0.25.0, v0.32.0 and v0.33.0 readers reject this version;
        v0.24.1 does not provide that fail-closed guarantee.
        """
        return self.replacements is not None or any(
            dep.replacement is not None for dep in self.dependencies.values()
        )

    def _validate_replacement_evidence(self) -> None:
        """Keep per-entry source evidence bound to the complete root rule table."""
        try:
            index = build_replacement_index(parse_replace_block(self.replacements or []))
            for dep in self.dependencies.values():
                rule = index.lookup(dep.to_dependency_ref(effective=False))
                evidence = dep.replacement
                if (rule is None) != (evidence is None) or (
                    evidence is not None and evidence.rule != rule
                ):
                    raise ReplacementConfigError(
                        f"Replacement evidence for {dep.get_unique_key()} disagrees with root replacements"
                    )
                if self.replacements is not None:
                    if any(
                        parent is not None and parent not in self.dependencies
                        for parent in dep.parent_keys
                    ):
                        raise ReplacementConfigError(
                            f"Replacement graph parent for {dep.get_unique_key()} is absent from the lockfile"
                        )
        except ReplacementConfigError as exc:
            raise LockfileFormatError(str(exc)) from exc

    def _required_lockfile_version(self) -> str:
        """Return the minimum schema version required by current lock state."""
        if self.framed_content_hashes and any(
            dep.content_hash for dep in self.dependencies.values()
        ):
            return FRAMED_CONTENT_HASH_VERSION
        if self._needs_v3():
            return "3"
        return "2" if self._needs_v2() else "1"

    def sync_lockfile_version(self) -> None:
        """Re-derive ``lockfile_version`` from current content.

        Callers that mutate content directly (instead of through
        ``add_dependency``) use this to keep the in-memory version equal to
        what ``to_yaml`` would emit before comparing locks (issue #3433).
        """
        self.lockfile_version = self._required_lockfile_version()

    def to_yaml(self) -> str:
        """Serialize to YAML string."""
        from ..core.deployment_ledger import DeploymentLedgerCodec

        self._validate_replacement_evidence()

        if not self.deployment_ledger.records:
            self.deployment_ledger = DeploymentLedgerCodec.from_lockfile(self)
        DeploymentLedgerCodec.apply_to_lockfile(self.deployment_ledger, self)
        # Content-driven v1<->v2 derivation:
        # the lockfile_version field always reflects current content at
        # emit time. ``add_dependency`` bumps to "2" eagerly, but callers
        # that mutate content directly need the field re-derived here.
        self.sync_lockfile_version()
        emit_version = self.lockfile_version
        # The synthesized self-entry (key ".") is an in-memory normalization
        # of the flat local_deployed_files / local_deployed_file_hashes
        # fields. It must not be written back into the dependencies list,
        # since the flat fields remain the source of truth in YAML.
        _self_dep = self.dependencies.pop(_SELF_KEY, None)
        try:
            data: dict[str, Any] = {
                "lockfile_version": emit_version,
            }
            if self.generated_at is not None:
                data["generated_at"] = self.generated_at
            if self.apm_version:
                data["apm_version"] = self.apm_version
            data["dependencies"] = [dep.to_dict() for dep in self.get_all_dependencies()]
            data["deployments"] = DeploymentLedgerCodec.rows(self.deployment_ledger)
            if self.mcp_servers:
                data["mcp_servers"] = sorted(self.mcp_servers)
            if self.mcp_configs:
                data["mcp_configs"] = dict(sorted(self.mcp_configs.items()))
            if self.mcp_omissions:
                data["mcp_omissions"] = dict(sorted(self.mcp_omissions.items()))
            if self.mcp_target_servers or self._mcp_target_servers_present:
                data["mcp_target_servers"] = {
                    target: sorted(servers)
                    for target, servers in sorted(self.mcp_target_servers.items())
                }
            if self.mcp_config_provenance:
                data["mcp_config_provenance"] = _normalized_mcp_provenance(
                    self.mcp_config_provenance
                )
            if self.lsp_servers:
                data["lsp_servers"] = sorted(self.lsp_servers)
            if self.lsp_configs:
                data["lsp_configs"] = dict(sorted(self.lsp_configs.items()))
            if self.lsp_target_servers:
                data["lsp_target_servers"] = {
                    target: sorted(servers)
                    for target, servers in sorted(self.lsp_target_servers.items())
                }
            if self.lsp_config_provenance:
                data["lsp_config_provenance"] = dict(sorted(self.lsp_config_provenance.items()))
            if self.local_deployed_files:
                data["local_deployed_files"] = sorted(self.local_deployed_files)
            if self.local_deployed_file_hashes:
                data["local_deployed_file_hashes"] = dict(
                    sorted(self.local_deployed_file_hashes.items())
                )
            if self.local_staged_files:
                data["local_staged_files"] = sorted(self.local_staged_files)
            if self.local_staged_file_hashes:
                data["local_staged_file_hashes"] = dict(
                    sorted(self.local_staged_file_hashes.items())
                )
            if self.replacements is not None:
                data["replacements"] = self.replacements
            from ..utils.yaml_io import yaml_to_str

            return yaml_to_str(data)
        finally:
            if _self_dep is not None:
                self.dependencies[_SELF_KEY] = _self_dep

    @classmethod
    def from_yaml(cls, yaml_str: str) -> LockFile:
        """Deserialize from YAML string."""
        from ..utils.yaml_io import load_yaml_str

        # Bounded loader: an untrusted bundle apm.lock.yaml cannot wedge the
        # parser with a merge-key bomb (the surrounding LockFile.read guard
        # cannot catch a non-terminating safe_load loop -- it can catch the
        # YAMLError this raises instead).
        try:
            loaded = load_yaml_str(yaml_str)
        except (yaml.YAMLError, ValueError) as exc:
            raise LockfileFormatError(f"Invalid lockfile YAML: {exc}") from exc
        data = _validate_lockfile_container(loaded)
        return cls._from_validated_data(data)

    def _canonicalize_legacy_parents(self) -> None:
        """Point a mixed-case legacy ``resolved_by`` at its locked parent's identity.

        Locks written before parents were recorded canonically (#3698) may name
        a case-insensitive parent ``Owner/Repo`` while its entry is ``owner/repo``.
        """
        known = {dep.repo_url for dep in self.dependencies.values()}
        canonical = {
            dep.repo_url.casefold(): dep.repo_url
            for dep in self.dependencies.values()
            if is_case_insensitive_package_identity(
                host=dep.host, source=dep.source, registry_prefix=dep.registry_prefix
            )
        }
        for dep in self.dependencies.values():
            if dep.resolved_by and dep.resolved_by not in known:
                dep.resolved_by = canonical.get(dep.resolved_by.casefold(), dep.resolved_by)

    def _rekey_port_blind_parents(self) -> None:
        """Point a pre-#3927 port-blind v3 parent key at its one ported entry."""
        ported: dict[str, list[str]] = {}
        for key, dep in self.dependencies.items():
            if dep.port:
                ported.setdefault(build_port_blind_dependency_key(dep), []).append(key)

        def rekey(parent: str | None) -> str | None:
            matches = ported.get(parent or "", [])
            if parent in self.dependencies or len(matches) != 1:
                return parent
            return matches[0]

        for dep in self.dependencies.values() if ported else ():
            dep.resolved_by = rekey(dep.resolved_by)
            if dep.replacement is not None:
                dep.replacement.requests = {
                    replace(request, parent=rekey(request.parent)): None
                    for request in dep.replacement.requests
                }

    @classmethod
    def _from_validated_data(cls, data: dict[str, Any]) -> LockFile:
        """Construct a lockfile from an already validated YAML mapping."""
        lock = cls(
            lockfile_version=data.get("lockfile_version", "1"),
            generated_at=data.get("generated_at"),
            apm_version=data.get("apm_version"),
            framed_content_hashes=data.get("lockfile_version") == FRAMED_CONTENT_HASH_VERSION,
        )
        lock._apm_version_null = "apm_version" in data and data["apm_version"] is None
        for dep_data in data.get("dependencies", []):
            lock.add_dependency(LockedDependency.from_dict(dep_data))
        lock.mcp_servers = list(data.get("mcp_servers", []))
        lock.mcp_configs = dict(data.get("mcp_configs") or {})
        lock.mcp_omissions = dict(data.get("mcp_omissions") or {})
        lock.mcp_target_servers = {
            target: list(servers)
            for target, servers in (data.get("mcp_target_servers") or {}).items()
        }
        lock._mcp_target_servers_present = "mcp_target_servers" in data
        lock.mcp_config_provenance = dict(data.get("mcp_config_provenance") or {})
        lock.lsp_servers = list(data.get("lsp_servers", []))
        lock.lsp_configs = dict(data.get("lsp_configs") or {})
        lock.lsp_target_servers = {
            target: list(servers)
            for target, servers in (data.get("lsp_target_servers") or {}).items()
        }
        lock._lsp_target_servers_present = "lsp_target_servers" in data
        lock.lsp_config_provenance = dict(data.get("lsp_config_provenance") or {})
        lock.local_deployed_files = list(data.get("local_deployed_files", []))
        lock.local_deployed_file_hashes = dict(data.get("local_deployed_file_hashes") or {})
        lock.local_staged_files = list(data.get("local_staged_files", []))
        lock.local_staged_file_hashes = dict(data.get("local_staged_file_hashes") or {})
        # Root replacement rules (issue #3180).  ``None`` = no replacement
        # section in lockfile; ``[]`` = explicitly cleared replacement state.
        raw_replacements = data.get("replacements")
        if "replacements" in data:
            if isinstance(raw_replacements, list):
                lock.replacements = raw_replacements
            else:
                raise LockfileFormatError("'replacements' must be a list")
        if lock._needs_v3() and data.get("lockfile_version", "1") not in {"3", "4"}:
            raise LockfileFormatError("Replacement evidence requires lockfile_version '3' or '4'")
        if lock.replacements is not None:
            lock._rekey_port_blind_parents()
        lock._validate_replacement_evidence()
        if lock.replacements is None:
            lock._canonicalize_legacy_parents()
        # Synthesize a virtual self-entry representing the project's own
        # local content. This unifies traversal across "real" dependencies
        # and the local package, without changing the on-disk YAML shape.
        if lock.local_deployed_files:
            lock.dependencies[_SELF_KEY] = lock.project_self_entry()
        from ..core.deployment_ledger import DeploymentLedgerCodec

        if "deployments" in data:
            deployment_rows = data["deployments"]
            lock.deployment_ledger = DeploymentLedgerCodec.from_rows(deployment_rows)
            lock._deployments_present = isinstance(deployment_rows, list) and (
                not deployment_rows or bool(lock.deployment_ledger.records)
            )
        else:
            lock.deployment_ledger = DeploymentLedgerCodec.from_lockfile(lock)
        return lock

    def write(
        self,
        path: Path,
        *,
        existing_lockfile: LockFile | None | _ExistingLockfileUnset = (_EXISTING_LOCKFILE_UNSET),
    ) -> bool:
        """Write lock file to disk, preserving legacy timestamp behavior.

        Inside :func:`suppress_lockfile_writes` (``apm install --frozen``,
        req-lk-006) the serialised content is recorded for the caller and
        the file on disk is left byte-for-byte untouched.  Returns whether
        the file was persisted, so callers never report a withheld write.

        New lockfiles omit ``generated_at``.  When the on-disk lockfile already
        carries the field, keep it stable for semantic no-ops and refresh it for
        substantive writes. This behavior should be changed to remove the legacy
        timestamp in a future APM version, but for now it preserves backward
        compatibility with older APM builds that expect the field. This method
        may mutate ``self.generated_at`` to preserve or refresh that metadata.

        A substantive write (or a new lockfile) records the running CLI in
        ``self.apm_version`` so the generator gate sees who last changed it;
        only a lock still carrying untranslated pre-0.37 MCP claims keeps its
        recorded version until the MCP phase translates them.
        Callers that already loaded the destination can pass ``existing_lockfile``
        to avoid parsing the same bytes again.
        """
        suppressed = _suppressed_writes.get()
        if suppressed is not None:
            suppressed.append(self.to_yaml())
            return False
        ensure_lockfile_writable(path)
        from ..utils.atomic_io import atomic_write_text, umask_file_mode
        from ..utils.staging_guard import assert_no_staging_paths
        from ..utils.yaml_io import load_yaml_str

        existing: LockFile | None
        legacy_timestamp_present = False
        if isinstance(existing_lockfile, _ExistingLockfileUnset) and path.exists():
            existing_text = path.read_text(encoding="utf-8")
            try:
                existing_data = load_yaml_str(existing_text)
            except (yaml.YAMLError, ValueError):
                existing_data = None
            existing = None
            if isinstance(existing_data, dict):
                legacy_timestamp_present = existing_data.get("generated_at") is not None
                try:
                    existing = type(self)._from_validated_data(
                        _validate_lockfile_container(existing_data)
                    )
                except UnsupportedLockfileVersionError:
                    if legacy_timestamp_present:
                        raise
                    existing = None
                except LockfileFormatError:
                    existing = None
        elif isinstance(existing_lockfile, _ExistingLockfileUnset):
            existing = None
        else:
            existing = existing_lockfile
        unchanged = existing is not None and self.is_semantically_equivalent(existing)
        if existing is not None and existing.generated_at is not None:
            if unchanged:
                self.generated_at = existing.generated_at
            else:
                self.generated_at = datetime.now(UTC).isoformat()
        elif legacy_timestamp_present:
            self.generated_at = datetime.now(UTC).isoformat()
        else:
            self.generated_at = None
        if unchanged:
            # A no-op keeps the recorded generator when this lock carries none
            # (a freshly built one); an explicit stamp, such as the MCP phase
            # recording that it translated pre-0.37 claims, is written.
            if self.apm_version is None:
                self.apm_version = existing.apm_version
        elif not _keeps_untranslated_mcp_stamp(self, existing):
            # The generator gate's only signal: whoever changes it owns it (#3962).
            self.apm_version = installed_apm_version()
        serialized = self.to_yaml()
        assert_no_staging_paths(serialized, path.name)
        if unchanged and _same_document_on_disk(path, serialized):
            # A proven no-op keeps the file's own bytes (comments, key style).
            return True
        atomic_write_text(path, serialized, new_file_mode=umask_file_mode(path))
        return True

    @classmethod
    def read(cls, path: Path) -> LockFile | None:
        """Read lock file from disk. Returns None when the file does not exist."""
        if not path.exists() and not path.is_symlink():
            return None
        try:
            text = path.read_text(encoding="utf-8")
            if has_conflict_markers(text):
                raise LockfileConflictError(path)
            lock = cls.from_yaml(text)
        except (LockfileFormatError, UnsupportedLockfileVersionError):
            raise
        except (yaml.YAMLError, ValueError, KeyError, TypeError) as exc:
            raise LockfileFormatError(f"Invalid lockfile at {path}: {exc}") from exc
        from .lockfile_generator import warn_if_newer

        # An explicit ``apm_version: null`` is unverifiable, unlike an absent one.
        warn_if_newer(path, "null" if lock._apm_version_null else lock.apm_version)
        return lock

    @classmethod
    def load_or_create(cls, path: Path) -> LockFile:
        """Load existing lock file or create a new one."""
        return cls.read(path) or cls()

    @classmethod
    def from_installed_packages(
        cls,
        installed_packages,
        dependency_graph,
    ) -> LockFile:
        """Create a lock file from installed packages.

        Args:
            installed_packages: List of
                :class:`~apm_cli.deps.installed_package.InstalledPackage`
                objects **or** legacy tuples of the form
                ``(dep_ref, resolved_commit, depth, resolved_by[, is_dev])``.
                The 5th tuple element is optional for backward compatibility.
            dependency_graph: The resolved DependencyGraph for additional metadata.
        """
        from .installed_package import InstalledPackage

        lock = cls(apm_version=installed_apm_version())

        for entry in installed_packages:
            registry_resolution = None
            git_semver_resolution = None
            if isinstance(entry, InstalledPackage):
                dep_ref = entry.dep_ref
                resolved_commit = entry.resolved_commit
                depth = entry.depth
                resolved_by = entry.resolved_by
                is_dev = entry.is_dev
                registry_config = getattr(entry, "registry_config", None)
                registry_resolution = getattr(entry, "registry_resolution", None)
                git_semver_resolution = getattr(entry, "git_semver_resolution", None)
            elif len(entry) >= 5:
                dep_ref, resolved_commit, depth, resolved_by, is_dev = entry[:5]
                registry_config = None
            else:
                dep_ref, resolved_commit, depth, resolved_by = entry[:4]
                is_dev = False
                registry_config = None

            locked_dep = LockedDependency.from_dependency_ref(
                dep_ref=dep_ref,
                resolved_commit=resolved_commit,
                depth=depth,
                resolved_by=resolved_by,
                is_dev=is_dev,
                registry_config=registry_config,
                registry_resolution=registry_resolution,
                git_semver_resolution=git_semver_resolution,
                package_name=getattr(entry, "package_name", None),
                package_version=getattr(entry, "package_version", None),
                extensions=getattr(entry, "extensions", None),
            )
            lock.add_dependency(locked_dep)

        return lock

    def get_installed_paths(self, apm_modules_dir: Path) -> list[str]:
        """Get relative installed paths for all dependencies in this lockfile.

        Computes expected installed paths for all dependencies, including
        transitive ones. Used by:
        - Primitive discovery to find all dependency primitives
        - Orphan detection to avoid false positives for transitive deps

        Args:
            apm_modules_dir: Path to the apm_modules directory.

        Returns:
            List[str]: POSIX-style relative installed paths (e.g., ['owner/repo']),
                       ordered by depth then repo_url (no duplicates).
        """
        seen: set = set()
        paths: list[str] = []
        for dep in self.get_all_dependencies():
            if dep.local_path == _SELF_KEY:
                continue
            dep_ref = dep.to_dependency_ref()
            install_path = dep_ref.get_install_path(apm_modules_dir)
            try:
                rel_path = install_path.relative_to(apm_modules_dir).as_posix()
            except ValueError:
                rel_path = Path(install_path).as_posix()
            if rel_path not in seen:
                seen.add(rel_path)
                paths.append(rel_path)
        return paths

    def save(
        self,
        path: Path,
        *,
        existing_lockfile: LockFile | None | _ExistingLockfileUnset = (_EXISTING_LOCKFILE_UNSET),
    ) -> bool:
        """Save lock file to disk (alias for write); return whether it persisted."""
        return self.write(path, existing_lockfile=existing_lockfile)

    def is_semantically_equivalent(self, other: LockFile) -> bool:
        """Return True if *other* has the same deps, MCP/LSP servers, and configs.

        Ignores ``generated_at`` and ``apm_version`` so that a no-change
        install does not dirty the lockfile.
        """
        if (
            self.lockfile_version != other.lockfile_version
            or self.replacements != other.replacements
        ):
            return False
        self_dependency_keys = set(self.dependencies).difference({_SELF_KEY})
        other_dependency_keys = set(other.dependencies).difference({_SELF_KEY})
        if self_dependency_keys != other_dependency_keys:
            return False
        for key in self_dependency_keys:
            dep = self.dependencies[key]
            other_dep = other.dependencies[key]
            if dep.to_dict() != other_dep.to_dict():
                return False
        if sorted(self.mcp_servers) != sorted(other.mcp_servers):
            return False
        # Python equality treats ``1`` and ``True`` as equal, which would keep a
        # stale drift baseline after a type-only change such as OpenCode's
        # ``enabled``.
        if _json_identity(self.mcp_configs) != _json_identity(other.mcp_configs) or _json_identity(
            self.mcp_omissions
        ) != _json_identity(other.mcp_omissions):
            return False
        if (
            self.mcp_target_servers != other.mcp_target_servers
            or (bool(self.mcp_target_servers) or self._mcp_target_servers_present)
            != (bool(other.mcp_target_servers) or other._mcp_target_servers_present)
            or dict(self.deployment_ledger.records) != dict(other.deployment_ledger.records)
        ):
            return False
        if _normalized_mcp_provenance(self.mcp_config_provenance) != _normalized_mcp_provenance(
            other.mcp_config_provenance
        ):
            return False
        if sorted(self.lsp_servers) != sorted(other.lsp_servers):
            return False
        if self.lsp_configs != other.lsp_configs:
            return False
        if self.lsp_target_servers != other.lsp_target_servers:
            return False
        if self.lsp_config_provenance != other.lsp_config_provenance:
            return False
        # Issue #887: include hash dict in equivalence so post-install
        # hash updates persist even when the file list is unchanged.
        return (
            sorted(self.local_deployed_files) == sorted(other.local_deployed_files)
            and dict(self.local_deployed_file_hashes) == dict(other.local_deployed_file_hashes)
            and sorted(self.local_staged_files) == sorted(other.local_staged_files)
            and dict(self.local_staged_file_hashes) == dict(other.local_staged_file_hashes)
        )

    @classmethod
    def installed_paths_for_project(cls, project_root: Path) -> list[str]:
        """Load apm.lock.yaml from project_root and return installed paths.

        Returns an empty list if the lockfile is missing, corrupt, or
        unreadable.

        Args:
            project_root: Path to project root containing apm.lock.yaml.

        Returns:
            List[str]: Relative installed paths (e.g., ['owner/repo']),
                       ordered by depth then repo_url (no duplicates).
        """
        try:
            lockfile_path = resolve_lockfile_path_for_read(project_root, read_only=True)
            lockfile = cls.read(lockfile_path)
            if not lockfile:
                return []
            return lockfile.get_installed_paths(project_root / "apm_modules")
        except (FileNotFoundError, yaml.YAMLError, ValueError, KeyError):
            return []


# Current lockfile filename (with .yaml extension for IDE syntax highlighting)
LOCKFILE_NAME = "apm.lock.yaml"
# Legacy lockfile filename used in older APM versions
LEGACY_LOCKFILE_NAME = "apm.lock"


def get_lockfile_path(project_root: Path) -> Path:
    """Get the path to the lock file for a project."""
    return project_root / LOCKFILE_NAME


def resolve_lockfile_path_for_read(project_root: Path, *, read_only: bool) -> Path:
    """Resolve the lockfile path, preserving legacy files for read-only callers."""
    if read_only:
        new_path = get_lockfile_path(project_root)
        legacy_path = project_root / LEGACY_LOCKFILE_NAME
        if (
            not new_path.exists()
            and not new_path.is_symlink()
            and (legacy_path.exists() or legacy_path.is_symlink())
        ):
            return legacy_path
        return new_path

    migrate_lockfile_if_needed(project_root)
    return get_lockfile_path(project_root)


def migrate_lockfile_if_needed(project_root: Path) -> bool:
    """Migrate legacy apm.lock to apm.lock.yaml if needed.

    Renames ``apm.lock`` to ``apm.lock.yaml`` when the new file does not yet
    exist.  This is a one-time, transparent migration for users upgrading from
    older APM versions.

    Args:
        project_root: Path to the project root directory.

    Returns:
        True if a migration was performed, False otherwise.
    """
    new_path = get_lockfile_path(project_root)
    legacy_path = project_root / LEGACY_LOCKFILE_NAME
    # A dangling symlink reports exists() == False; renaming onto it would
    # replace the link, so leave it for the writer to refuse.
    if not new_path.exists() and not new_path.is_symlink() and legacy_path.exists():
        try:
            legacy_path.rename(new_path)
        except OSError:
            logger.debug("Could not rename %s to %s", legacy_path, new_path, exc_info=True)
            return False
        return True
    return False


def get_lockfile_installed_paths(project_root: Path) -> list[str]:
    """Deprecated: use LockFile.installed_paths_for_project() instead."""
    return LockFile.installed_paths_for_project(project_root)
