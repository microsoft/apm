"""Installed local-bundle executables, for ``apm approve`` (#4221, #4437).

A local bundle is not a dependency under ``apm_modules/``, so the approval
commands cannot scan it. ``apm install <bundle>`` records, in the scope's
``apm_modules/``, the slug, the exact-content approval key the install printed,
the executable types (hooks, MCP, LSP) the bundle ships, and the dependency
policy identities from its metadata. ``--pending`` re-checks every entry
against current trust, so an approved key never shows as pending;
``apm approve <key>`` grants only a key the last install recorded.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

_RECORD = Path("apm_modules") / ".local-bundle-pending.json"

if TYPE_CHECKING:
    from ..policy.models import LocalBundleSubjects


@dataclass(frozen=True)
class BundlePolicySubject:
    """The policy identity captured from a bundle's authoritative metadata."""

    canonical_ref: str
    host: str | None
    case_insensitive_identity_prefix_segments: int
    unique_key: str

    def get_canonical_dependency_string(self) -> str:
        """Return the canonical identity used by dependency-policy matching."""
        return self.canonical_ref

    @property
    def policy_host(self) -> str | None:
        """Return the effective host used by host-qualified policy patterns."""
        return self.host

    def get_unique_key(self) -> str:
        """Return the identity captured with this policy subject."""
        return self.unique_key


@dataclass(frozen=True)
class StoredBundlePolicySubjects:
    """Bundle-name and embedded-package identities captured at install time."""

    names: tuple[BundlePolicySubject, ...]
    packages: tuple[BundlePolicySubject, ...]


@dataclass(frozen=True)
class PendingBundle:
    """One installed local bundle whose executables are parked."""

    slug: str
    approval_key: str
    exec_types: tuple[str, ...]
    policy_subjects: StoredBundlePolicySubjects | None = None


def _record_path(scope_dir: Path) -> Path:
    return Path(scope_dir) / _RECORD


def pending_record_blocker(scope_dir: Path) -> str | None:
    """Return why the scope's pending record cannot be safely written, else ``None``."""
    from ..utils.path_security import has_symlink_component

    path = _record_path(scope_dir)
    if has_symlink_component(Path(scope_dir), path):
        return f"{_RECORD.as_posix()} (linked path)"
    return None


