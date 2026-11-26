"""Pure drift-detection helpers for diff-aware ``apm install``.

These functions are stateless and side-effect-free, making them easy to test
in isolation and to reuse from multiple call sites in ``install.py`` without
duplicating logic.

Four kinds of drift are detected:

* **Ref drift** — the ``ref`` pinned in ``apm.yml`` differs from what the
  lockfile recorded as ``resolved_ref``.  This includes transitions such as
  ``None → "v1.0.0"`` (user adds a pin), ``"main" → None`` (user removes a
  pin), ``"v1.0.0" → "v2.0.0"`` (user bumps the pin), and hash-based pins
  (``None → "abc1234"`` or ``"abc1234" → "def5678"``).

* **Orphan drift** — packages present in the lockfile but absent from the
  current manifest.  Their deployed files should be removed.

* **Config drift** — an already-installed dependency's serialised configuration
  differs from the baseline stored in the lockfile.  (Currently only MCP
  servers; extendable to other integrator types.)

* **Stale-file drift** -- files previously deployed for a still-present
  package that are no longer produced by the current install (e.g. a
  rename or removal inside the package).  The now-unused paths should be
  removed.  See :func:`detect_stale_files`.

Scope / non-goals
-----------------
* **Hash-based refs** — handled identically to branch/tag refs: both
  ``dep_ref.reference`` and ``locked_dep.resolved_ref`` store the raw ref
  string from ``apm.yml``/lockfile respectively, so a change from
  ``"abc1234"`` to ``"def5678"`` is detected just like ``"v1.0" → "v2.0"``.

* **URL format changes** — transparent.  ``DependencyReference.parse()``
  normalises all input formats (HTTPS, SSH, shorthand, FQDN) into the same
  canonical ``repo_url`` before the lockfile stores them.  Changing
  ``owner/repo`` to ``https://github.com/owner/repo.git`` in ``apm.yml`` is a
  formatting-only change that produces the same unique key and is correctly
  treated as no drift.

* **Host-provider changes** -- detected through the canonical host-provider
  registry. Equivalent explicit/inferred provider forms remain unchanged,
  while a backend or credential-route change forces a re-download.
* **HTTP transport flips** — detected.  Switching between HTTPS and insecure
  HTTP toggles ``is_insecure`` and forces a re-download even when the package
  identity and ref are otherwise unchanged.
"""

from __future__ import annotations

import builtins
from dataclasses import dataclass
from dataclasses import replace as _dataclass_replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from apm_cli.core.host_providers import effective_host_provider_identity
from apm_cli.core.remediation import FROZEN_MARKETPLACE_UNLOCKED, MARKETPLACE_CATALOG_REPINNED
from apm_cli.install.phases._skip_logic import _should_use_locked_ref
from apm_cli.utils.github_host import default_host

if TYPE_CHECKING:
    from apm_cli.deps.lockfile import LockedDependency, LockFile
    from apm_cli.models.apm_package import DependencyReference


# ---------------------------------------------------------------------------
# Ref drift
# ---------------------------------------------------------------------------


def _registry_range_covers_locked_version(
    manifest_range: str | None,
    locked_version: str | None,
) -> bool:
    """True when the manifest range would still select the lockfile's exact registry version.

    Uses the resolver's own ``pick_best`` so replay applies the same
    pre-release eligibility rule: a stable range never selects a pre-release.
    """
    if not manifest_range or not locked_version:
        return False
    from apm_cli.deps.registry.semver import pick_best

    return pick_best(manifest_range, [locked_version]) == locked_version


def registry_selector_mismatch(selector: str | None, locked_version: str | None) -> bool:
    """True when a registry version selector no longer selects the locked version.

    The manifest carries a selector (for example ``^1.2.0``) while the lock
    records the exact version it resolved to (for example ``1.5.3``). A semver
    range drifts only when it stops covering the locked version; any other
    selector (exact tag or label) is exact-matched by the resolver, so it
    drifts on a literal mismatch.
    """
    from apm_cli.deps.registry.semver import is_semver_range

    if not is_semver_range(selector or ""):
        return selector != locked_version
    return not _registry_range_covers_locked_version(selector, locked_version)


def has_complete_registry_pin(locked_dep: Any) -> bool:
    """True when a registry lock row records its exact version, URL and hash.

    These are the registry pin a replay needs (req-lk-003, req-lk-019):
    the exact version selects the package, ``resolved_hash`` is the trust
    anchor, and ``resolved_url`` is advisory. No commit is involved.
    """
    return bool(
        getattr(locked_dep, "version", None)
        and getattr(locked_dep, "resolved_url", None)
        and getattr(locked_dep, "resolved_hash", None)
    )