def _read(scope_dir: Path) -> dict[str, dict]:
    path = _record_path(scope_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    bundles = data.get("bundles") if isinstance(data, dict) else None
    return bundles if isinstance(bundles, dict) else {}


def record_bundle_executables(
    scope_dir: Path,
    slug: str,
    approval_key: str | None,
    exec_types: Iterable[str],
    *,
    policy_subjects: LocalBundleSubjects | None = None,
) -> None:
    """Record (or, with no *exec_types*, clear) one installed bundle's key and types."""
    bundles = _read(scope_dir)
    types = sorted(set(exec_types))
    if types and approval_key:
        entry: dict[str, Any] = {"approval_key": approval_key, "exec_types": types}
        if policy_subjects is not None:
            entry["policy_subjects"] = _serialize_policy_subjects(policy_subjects)
        bundles[slug] = entry
    elif slug in bundles:
        del bundles[slug]
    else:
        return
    _write(scope_dir, bundles)


def _serialize_policy_subjects(subjects: LocalBundleSubjects) -> dict[str, list[dict[str, Any]]]:
    """Persist the exact identity semantics emitted by bundle_policy_subjects."""

    def serialize(reference: Any) -> dict[str, Any]:
        return {
            "canonical_ref": reference.get_canonical_dependency_string(),
            "host": reference.policy_host,
            "case_insensitive_identity_prefix_segments": (
                reference.case_insensitive_identity_prefix_segments
            ),
            "unique_key": reference.get_unique_key(),
        }

    return {
        "names": [serialize(reference) for reference in subjects.names],
        "packages": [serialize(reference) for reference in subjects.packages],
    }


def _deserialize_policy_subjects(value: Any) -> StoredBundlePolicySubjects | None:
    if not isinstance(value, dict):
        return None

    def deserialize(items: Any) -> tuple[BundlePolicySubject, ...] | None:
        if not isinstance(items, list):
            return None
        references = []
        for item in items:
            if not isinstance(item, dict):
                return None
            canonical_ref = item.get("canonical_ref")
            host = item.get("host")
            prefix_segments = item.get("case_insensitive_identity_prefix_segments")
            unique_key = item.get("unique_key")
            if (
                not isinstance(canonical_ref, str)
                or not canonical_ref
                or (host is not None and not isinstance(host, str))
                or not isinstance(prefix_segments, int)
                or isinstance(prefix_segments, bool)
                or prefix_segments < 0
                or not isinstance(unique_key, str)
                or not unique_key
            ):
                return None
            references.append(BundlePolicySubject(canonical_ref, host, prefix_segments, unique_key))
        return tuple(references)

    names = deserialize(value.get("names"))
    packages = deserialize(value.get("packages"))
    if not names or packages is None:
        return None
    return StoredBundlePolicySubjects(names, packages)


def _write(scope_dir: Path, bundles: dict[str, dict]) -> None:
    path = _record_path(scope_dir)
    if blocker := pending_record_blocker(scope_dir):
        from ..utils.path_security import PathTraversalError

        raise PathTraversalError(f"Refusing to write the local bundle record through {blocker}")
    from ..integration import write_seam

    if not bundles:
        write_seam.unlink(path, missing_ok=True)  # journaled like its write (#5130)
        return
    write_seam.mkdir(path.parent, parents=True, exist_ok=True)
    # Atomic: a failed update leaves the previous record (and its notes) intact.
    write_seam.atomic_write_text(
        path, json.dumps({"bundles": bundles}, indent=2, sort_keys=True) + "\n"
    )


@dataclass
class BundleRecordStage:
    """One install's bundle executables entry, persisted only on commit (#4492).

    A failed install never calls :meth:`commit`, so it leaves the record as it was.
    """

    scope_dir: Path
    slug: str
    approval_key: str | None
    exec_types: tuple[str, ...]
    policy_subjects: LocalBundleSubjects | None = None
    committed: bool = False

    def commit(self) -> None:
        """Persist the staged entry once; later calls are no-ops."""
        if self.committed:
            return
        self.committed = True
        record_bundle_executables(
            self.scope_dir,
            self.slug,
            self.approval_key,
            self.exec_types,
            policy_subjects=self.policy_subjects,
        )


def _recorded(scope_dir: Path) -> list[PendingBundle]:
    found: list[PendingBundle] = []
    for slug, entry in sorted(_read(scope_dir).items()):
        if not isinstance(entry, dict):
            continue
        key = entry.get("approval_key")
        types = entry.get("exec_types")
        if not isinstance(key, str) or not isinstance(types, list):
            continue
        found.append(
            PendingBundle(
                str(slug),
                key,
                tuple(str(t) for t in types),
                _deserialize_policy_subjects(entry.get("policy_subjects")),
            )
        )
    return found


def recorded_bundle_slugs(scope_dir: Path) -> list[str]:
    """Return the slugs of the installed bundles that ship executables."""
    return [bundle.slug for bundle in _recorded(scope_dir)]


def _read_strict(scope_dir: Path) -> dict[str, dict]:
    """The record's bundles for an uninstall update; raise when it exists but cannot be read."""
    path = _record_path(scope_dir)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{_RECORD.as_posix()} cannot be read: {exc}") from exc
    bundles = data.get("bundles") if isinstance(data, dict) else None
    if not isinstance(bundles, dict):
        raise ValueError(f"{_RECORD.as_posix()} cannot be read: no 'bundles' mapping")
    return bundles


def _retiring(entry: Any) -> list[str]:
    types = entry.get("retiring") if isinstance(entry, dict) else None
    return [str(t) for t in types] if isinstance(types, list) else []


def mark_bundle_retiring(scope_dir: Path, slug: str, exec_type: str) -> None:
    """Note, before uninstall removes anything, that it is retiring *exec_type* (#4987).

    A retry after a failed :func:`drop_bundle_exec_type` finds the note and
    finishes; a bundle whose *exec_type* is only parked carries none. Raises
    ``ValueError`` when the record exists but cannot be read.
    """
    bundles = _read_strict(scope_dir)
    entry = bundles.get(slug)
    types = entry.get("exec_types") if isinstance(entry, dict) else None
    if not isinstance(types, list) or exec_type not in types:
        return
    if exec_type in _retiring(entry):
        return
    bundles[slug] = {**entry, "retiring": sorted({*_retiring(entry), exec_type})}
    _write(scope_dir, bundles)


def retiring_bundle_slugs(scope_dir: Path, exec_type: str) -> set[str]:
    """Slugs whose uninstall began retiring *exec_type* and did not finish its record update."""
    return set(retiring_bundle_keys(scope_dir, exec_type))


def retiring_bundle_keys(scope_dir: Path, exec_type: str) -> dict[str, str | None]:
    """Each retiring slug with the approval key its install recorded (#5003)."""
    return {
        slug: entry.get("approval_key") if isinstance(entry.get("approval_key"), str) else None
        for slug, entry in _read(scope_dir).items()
        if exec_type in _retiring(entry)
    }


def drop_bundle_exec_type(scope_dir: Path, slug: str, exec_type: str) -> None:
    """Forget *exec_type* for one installed bundle once its uninstall removed it (#3700).

    Raises ``ValueError`` when the record exists but cannot be read.
    """
    bundles = _read_strict(scope_dir)
    entry = bundles.get(slug)
    types = entry.get("exec_types") if isinstance(entry, dict) else None
    if not isinstance(types, list) or exec_type not in types:
        return
    remaining = [t for t in types if t != exec_type]
    retiring = [t for t in _retiring(entry) if t != exec_type]
    if not remaining:
        del bundles[slug]
    else:
        bundles[slug] = {k: v for k, v in entry.items() if k != "retiring"}
        bundles[slug]["exec_types"] = remaining
        if retiring:
            bundles[slug]["retiring"] = retiring
    _write(scope_dir, bundles)


def pending_bundles(
    scope_dir: Path, is_approved: Callable[[str, str], bool]
) -> list[PendingBundle]:
    """Return recorded bundles with a parked type *is_approved(key, type)* still refuses."""
    found: list[PendingBundle] = []
    for bundle in _recorded(scope_dir):
        still = tuple(t for t in bundle.exec_types if not is_approved(bundle.approval_key, t))
        if still:
            found.append(
                PendingBundle(bundle.slug, bundle.approval_key, still, bundle.policy_subjects)
            )
    return found


def recorded_bundle_keys(scope_dir: Path, owner: str) -> tuple[str, ...]:
    """Return the approval keys the last installs recorded for bundle *owner* (RA-025).

    *owner* is the bundle's package id (its MCP provenance token); a key
    names it before ``#`` (``<package>#<version>@sha256:<digest>``).
    """
    return tuple(
        bundle.approval_key
        for bundle in _recorded(scope_dir)
        if bundle.slug == owner or bundle.approval_key.split("#", 1)[0] == owner
    )


def bundle_grant_withdrawn(scope_dir: Path, owner: str, context: Any, exec_type: str) -> bool:
    """Whether a deny or revoke withdrew *exec_type* from bundle *owner* (RA-025, RA-041).

    The one baseline rule for a local bundle's services: a bundle is not in
    apm.yml, so its MCP and LSP servers stay on the lock's baseline until the
    exact bundle key its install recorded no longer grants the type. A bundle
    without a record keeps its baseline.
    """
    from apm_cli.security.trust import package_grants_type

    keys = recorded_bundle_keys(scope_dir, owner)
    return bool(keys) and not package_grants_type(context, keys, exec_type)


def recorded_bundle(scope_dir: Path, approval_key: str) -> PendingBundle | None:
    """Return the installed bundle whose last install printed exactly *approval_key* (#4437).

    The key binds the bundle's content digest, so a key with a wrong digest,
    or one a later install of changed bundle bytes replaced, matches nothing.
    """
    for bundle in _recorded(scope_dir):
        if bundle.approval_key == approval_key:
            return bundle
    return None