def _normalize_dep_source(dep: Any) -> str:
    """Return the resolver source label for *dep*, defaulting to ``git``."""
    raw = getattr(dep, "source", None)
    if not isinstance(raw, str) or not raw:
        return "git"
    return raw


def _optional_string_attr(dep: Any, name: str) -> str | None:
    """Read an optional string field without accepting dynamic mock attributes."""
    value = getattr(dep, name, None)
    return value if isinstance(value, str) and value else None


def revision_pin_mismatch(reference: str | None, locked_dep: LockedDependency) -> str | None:
    """Return why a full-SHA manifest pin disagrees with the locked commit.

    A manifest ref that is a full commit SHA is immutable: the locked
    ``resolved_commit`` must name that same commit, whatever ``resolved_ref``
    the row records. Returns ``None`` when *reference* is not a full SHA or
    the commits agree.
    """
    from apm_cli.deps.revision_pins import is_full_revision_pin

    if not is_full_revision_pin(reference):
        return None
    locked_commit = locked_dep.resolved_commit
    if (
        isinstance(locked_commit, str)
        and locked_commit.strip().lower() == reference.strip().lower()
    ):
        return None
    return (
        f"manifest commit '{reference}' != "
        f"lockfile resolved_commit '{locked_commit or '(missing)'}'"
    )


def _registry_marketplace_mismatch(spec: str | None, locked_dep: Any) -> str | None:
    """Compare a registry-routed catalog selection to its locked registry evidence.

    A catalog entry routed to a registry locks registry evidence, not a commit
    (#4009): the exact version, resolved URL and resolved hash.
    """
    if not has_complete_registry_pin(locked_dep):
        return "missing locked registry version, URL or hash evidence"
    if spec and registry_selector_mismatch(spec, locked_dep.version):
        return f"manifest version '{spec}' != lockfile registry version '{locked_dep.version}'"
    return None


def _non_git_marketplace_mismatch(
    spec: str | None, locked_dep: LockedDependency
) -> tuple[bool, str | None]:
    """Route registry-locked (#4009) and local-catalog (#4639) entries to their evidence checks.

    Returns ``(True, mismatch)`` when the entry is not Git-locked, else ``(False, None)``.
    """
    if getattr(locked_dep, "source", None) == "registry":
        return True, _registry_marketplace_mismatch(spec, locked_dep)
    if locked_dep.is_local_source:
        return True, _local_catalog_mismatch(spec, locked_dep)
    return False, None


def marketplace_ref_mismatch(
    dep_ref: DependencyReference,
    locked_dep: LockedDependency,
) -> str | None:
    """Compare a marketplace selection to locked evidence without catalog I/O.

    A plugin from a local catalog is locked by its content (``local_path`` and
    ``content_hash``), never by a commit, and a local catalog selects no
    version or ref, so a manifest selector cannot be satisfied (#4639).
    """
    from apm_cli.deps.revision_pins import is_full_revision_pin
    from apm_cli.marketplace.semver import parse_semver, satisfies_range
    from apm_cli.marketplace.version_resolver import is_version_constraint

    spec = dep_ref.marketplace_version_spec
    handled, mismatch = _non_git_marketplace_mismatch(spec, locked_dep)
    if handled:
        return mismatch
    if not is_full_revision_pin(locked_dep.resolved_commit):
        return "missing locked commit evidence"
    if not spec:
        return None
    if is_full_revision_pin(spec):
        return revision_pin_mismatch(spec, locked_dep)
    elif is_version_constraint(spec):
        # Git semver locks already distinguish selected versions from package
        # inventory using constraint + resolved_tag. Never infer tag patterns
        # from a suffix or accept the requested selector as resolution proof.
        if (
            not locked_dep.constraint
            or not locked_dep.resolved_tag
            or locked_dep.resolved_ref != locked_dep.resolved_tag
            or not locked_dep.version
        ):
            return f"missing locked version evidence for manifest version '{spec}'"
        version = parse_semver(locked_dep.version)
        if version is None or not satisfies_range(version, spec):
            return f"manifest version '{spec}' != lockfile selected version '{locked_dep.version}'"
    elif spec != locked_dep.resolved_ref:
        return f"manifest ref '{spec}' != lockfile ref '{locked_dep.resolved_ref or '(missing)'}'"
    return None


def _local_catalog_mismatch(spec: str | None, locked_dep: LockedDependency) -> str | None:
    if not locked_dep.local_path or not locked_dep.content_hash:
        return "missing locked content evidence"
    if spec:
        return f"manifest ref '{spec}' cannot select a local catalog plugin"
    return None


def locked_marketplace_row(
    dep_ref: DependencyReference,
    lockfile: LockFile | None,
    marketplace_index: dict | None = None,
    *,
    check_origin: bool = True,
) -> LockedDependency | None:
    """Return the lock row an unchanged marketplace declaration still matches.

    The row must be the lock's single association for the declaration's
    ``{name, marketplace}``, carry no replacement binding, satisfy
    :func:`marketplace_ref_mismatch`, and, while the root ``marketplaces:``
    map declares the name, record the origin the map declares (#2903;
    *check_origin* ``False`` skips only that comparison). Never performs
    catalog I/O. Callers looking up many declarations pass one
    ``lockfile.marketplace_index()``.
    """
    if lockfile is None or getattr(dep_ref, "is_marketplace", False) is not True:
        return None
    locked_dep = lockfile.get_dependency(
        dep_ref.get_unique_key(), marketplace_index=marketplace_index
    )
    if (
        locked_dep is None
        or locked_dep.replacement is not None
        or marketplace_ref_mismatch(dep_ref, locked_dep) is not None
        or (check_origin and _declared_origin_conflict(dep_ref, locked_dep) is not None)
    ):
        return None
    return locked_dep


def _declared_origin_conflict(dep_ref: DependencyReference, locked_dep: LockedDependency):
    from apm_cli.marketplace.project_marketplaces import declared_origin_conflict

    return declared_origin_conflict(dep_ref.marketplace_name, locked_dep.source_url)


def _owns_locked_servers(
    lockfile: LockFile, locked_dep: LockedDependency, package_dir: Path
) -> bool:
    """True when the lock records MCP or LSP servers contributed by *locked_dep*.

    Each kind's owner token comes from its provenance writer: MCP names the
    declaring package, LSP uses the lock key or, on a first install, the
    package's ``apm_modules``-relative *package_dir*.
    """
    from apm_cli.install.lsp.integration import package_lsp_owner
    from apm_cli.integration.mcp_config_view import mcp_package_owner

    mcp_owners = {mcp_package_owner(locked_dep.name, package_dir=package_dir)}
    lsp_owners = {
        package_lsp_owner(owner) for owner in (locked_dep.get_unique_key(), package_dir.as_posix())
    }
    for owners, provenance in (
        (mcp_owners, lockfile.mcp_config_provenance),
        (lsp_owners, lockfile.lsp_config_provenance),
    ):
        for recorded in (provenance or {}).values():
            tokens = recorded if isinstance(recorded, list) else [recorded]
            if owners.intersection(tokens):
                return True
    return False


MARKETPLACE_REPLAYED = "replayed"
MARKETPLACE_UNLOCKED = "unlocked"
MARKETPLACE_UPDATE = "update"
MARKETPLACE_CATALOG_METADATA = "catalog-metadata"
MARKETPLACE_ORIGIN_CHANGED = "origin-changed"
MARKETPLACE_LOCAL_CATALOG = "local-catalog"


@dataclass(frozen=True)
class MarketplaceReplay:
    """Outcome of the replay-vs-re-resolve decision for one declaration."""

    reason: str
    reference: DependencyReference | None = None
    origin_conflict: Exception | None = None


def locked_marketplace_replay(
    dep_ref: DependencyReference,
    lockfile: LockFile | None,
    *,
    update_refs: bool = False,
    modules_dir: Path | None = None,
) -> MarketplaceReplay:
    """Decide whether a marketplace declaration replays its lock row (#3911).

    Single owner of "replay vs re-resolve" for object-form marketplace
    declarations. A row found by :func:`locked_marketplace_row` replays
    without catalog, alias or registration I/O (``replayed``); the row's
    commit and hashes then pin the download. New, changed, ambiguous,
    replacement-bound or under-evidenced entries are ``unlocked`` and
    ``apm update`` / ``--refresh`` (*update_refs*) is ``update``; both
    resolve through the marketplace. A row recorded from an origin other
    than the one the root ``marketplaces:`` map now declares is
    ``origin-changed``: it re-resolves through the map, since the file wins
    (#2903), and ``--frozen`` refuses it.

    ``catalog-metadata`` covers one case: catalog entries may define MCP or
    LSP servers inline, and the lock cannot rebuild that metadata, so a row
    whose package owns locked servers replays only while its materialized
    copy under *modules_dir* exists. A cold copy reads the catalog for that
    metadata only; :func:`catalog_metadata_refusal` keeps the lock's pin.

    ``local-catalog`` covers a row locked from a local catalog (#4639): the
    catalog on disk stays the plugin's live source, so every install reads
    it again, and ``--frozen`` accepts the row only while the catalog still
    selects it (:func:`local_catalog_refusal`).
    """
    if update_refs:
        return MarketplaceReplay(MARKETPLACE_UPDATE)
    locked_dep = locked_marketplace_row(dep_ref, lockfile)
    if locked_dep is None:
        return _unlocked_marketplace_replay(dep_ref, lockfile)
    if locked_dep.is_local_source:
        return MarketplaceReplay(MARKETPLACE_LOCAL_CATALOG)
    replayed = locked_dep.to_edge_reference(dep_ref)
    if modules_dir is not None and replayed.get_install_path(modules_dir).is_dir():
        return MarketplaceReplay(MARKETPLACE_REPLAYED, replayed)
    if _owns_locked_servers(lockfile, locked_dep, replayed.get_install_path(Path())):
        return MarketplaceReplay(MARKETPLACE_CATALOG_METADATA)
    return MarketplaceReplay(MARKETPLACE_REPLAYED, replayed)


def _unlocked_marketplace_replay(
    dep_ref: DependencyReference, lockfile: LockFile | None
) -> MarketplaceReplay:
    """Classify a declaration with no replayable row: ``origin-changed`` or ``unlocked``."""
    locked_dep = locked_marketplace_row(dep_ref, lockfile, check_origin=False)
    declared_origin = None if locked_dep is None else _declared_origin_conflict(dep_ref, locked_dep)
    if declared_origin is None:
        return MarketplaceReplay(MARKETPLACE_UNLOCKED)
    from apm_cli.marketplace.errors import ProjectMarketplaceOriginConflictError

    return MarketplaceReplay(
        MARKETPLACE_ORIGIN_CHANGED,
        origin_conflict=ProjectMarketplaceOriginConflictError(
            dep_ref.marketplace_name,
            dep_ref.marketplace_plugin_name,
            locked_dep.source_url,
            declared_origin,
        ),
    )


def frozen_marketplace_refusal(decision: MarketplaceReplay) -> Exception | None:
    """Return the ``--frozen`` refusal for a declaration the lock cannot replay.

    An unlocked declaration is refused, and an ``origin-changed`` one with
    its origin conflict; ``catalog-metadata`` rows keep reading the catalog
    because only it holds their server definitions.
    """
    if decision.origin_conflict is not None:
        return decision.origin_conflict
    if decision.reason != MARKETPLACE_UNLOCKED:
        return None
    return ValueError(
        "--frozen replays marketplace dependencies from apm.lock.yaml only, and "
        f"no locked entry matches this declaration. {FROZEN_MARKETPLACE_UNLOCKED.sentence()}"
    )


def catalog_metadata_refusal(
    dep_ref: DependencyReference,
    resolved: DependencyReference,
    lockfile: LockFile | None,
) -> ValueError | None:
    """Refuse a ``catalog-metadata`` re-read whose catalog no longer pins the lock row.

    The catalog is read only for the server definitions the lock cannot
    rebuild; the locked commit still pins the download. A catalog that now
    selects another source or ref would silently re-pin the package, so only
    ``apm update`` may move it.
    """
    locked_dep = locked_marketplace_row(dep_ref, lockfile)
    if (
        locked_dep is not None
        and resolved.get_unique_key() == locked_dep.get_unique_key()
        and not detect_ref_change(resolved, locked_dep)
    ):
        return None
    locked = (
        f"{locked_dep.get_unique_key()}#{locked_dep.resolved_ref or locked_dep.resolved_commit}"
        if locked_dep is not None
        else "another selection"
    )
    current = f"{resolved.get_unique_key()}#{resolved.reference or 'HEAD'}"
    return ValueError(
        f"the catalog now selects {current}, but apm.lock.yaml pins {locked}. "
        "Catalog edits do not change what installs; "
        f"{MARKETPLACE_CATALOG_REPINNED.bind(package=dep_ref.get_display_name()).clause()}"
    )


def local_catalog_refusal(
    dep_ref: DependencyReference,
    resolved: DependencyReference,
    lockfile: LockFile | None,
) -> ValueError | None:
    """Refuse a ``--frozen`` install whose local catalog no longer selects the lock row.

    A ``local-catalog`` row replays only while the catalog on disk still
    resolves the declaration to the locked plugin directory; an edited
    catalog entry needs a plain ``apm install`` to lock its new selection.
    """
    locked_dep = locked_marketplace_row(dep_ref, lockfile)
    if (
        locked_dep is not None
        and resolved.is_local
        and resolved.get_unique_key() == locked_dep.get_unique_key()
    ):
        return None
    locked = locked_dep.local_path if locked_dep is not None else "another selection"
    current = resolved.local_path if resolved.is_local else resolved.get_unique_key()
    return ValueError(
        "declared source differs from apm.lock.yaml: the local catalog now selects "
        f"{current}, but apm.lock.yaml pins {locked}. "
        f"{FROZEN_MARKETPLACE_UNLOCKED.sentence()}"
    )


def _routes_to_locked_proxy(dep_ref: DependencyReference, locked_dep: Any) -> bool:
    """Ask the proxy routing owner whether *dep_ref* reaches *locked_dep*'s proxy."""
    from apm_cli.deps.artifactory_orchestrator import ArtifactoryRouter

    return ArtifactoryRouter.routes_to_locked_proxy(dep_ref, locked_dep)


def detect_ref_change(
    dep_ref: DependencyReference,
    locked_dep: LockedDependency | None,
    *,
    update_refs: bool = False,
    logger=None,
) -> bool:
    """Return ``True`` when the manifest ref differs from the locked resolved_ref.

    Handles all transitions:

    * ref *added*  (``None`` -> ``"v1.0.0"``)
    * ref *removed* (``"main"`` -> ``None``)
    * ref *changed* (``"v1.0.0"`` -> ``"v2.0.0"``)

    .. note::

       Host names are normally part of the dependency key. Provider changes
       for the same host are detected through canonical host classification.

    Args:
        dep_ref: The dependency as declared in the current manifest.
        locked_dep: The matching entry from the existing lockfile, or ``None``
                    when the package is brand-new (not yet in the lockfile).
        update_refs: Pass ``True`` when running in ``--update`` mode.  In that
                     mode the lockfile is intentionally ignored, so this
                     function always returns ``False`` to avoid double-action.

    Returns:
        ``True`` when a re-download is needed due to a ref change; ``False``
        when the ref is unchanged, when the package is new, or when
        ``update_refs=True``.
    """
    if update_refs:
        return False
    if locked_dep is None:
        return False  # new package — not drift, just a first install
    if getattr(dep_ref, "is_marketplace", False) is True:
        return marketplace_ref_mismatch(dep_ref, locked_dep) is not None
    if not locked_dep.matches_replacement(dep_ref):
        return True
    selected_locked = (
        locked_dep.to_dependency_ref() if locked_dep.replacement is not None else locked_dep
    )

    # Source flip drift: manifest changed resolver between installs (e.g.
    # was ``- git: ...``, now ``acme/foo@corp#^1.0.0``). The install path,
    # auth chain, and trust anchor all change — force a re-resolve.
    # Note: source=None and source="git" are equivalent (legacy default).
    manifest_source = _normalize_dep_source(dep_ref)
    locked_source = _normalize_dep_source(selected_locked)
    if manifest_source != locked_source:
        return True

    # Compare effective providers, not raw host_type: explicit and inferred
    # forms selecting the same backend and credential route remain equivalent.
    if manifest_source == "git":
        try:
            manifest_provider = effective_host_provider_identity(
                _optional_string_attr(dep_ref, "host") or default_host(),
                host_type=_optional_string_attr(dep_ref, "host_type"),
            )
            locked_provider = effective_host_provider_identity(
                _optional_string_attr(selected_locked, "host") or default_host(),
                host_type=_optional_string_attr(selected_locked, "host_type"),
            )
        except (ValueError, RuntimeError):
            # An unclassifiable lock cannot safely authorize replay.
            return True
        # A proxy-routed row records the proxy host instead of the upstream
        # one; the current route to that same proxy keeps it the same source.
        if manifest_provider != locked_provider and not _routes_to_locked_proxy(
            dep_ref, selected_locked
        ):
            return True

    # Registry-sourced deps: the manifest carries a semver range
    # (e.g. ``^1.2.0``) while the lockfile records an exact version
    # (e.g. ``1.5.3``). Plain string comparison would be a false
    # positive — instead, ask whether the locked version still
    # satisfies the manifest range. If yes, no drift; if no, drift
    # (the user expanded/contracted the range away from the locked
    # version and we need to re-resolve).
    if manifest_source == "registry":
        return registry_selector_mismatch(dep_ref.reference, locked_dep.version)

    if (getattr(dep_ref, "is_insecure", False) is True) != (
        getattr(selected_locked, "is_insecure", False) is True
    ):
        return True

    # Git-source semver-range deps (issue #1488): the manifest carries
    # a semver range (``^1.2.0``) while the lockfile records the
    # resolved tag (``v1.5.3``) plus the original constraint. Direct
    # comparison of the range against the tag is a false positive --
    # instead, treat the dep as unchanged when the lockfile already
    # stores the same constraint (and we therefore trust its resolution
    # until the user runs ``--update``).
    # A row locked without a constraint recorded the selector literally (an
    # exact pin under PROXY_REGISTRY_ONLY), so the literal comparison applies.
    if getattr(dep_ref, "ref_kind", None) == "semver" and locked_dep.constraint is not None:
        return dep_ref.reference != locked_dep.constraint

    # Git/local deps: direct ref comparison. Handles None→value, value→None,
    # and value→value. No truthiness guard on locked_dep.resolved_ref —
    # None != "v1.0.0" is True. A full-SHA pin is satisfied only by that exact
    # locked commit, whatever resolved_ref the lock row records.
    return (
        dep_ref.reference != locked_dep.resolved_ref
        or revision_pin_mismatch(dep_ref.reference, locked_dep) is not None
    )


def should_force_ref_recheck(
    dep_ref: DependencyReference,
    locked_dep: LockedDependency | None,
    *,
    update_refs: bool,
    package_path: Path | None = None,
    update_keys: set[str] | None = None,
) -> bool:
    """Return whether an existing install path must reach the download owner.

    This is the single gate shared by the dependency resolver and its download
    callback. The resolver reads a package's ``apm.yml`` from whatever path this
    gate leaves in place, so returning False means "the materialized copy is
    current". A normal install rechecks local packages (their source can change
    without any ref moving), manifest/lock drift, and a copy whose commit or
    content contradicts its lock row (:func:`locked_copy_is_stale`);
    *update_keys* scopes both
    the local recheck and the update rechecks to a targeted run's selected
    subtree. An explicit update also
    rechecks mutable Git refs, semver ranges and hook-only packages. Hook
    replacements must pass staged admission before any live content is replaced.

    This gate decides ref/content freshness only. Slot ownership is a separate
    staging fact: when several local sources share one ``_local/<name>`` slot,
    the download callback reuses the copy this run already staged for it
    (``ResolutionStagingSession.owns_path``) instead of staging it twice.
    """
    if locked_dep is not None and not locked_dep.matches_replacement(dep_ref):
        return True
    if dep_ref.is_local:
        # #3401 / #3501: the apm_modules copy is from the previous install, so
        # reading it would resolve transitive deps from a stale manifest. A
        # targeted run re-reads only its selected subtree; an unselected local
        # package keeps its locked state, even mid-edit (#3649).
        return update_keys is None or dep_ref.get_unique_key() in update_keys
    if getattr(dep_ref, "artifactory_prefix", None):
        return False
    if locked_dep is None:
        return True
    if update_refs:
        from apm_cli.deps.revision_pins import is_full_revision_pin

        if update_keys is not None and dep_ref.get_unique_key() not in update_keys:
            return False
        if dep_ref.replacement is not None:
            return not is_full_revision_pin(dep_ref.reference)
        if getattr(dep_ref, "ref_kind", None) == "semver":
            return True
        if package_path is not None:
            from apm_cli.models.validation import PackageType, detect_package_type

            package_type, _ = detect_package_type(package_path)
            if package_type is PackageType.HOOK_PACKAGE:
                return True
        if _refetches_mutable_ref_during_resolution(dep_ref):
            # #3392: a branch (or tag, or default-branch) ref of a plain Git
            # package may point at a new commit whose apm.yml re-pins children.
            # Fetch it here, during resolution, so transitive deps come from that
            # commit. The download phase skips packages the callback already
            # fetched, so this moves the update fetch earlier rather than adding
            # one.
            return not is_full_revision_pin(dep_ref.reference)
        return False
    if detect_ref_change(dep_ref, locked_dep, update_refs=False):
        return True
    # #3884: an unchanged manifest ref does not prove the copy on disk holds
    # the locked commit; a stale copy would resolve children from an old apm.yml.
    return package_path is not None and locked_copy_is_stale(locked_dep, package_path)


LOCKED_COPY_GIT_HEAD = "git-head"
LOCKED_COPY_PIN = "pin"
LOCKED_COPY_CONTENT_HASH = "content-hash"


def _locked_commit(locked_dep: Any) -> str | None:
    commit = getattr(locked_dep, "resolved_commit", None)
    if isinstance(commit, str) and commit and commit != "cached":
        return commit
    return None


def locked_copy_proof(
    locked_dep: Any, package_path: Path, *, trust_marker: bool = False
) -> str | None:
    """Return how the copy at *package_path* proves it holds *locked_dep*, else None.

    This is the single owner of "is this apm_modules copy the locked
    content". Strongest evidence first: a readable ``.git`` HEAD decides on
    its own; otherwise the ``.apm-pin`` marker naming the locked
    ``resolved_commit``, then the recorded ``content_hash`` (rows without a
    commit, or copies without a marker). The marker only catches honest
    staleness, so by default only the git HEAD or the verified content hash
    prove a copy whose bytes may be deployed; *trust_marker* admits the
    marker for resolution freshness (:func:`locked_copy_is_stale`).
    """
    if locked_dep is None or not package_path.is_dir():
        return None
    commit = _locked_commit(locked_dep)
    if commit is not None:
        if (package_path / ".git").exists():
            try:
                from git import Repo as GitRepo

                head = GitRepo(package_path).head.commit.hexsha
            except Exception:  # unreadable checkout: fall back to marker and hash
                head = None
            if head is not None:
                return LOCKED_COPY_GIT_HEAD if head == commit else None
        if trust_marker:
            from apm_cli.install.cache_pin import CachePinError, verify_marker

            try:
                verify_marker(package_path, commit)
                return LOCKED_COPY_PIN
            except CachePinError:
                pass
    from apm_cli.install.phases._redownload import _should_skip_redownload

    if _should_skip_redownload(locked_dep, package_path):
        return LOCKED_COPY_CONTENT_HASH
    return None


def locked_copy_holds_commit(locked_dep: Any, package_path: Path) -> bool:
    """Whether a cached copy proves the locked commit for replay (marker admitted)."""
    return locked_copy_proof(locked_dep, package_path, trust_marker=True) is not None


def locked_copy_is_stale(locked_dep: Any, package_path: Path) -> bool:
    """Return whether an existing copy contradicts what *locked_dep* records.

    Only evidence can contradict a copy: a recorded content hash, an
    ``.apm-pin`` marker or a ``.git`` checkout that does not prove the locked
    content. A legacy copy with none of these (and a row without a content
    hash) is kept, as before #3884; installs mark it from then on.
    """
    if locked_dep is None or not package_path.is_dir():
        return False
    if locked_copy_proof(locked_dep, package_path, trust_marker=True) is not None:
        return False
    from apm_cli.install.cache_pin import MARKER_FILENAME

    return bool(
        getattr(locked_dep, "content_hash", None)
        or (_locked_commit(locked_dep) is not None and (package_path / MARKER_FILENAME).exists())
        or (_locked_commit(locked_dep) is not None and (package_path / ".git").exists())
    )


def _refetches_mutable_ref_during_resolution(dep_ref: DependencyReference) -> bool:
    """Return whether an update fetches *dep_ref*'s mutable ref while resolving.

    Only whole-repository Git packages qualify. Registry, marketplace and
    virtual (subdirectory or file) packages are re-resolved later in the update
    pipeline; fetching them here would pin the stale selection. Callers have
    already excluded local and Artifactory-proxied dependencies.
    """
    return not (
        dep_ref.source == "registry"
        or getattr(dep_ref, "is_marketplace", False)
        or getattr(dep_ref, "marketplace_name", None)
        or getattr(dep_ref, "is_virtual", False)
    )


# ---------------------------------------------------------------------------
# Orphan drift
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# File-level stale detection (intra-package)
# ---------------------------------------------------------------------------


def detect_stale_files(
    old_deployed: builtins.list,
    new_deployed: builtins.list,
) -> builtins.set:
    """Return the set of paths that were deployed previously but are no longer produced.

    Complements the orphan owner (``install/orphans.py``), which operates at the *package* level
    (a whole package left the manifest).  This helper operates at the *file*
    level *inside* a still-present package: if a package renamed or removed a
    file between installs, the now-unused path is flagged as stale.

    Pure set-difference semantics: ``set(old_deployed) - set(new_deployed)``.
    The function does not touch the filesystem; the caller is responsible for
    actually removing the files.

    Args:
        old_deployed: Paths recorded in the previous lockfile's
                      ``deployed_files`` for this package.
        new_deployed: Paths produced by the current install for this package.

    Returns:
        Workspace-relative path strings that should no longer exist on disk.
    """
    return builtins.set(old_deployed) - builtins.set(new_deployed)


# ---------------------------------------------------------------------------
# Config drift (integrator-agnostic)
# ---------------------------------------------------------------------------


def detect_config_drift(
    current_configs: dict[str, dict],
    stored_configs: dict[str, dict],
    logger=None,
) -> builtins.set:
    """Return names of entries whose current config differs from the stored baseline.

    Only entries that *have* a stored baseline and whose config has *changed*
    are returned.  Brand-new entries (not in ``stored_configs``) are excluded
    because they have never been installed — they are installs, not updates.

    Args:
        current_configs: Mapping of name → current serialised config (from the
                         manifest / dependency objects).
        stored_configs: Mapping of name → previously stored config (from the
                        lockfile).

    Returns:
        A set of names (strings) whose configuration has drifted.
    """
    from apm_cli.integration.mcp_config_view import McpConfigDiff

    return builtins.set(McpConfigDiff.between(current_configs, stored_configs).changed)


# ---------------------------------------------------------------------------
# Download ref construction
# ---------------------------------------------------------------------------


def _registry_replay_overrides_from_lock(locked_dep: Any) -> dict[str, Any] | None:
    """Fields to merge onto dep_ref so a registry install replays the locked version."""
    if locked_dep.source == "registry" and locked_dep.version:
        return {"reference": locked_dep.version, "source": "registry"}
    return None


def build_download_ref(
    dep_ref: DependencyReference,
    existing_lockfile: LockFile | None,
    *,
    update_refs: bool,
    ref_changed: bool,
    logger=None,
    require_locked: bool = False,
) -> DependencyReference:
    """Build the dependency reference passed to the package downloader.

    Returns a :class:`DependencyReference` (not a flat string) so that
    structured fields like ``virtual_path`` survive the trip to
    ``download_package()`` without a lossy ``str()`` → ``parse()``
    round-trip.  See :issue:`382`.

    Uses the locked commit SHA for reproducibility, unless:
    * ``update_refs=True`` — intentional update run; use the manifest ref.
    * ``ref_changed=True`` — the user changed the pin; use the manifest ref.

    Args:
        dep_ref: The dependency as declared in the current manifest.
        existing_lockfile: Existing lockfile, or ``None`` on first install.
        update_refs: Whether ``--update`` mode is active.
        ref_changed: Whether :func:`detect_ref_change` returned ``True`` for
                     this dependency.

    Returns:
        A :class:`DependencyReference` suitable for
        ``GitHubPackageDownloader.download_package``.
    """
    from apm_cli.deps.revision_pins import is_full_revision_pin

    if require_locked:
        locked = (
            existing_lockfile.get_dependency(dep_ref.get_unique_key())
            if existing_lockfile is not None
            else None
        )
        if locked is None or update_refs or ref_changed or locked.source == "local":
            raise ValueError("source has no unchanged remote lock pin")
        if locked.source == "registry":
            if not has_complete_registry_pin(locked):
                raise ValueError("registry source has no complete locked version, URL and hash")
        elif not is_full_revision_pin(locked.resolved_commit):
            raise ValueError("remote source has no immutable locked commit")

    fetch_commit = getattr(dep_ref, "fetch_commit", None)
    if isinstance(fetch_commit, str) and is_full_revision_pin(fetch_commit):
        # Resolution (update or --refresh) pinned this package's same-repository
        # children to this commit (#5525); the package comes from it too.
        return _dataclass_replace(dep_ref.source_reference, reference=fetch_commit)

    if existing_lockfile and not update_refs and not ref_changed:
        locked_dep = existing_lockfile.get_dependency(dep_ref.get_unique_key())
        if locked_dep and locked_dep.matches_replacement(dep_ref):
            overrides: dict[str, Any] = {}

            # Registry-proxy rows preserve their configured proxy host for
            # reproducibility. Ordinary dependency hosts and insecure transport
            # are always taken from the current manifest, never from lock state.
            # When registry_prefix is set, also restore the artifactory_prefix
            # field on dep_ref so the downloader takes the proxy code-path and
            # uses PROXY_REGISTRY_TOKEN for auth instead of the GitHub PAT.
            # A dep whose current route already lands on that proxy keeps it,
            # with the configured scheme (#3657).
            if (
                locked_dep.registry_prefix
                and locked_dep.host
                and not _routes_to_locked_proxy(dep_ref, locked_dep)
            ):
                overrides["host"] = locked_dep.host
                overrides["artifactory_prefix"] = locked_dep.registry_prefix

            reg_replay = _registry_replay_overrides_from_lock(locked_dep)
            if reg_replay is not None:
                overrides.update(reg_replay)
            # Use locked commit SHA for byte-for-byte reproducibility
            # (bypassed during --update, also guarded by the helper itself).
            elif _should_use_locked_ref(locked_dep.resolved_commit, update_refs):
                overrides["reference"] = locked_dep.resolved_commit
            # For proxy deps without a commit SHA (Artifactory zip archives),
            # preserve the locked ref so we download the same ref on replay.
            elif locked_dep.registry_prefix and locked_dep.resolved_ref and not dep_ref.reference:
                overrides["reference"] = locked_dep.resolved_ref

            if overrides:
                return _dataclass_replace(dep_ref.source_reference, **overrides)

    return dep_ref.source_reference
