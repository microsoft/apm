"""Canonical read-only view of current MCP source configuration.

The view derives current MCP declarations from the root manifest and only the
package manifests bounded by the current lockfile. It owns first-wins
projection and the symmetric comparison with the stored lockfile baseline.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import yaml

from apm_cli.core.remediation import (
    MCP_ADMITTED_DENIED,
    MCP_ADMITTED_UNAPPROVED,
    MCP_LOCAL_PACKAGE_UNRESOLVED,
    MCP_PACKAGE_MANIFEST_MISSING,
)
from apm_cli.core.scope import is_user_scope_root, manifest_file
from apm_cli.deps.lockfile_write_authority import lockfile_exists
from apm_cli.deps.path_anchoring import (
    LocalResolutionError,
    build_local_parent_index,
    resolve_local_dep_dir,
)
from apm_cli.deps.plugin_root_render import plugin_root_of
from apm_cli.integration._shared import deduplicate_deps
from apm_cli.models.apm_package import APMPackage
from apm_cli.models.dependency.mcp import MCPDependency
from apm_cli.models.validation import PackageType, detect_package_type
from apm_cli.security.executables import _strip_version, locked_dependency_approval_keys
from apm_cli.security.trust import (
    REASON_DEFINITION_CHANGED,
    REASON_DENIED,
    REASON_UNAPPROVED,
    REASON_UNTRUSTED_TRANSITIVE,
    McpAdmission,
)
from apm_cli.security.trust_fingerprint import evidence_fingerprint

if TYPE_CHECKING:
    from apm_cli.deps.lockfile import LockedDependency, LockFile
    from apm_cli.utils.diagnostics import DiagnosticCollector


class _McpViewLogger(Protocol):
    """Output methods used while deriving the current MCP view."""

    def progress(self, message: str) -> None:
        """Report an install-time trust decision."""

    def warning(self, message: str) -> None:
        """Report a trust decision that needs user attention."""

    def verbose_detail(self, message: str) -> None:
        """Report a non-actionable derivation detail in verbose mode."""


@dataclass(frozen=True)
class McpSourceProblem:
    """A locked package manifest that could not provide current MCP truth."""

    package_key: str
    manifest_path: Path
    message: str


@dataclass(frozen=True)
class CurrentMcpConfigView:
    """Current root plus lockfile-bounded MCP declarations and projections."""

    dependencies: tuple[MCPDependency, ...]
    configs: Mapping[str, Mapping[str, Any]]
    provenance: Mapping[str, str]
    problems: tuple[McpSourceProblem, ...]
    declaration_fingerprints: Mapping[str, str] = field(default_factory=dict)
    omissions: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    refusable_names: frozenset[str] = frozenset()
    pending_packages: frozenset[str] = frozenset()
    # Transitive server name -> lock key of the single direct (apm.yml)
    # dependency that brings it in. Unlike ``provenance`` (the declaring
    # package's display name) this is an entry the user can remove; servers
    # whose chain is ambiguous or unknown are absent.
    brought_in_by: Mapping[str, str] = field(default_factory=dict)
    # Server name -> materialized package root of an authored
    # ``${CLAUDE_PLUGIN_ROOT}``. ``configs`` keep the placeholder (#3825); this
    # only lets ``diff`` accept a lock recorded before that, already expanded.
    plugin_roots: Mapping[str, str] = field(default_factory=dict)
    project_root: Path | None = None
    # Recorded local-bundle servers (``bundle:<owner>`` provenance) kept in
    # ``configs``: ``apm install <bundle>`` owns them, never the dependency graph.
    bundle_owned: frozenset[str] = frozenset()
    # Dependency servers the executables gate decides (self-defined).
    gated_names: frozenset[str] = frozenset()
    # Server name -> lock identity of the package to ``apm approve``.
    approval_targets: Mapping[str, str] = field(default_factory=dict)
    # ``(server, package)`` admitted only by this run's --trust-transitive-mcp.
    one_off_admitted: tuple[tuple[str, str], ...] = ()
    # True once ``select`` replayed this run's complete selection (executables
    # gate, dependency inclusion, transitive trust): ``omissions`` then holds
    # exactly the evidence this run would record, so recorded evidence must equal it.
    selection_replayed: bool = False

    @classmethod
    def select(
        cls,
        root: APMPackage,
        lockfile: LockFile | None,
        modules_root: Path,
        *,
        admission: McpAdmission,
        recorded: LockFile | None,
        exact: bool = True,
        diagnostics: DiagnosticCollector | None = None,
        logger: _McpViewLogger | None = None,
        include_dependency_mcp: bool = True,
        allow_pending: bool | None = None,
        active_package_keys: frozenset[str] | None = None,
        claude_native_keys: frozenset[str] = frozenset(),
    ) -> CurrentMcpConfigView:
        """Derive the view install, ``--frozen`` and audit compare with the lockfile.

        *admission* is the trust owner's gate for this run (security/trust.py).
        ``recorded`` is the committed lockfile whose omission evidence must
        justify every left-out server; only install, which records new
        evidence, passes None. With *exact* (install, ``--frozen``) recorded
        evidence must equal what this run would record; audit passes False and
        accepts any reason the server's structure permits, while consent is
        re-checked through ``consent_problems``.
        """
        view = cls.derive(
            root,
            lockfile,
            modules_root,
            admission=admission,
            diagnostics=diagnostics,
            logger=logger,
            include_dependency_mcp=include_dependency_mcp,
            allow_pending=allow_pending,
            active_package_keys=active_package_keys,
            claude_native_keys=claude_native_keys,
        )
        if exact:
            view = replace(view, selection_replayed=True)
        if recorded is not None:
            view = view.with_recorded_omissions(recorded)
        return view

    @classmethod
    def derive(
        cls,
        root: APMPackage,
        lockfile: LockFile | None,
        modules_root: Path,
        *,
        admission: McpAdmission,
        diagnostics: DiagnosticCollector | None = None,
        logger: _McpViewLogger | None = None,
        include_dependency_mcp: bool = True,
        allow_pending: bool | None = None,
        active_package_keys: frozenset[str] | None = None,
        claude_native_keys: frozenset[str] = frozenset(),
    ) -> CurrentMcpConfigView:
        """Derive MCP truth for the resolver's complete active graph, when supplied.

        Every dependency-declared server is admitted through *admission* (the
        security/trust.py owner), so install, update, ``--frozen`` and audit
        reach the same verdict for the same inputs.

        ``allow_pending`` selects pending (True), strict (False), or legacy
        compatibility (None) evidence. Active membership is not a materialized
        package selection: unavailable active sources still require evidence.
        """
        project_root = root.package_path or Path.cwd()
        with ExitStack() as scratch:
            collected = _collect_locked_dependencies(
                lockfile,
                modules_root,
                project_root,
                scratch=scratch,
                admission=admission,
                diagnostics=diagnostics,
                logger=logger,
                include_dependency_mcp=include_dependency_mcp,
                allow_pending=allow_pending,
                active_package_keys=active_package_keys,
                claude_native_keys=claude_native_keys,
            )
        # Root is the authoring project: both prod and dev MCP are intentionally
        # included here (behaviour introduced in #1780). Transitive package deps
        # are collected prod-only (see _collect_locked_dependencies and
        # _collect_unlocked_compat).
        root_deps = root.get_all_mcp_dependencies()
        root_names = set(_get_server_configs(root_deps))
        dependencies = tuple(_deduplicate(root_deps + collected.dependencies))
        configs = _get_server_configs(dependencies)
        bundle_owned = _granted_bundle_servers(
            _bundle_owned_baseline(lockfile, declared=configs), admission, modules_root
        )
        configs.update({name: lockfile.mcp_configs[name] for name in bundle_owned})
        return cls(
            dependencies=dependencies,
            configs=configs,
            provenance={**_get_server_provenance(dependencies), **bundle_owned},
            bundle_owned=frozenset(bundle_owned),
            problems=tuple(collected.problems),
            pending_packages=frozenset(collected.pending_packages),
            declaration_fingerprints={
                name: fingerprint
                for name, fingerprint in collected.fingerprints.items()
                if name not in root_names
            },
            omissions={
                name: evidence
                for name, evidence in collected.omissions.items()
                if name not in configs
            },
            refusable_names=frozenset(collected.refusable - root_names),
            brought_in_by={
                name: key
                for name, key in collected.brought_in_by.items()
                if key is not None and name not in root_names
            },
            plugin_roots={
                dependency.name: root
                for dependency in dependencies
                if (root := plugin_root_of(dependency)) is not None
            },
            project_root=project_root,
            gated_names=frozenset(collected.gated - root_names),
            approval_targets={
                name: target
                for name, target in collected.approval_targets.items()
                if name not in root_names
            },
            one_off_admitted=tuple(
                (name, package)
                for name, package in collected.one_off
                if name not in root_names and name in configs
            ),
        )

    def omission_evidence(self, lockfile: LockFile) -> dict[str, dict[str, str]]:
        """Record explicit omissions without erasing a previously admitted baseline."""
        admitted = _admitted_names(lockfile)
        return {
            name: dict(evidence)
            for name, evidence in self.omissions.items()
            if name not in admitted
        }

    def with_recorded_omissions(self, lockfile: LockFile) -> CurrentMcpConfigView:
        """Apply exact install evidence, never infer intent from empty state.

        After this run's executables gate decided the view, recorded evidence
        must equal what this run would record. Otherwise (audit) any reason the
        server's structure permits is accepted.
        """
        configs = dict(self.configs)
        problems = list(self.problems)
        admitted = _admitted_names(lockfile)
        # Parking is always safe, so any reason the current inputs can produce
        # for a server is accepted evidence; admission is what audit verifies.
        structural = {
            "only-apm": set(self.declaration_fingerprints),
            REASON_UNTRUSTED_TRANSITIVE: self.refusable_names,
            REASON_UNAPPROVED: self.gated_names,
            REASON_DENIED: self.gated_names | self.refusable_names,
            REASON_DEFINITION_CHANGED: self.gated_names | self.refusable_names,
        }
        candidates = (
            (set(lockfile.mcp_omissions) & set(configs)) | set(self.declaration_fingerprints)
        ) - admitted
        for name in sorted(candidates):
            evidence = lockfile.mcp_omissions.get(name)
            fingerprint = self.declaration_fingerprints.get(name)
            if self.selection_replayed:
                valid = fingerprint is not None and evidence == self.omissions.get(name, False)
            else:
                valid = (
                    fingerprint is not None
                    and isinstance(evidence, dict)
                    and isinstance(evidence.get("reason"), str)
                    and evidence.get("reason") in structural
                    and evidence
                    == {
                        "declaration": fingerprint,
                        "reason": evidence.get("reason"),
                    }
                    and name in structural[evidence["reason"]]
                )
            if valid and name not in admitted:
                configs.pop(name, None)
            elif self.selection_replayed and evidence is None and name in configs:
                # This run deploys it; the diff reports it missing from the lock.
                continue
            elif name not in admitted or evidence is not None:
                problems.append(
                    McpSourceProblem(
                        package_key=name,
                        manifest_path=Path("apm.lock.yaml"),
                        message=(
                            "MCP selection is unknown: omission evidence is missing, damaged "
                            "or stale. Repeat the intended install selection "
                            "('--only apm' for skills-only); do not enable MCP just to pass "
                            "this check"
                        ),
                    )
                )
        for name, message in self.consent_problems(lockfile).items():
            # The locked config stays in the comparison so the consent problem
            # is the single, actionable signal for this server.
            if name in lockfile.mcp_configs:
                configs[name] = lockfile.mcp_configs[name]
            problems.append(
                McpSourceProblem(
                    package_key=name, manifest_path=Path("apm.lock.yaml"), message=message
                )
            )
        return replace(self, configs=configs, problems=tuple(problems))

    def consent_problems(self, lockfile: LockFile) -> dict[str, str]:
        """Servers the lock admitted that current manifest consent parks (#3716).

        The lockfile is evidence, never consent: a lock-only edit or a lock
        produced by a one-off ``--trust-transitive-mcp`` run cannot keep a
        server that ``apm.yml`` does not approve.
        """
        problems: dict[str, str] = {}
        admitted = set(lockfile.mcp_configs) | set(lockfile.mcp_servers)
        for name in sorted(admitted):
            evidence = self.omissions.get(name)
            reason = evidence.get("reason") if isinstance(evidence, Mapping) else None
            if reason in (None, "only-apm") or name in self.configs:
                continue
            target = self.approval_targets.get(name, "<package>")
            if reason == REASON_DENIED:
                # A deny is not a missing approval: name the gate and the deny (#3957).
                problems[name] = (
                    "admitted in apm.lock.yaml but the executables gate now parks it "
                    f"(denied); {MCP_ADMITTED_DENIED.bind(target=target).clause()}"
                )
                continue
            problems[name] = (
                f"admitted in apm.lock.yaml but not approved in apm.yml ({reason}); "
                f"{MCP_ADMITTED_UNAPPROVED.bind(target=target).clause()}"
            )
        return problems

    def pending_servers(self, lockfile: LockFile) -> frozenset[str]:
        """Defer only unambiguous recorded declarers whose sources are unavailable."""
        declarers: dict[str, list[str]] = {}
        for key, dependency in lockfile.dependencies.items():
            if key != "." and dependency.name:
                declarers.setdefault(dependency.name, []).append(key)
        pending_names = {
            name
            for name, keys in declarers.items()
            if len(keys) == 1 and keys[0] in self.pending_packages
        }
        pending = set()
        for server, provenance in lockfile.mcp_config_provenance.items():
            owners = {provenance} if isinstance(provenance, str) else set(provenance)
            if owners and owners <= pending_names and server not in self.configs:
                pending.add(server)
        return frozenset(pending)

    def _legacy_equivalent(self, name: str, current: Any, stored: Any) -> bool:
        """Whether *stored* is *current* recorded with its plugin root expanded."""
        from apm_cli.deps.plugin_root_render import legacy_absolute_equivalent

        return legacy_absolute_equivalent(
            current,
            stored,
            plugin_root=self.plugin_roots.get(name),
            project_root=self.project_root,
        )

    def kept_dependencies(self) -> tuple[Any, ...]:
        """Every server this view keeps as baseline, for org MCP policy.

        Root, transitive, and recorded local-bundle servers all deploy, so
        policy judges each. A kept config with no current declaration (a
        bundle-owned or consent-retained lock entry) is parsed from its
        recorded config; one that no longer parses is judged by name.
        """
        kept: list[Any] = []
        seen: set[str] = set()
        for dependency in self.dependencies:
            name = getattr(dependency, "name", dependency)
            if name in self.configs and name not in seen:
                kept.append(dependency)
                seen.add(name)
        for name in sorted(set(self.configs) - seen):
            try:
                kept.append(MCPDependency.from_dict(dict(self.configs[name])))
            except (ValueError, TypeError):
                kept.append(MCPDependency(name=name, registry=False))
        return tuple(kept)

    def diff(self, stored_configs: Mapping[str, Mapping[str, Any]]) -> McpConfigDiff:
        """Compare this current view with a stored lockfile baseline."""
        return McpConfigDiff.between(
            self.configs, stored_configs, equivalent=self._legacy_equivalent
        )

    def replay_recorded_roots(
        self, dependencies: list[Any], stored_configs: Mapping[str, Mapping[str, Any]]
    ) -> list[Any]:
        """Mark pre-#3825 recorded roots so ``--frozen`` keeps unchanged native entries.

        A lock written before #3825 holds ``${CLAUDE_PLUGIN_ROOT}`` already
        expanded, possibly at another clone's path. The recorded root is never
        rendered: a native entry that already equals the recorded render is
        kept byte-for-byte, so its lock receipt stays valid, and any other entry
        gets this project's portable render. The next ordinary install migrates
        both.
        """
        from apm_cli.deps.plugin_root_render import legacy_absolute_root

        replayed: list[Any] = []
        for dependency in dependencies:
            name = getattr(dependency, "name", None)
            current = self.configs.get(name) if isinstance(name, str) else None
            stored = stored_configs.get(name) if current is not None else None
            root = (
                legacy_absolute_root(
                    current,
                    stored,
                    plugin_root=self.plugin_roots.get(name),
                    project_root=self.project_root,
                )
                if stored is not None and stored != current
                else None
            )
            if root is not None:
                dependency = replace(dependency, recorded_plugin_root=root)
            replayed.append(dependency)
        return replayed


@dataclass(frozen=True)
class McpConfigDiff:
    """Symmetric name and value differences between current and stored MCP config."""

    changed: frozenset[str]
    source_only: frozenset[str]
    lock_only: frozenset[str]

    @classmethod
    def between(
        cls,
        current: Mapping[str, Mapping[str, Any]],
        stored: Mapping[str, Mapping[str, Any]],
        *,
        equivalent: Callable[[str, Any, Any], bool] | None = None,
    ) -> McpConfigDiff:
        """Partition changed, source-only, and lock-only server names.

        *equivalent* may accept a differing stored value as the same declaration.
        """
        current_names = frozenset(current)
        stored_names = frozenset(stored)
        shared = current_names & stored_names
        return cls(
            changed=frozenset(
                name
                for name in shared
                if current[name] != stored[name]
                and not (equivalent is not None and equivalent(name, current[name], stored[name]))
            ),
            source_only=current_names - stored_names,
            lock_only=stored_names - current_names,
        )

    @property
    def is_empty(self) -> bool:
        """Return whether current and stored configurations are identical."""
        return not (self.changed or self.source_only or self.lock_only)


# Trust notices already shown in this process (#4256): one install collects
# the locked MCP view in several passes, but the user needs one notice per server.
_TRUST_NOTICES_SHOWN: set[str] = set()


def _progress_trust_once(logger: Any, message: str) -> None:
    if message in _TRUST_NOTICES_SHOWN:
        return
    _TRUST_NOTICES_SHOWN.add(message)
    logger.progress(message)


def _deduplicate(dependencies: list[Any] | tuple[Any, ...]) -> list[Any]:
    """Return first-wins dependencies using the shared MCP/LSP semantics."""
    return deduplicate_deps(list(dependencies))


def _admitted_names(lockfile: LockFile) -> set[str]:
    """Retain every surviving indication of a previously admitted MCP baseline."""
    return (
        set(lockfile.mcp_servers)
        | set(lockfile.mcp_configs)
        | set(lockfile.mcp_config_provenance)
        | {name for names in lockfile.mcp_target_servers.values() for name in names}
    )


class _DirectDependencyIndex:
    """Trace locked packages through their recorded parents to apm.yml entries.

    ``resolved_by`` holds the parent's lock key (replacement-enabled locks) or
    its canonical repo URL (legacy locks), so both forms are resolved here.
    """

    def __init__(self, lockfile: LockFile) -> None:
        self._lockfile = lockfile
        self._by_repo_url: dict[str, list[str]] = {}
        for key, dependency in lockfile.dependencies.items():
            if key != ".":
                self._by_repo_url.setdefault(dependency.repo_url, []).append(key)
        self._memo: dict[str, frozenset[str] | None] = {}

    def _parent_keys(self, identity: str) -> list[str]:
        if identity in self._lockfile.dependencies:
            return [identity]
        if self._lockfile.replacements is not None:
            parent = self._lockfile.get_dependency(identity)
            return [parent.get_unique_key()] if parent is not None else []
        return self._by_repo_url.get(identity, [])

    def roots(self, key: str, visiting: frozenset[str] = frozenset()) -> frozenset[str] | None:
        """Return the direct dependency keys that bring in *key*, or None if unknown."""
        if key in self._memo:
            return self._memo[key]
        dependency = self._lockfile.dependencies.get(key)
        if dependency is None or key in visiting:
            return None
        visiting = visiting | {key}
        found: set[str] = set()
        for parent in dependency.parent_keys:
            if parent is None:
                if dependency.depth > 1:
                    return self._remember(key, None)
                found.add(key)
                continue
            candidates = self._parent_keys(parent)
            if not candidates:
                return self._remember(key, None)
            for candidate in candidates:
                parent_roots = self.roots(candidate, visiting)
                if parent_roots is None:
                    return self._remember(key, None)
                found |= parent_roots
        return self._remember(key, frozenset(found) or None)

    def _remember(self, key: str, value: frozenset[str] | None) -> frozenset[str] | None:
        self._memo[key] = value
        return value


def _declaration_fingerprint(dependency: LockedDependency, declaration: MCPDependency) -> str:
    """Bind intent to canonical package identity, immutable source and exact declaration."""
    payload = {
        "dependency": dependency.get_unique_key(),
        "commit": dependency.resolved_commit,
        "digest": dependency.resolved_hash,
        "port": dependency.port,
        "source": dependency.source,
        "depth": dependency.depth,
        "declaration": declaration.to_dict(),
    }
    return evidence_fingerprint(payload)


def _authored_mcp_declarations(manifest: dict[str, Any]) -> dict[str, MCPDependency]:
    """Parse portable authored declarations through the canonical MCP model."""
    dependencies = manifest.get("dependencies") or {}
    if not isinstance(dependencies, dict):
        raise ValueError("package dependencies must be a mapping")
    entries = dependencies.get("mcp")
    if not isinstance(entries, list):
        return {}
    declarations: dict[str, MCPDependency] = {}
    for entry in entries:
        if isinstance(entry, str):
            declaration = MCPDependency.from_string(entry)
        elif isinstance(entry, dict):
            declaration = MCPDependency.from_dict(entry)
        else:
            continue
        declarations.setdefault(declaration.name, declaration)
    return declarations


def _get_server_configs(dependencies: list[Any] | tuple[Any, ...]) -> dict[str, dict[str, Any]]:
    """Project dependencies to their serialized server configurations."""
    configs: dict[str, dict[str, Any]] = {}
    for dependency in dependencies:
        if hasattr(dependency, "to_dict") and hasattr(dependency, "name"):
            configs[dependency.name] = dependency.to_dict()
        elif isinstance(dependency, str):
            configs[dependency] = {"name": dependency}
    return configs


BUNDLE_MCP_OWNER_PREFIX = "bundle:"


def bundle_mcp_owner_token(owner: str) -> str:
    """Return the ``mcp_config_provenance`` token for a local bundle's MCP servers."""
    return f"{BUNDLE_MCP_OWNER_PREFIX}{owner}"


def _granted_bundle_servers(
    owned: dict[str, str], admission: McpAdmission, modules_root: Path
) -> dict[str, str]:
    """Keep the bundle servers whose recorded bundle approval still grants MCP (RA-025).

    A local bundle is not in apm.yml, so its servers stay on the lock's
    baseline; a deny or a revoked approval of the exact bundle key the install
    recorded (``install/local_bundle_pending.py``) drops them here, so install
    retires them as it retires the bundle's hooks. A bundle without a record
    keeps its baseline.
    """
    if not owned:
        return owned
    context = admission.context
    if context is None:
        return owned
    from apm_cli.install.local_bundle_pending import bundle_grant_withdrawn
    from apm_cli.security.executables import EXEC_TYPE_MCP

    return {
        name: token
        for name, token in owned.items()
        if not bundle_grant_withdrawn(
            modules_root.parent, token[len(BUNDLE_MCP_OWNER_PREFIX) :], context, EXEC_TYPE_MCP
        )
    }


def _bundle_owned_baseline(
    lockfile: LockFile | None, *, declared: Mapping[str, Any]
) -> dict[str, str]:
    """Recorded local-bundle servers that no current declaration supersedes.

    Their only source is ``apm install <bundle>``, so the lock is their
    baseline until that bundle is reinstalled without them. A ``bundle:``
    provenance alone is a claim, not a deployment: the server is baseline
    only when the lock also holds the native-entry receipt (an MCP URI
    deployment row with a content hash) that a bundle install records.
    """
    if lockfile is None:
        return {}
    from apm_cli.core.deployment_ledger import DeploymentLedgerCodec

    deployed = {
        name
        for hashes in DeploymentLedgerCodec.mcp_native_hashes(lockfile).values()
        for name in hashes
    }
    return {
        name: owner
        for name, owner in lockfile.mcp_config_provenance.items()
        if isinstance(owner, str)
        and owner.startswith(BUNDLE_MCP_OWNER_PREFIX)
        and name not in declared
        and name in lockfile.mcp_configs
        and name in deployed
    }


def mcp_package_owner(*names: str | None, package_dir: Path) -> str:
    """Return the ``mcp_config_provenance`` token for a package's MCP servers.

    The declaring package is named by the first non-empty manifest or lock
    name, falling back to its materialized directory name. A package can
    never claim the local-bundle owner namespace.
    """
    owner = next((name for name in names if name), package_dir.name)
    return f"package:{owner}" if owner.startswith(BUNDLE_MCP_OWNER_PREFIX) else owner


def _get_server_provenance(dependencies: list[Any] | tuple[Any, ...]) -> dict[str, str]:
    """Project surviving transitive dependencies to their declaring package."""
    provenance: dict[str, str] = {}
    for dependency in dependencies:
        resolved_by = getattr(dependency, "resolved_by", None)
        if resolved_by and hasattr(dependency, "name"):
            provenance[dependency.name] = resolved_by
    return provenance


def _log_skipped_dev_mcp(
    package: APMPackage,
    declarer: str,
    logger: _McpViewLogger | None,
) -> None:
    """Explain a dependency-scope exclusion without logging server names."""
    skipped_count = len(package.get_dev_mcp_dependencies())
    if skipped_count and logger is not None:
        logger.verbose_detail(
            f"Skipping {skipped_count} author-only MCP server(s) from "
            f"'{declarer}'; transitive devDependencies.mcp do not propagate"
        )


def _fallback_manifest_path(
    dependency: LockedDependency,
    modules_root: Path,
    project_root: Path,
) -> Path:
    """Return a useful manifest location when canonical path resolution fails."""
    if dependency.source == "local" and dependency.local_path:
        raw = Path(dependency.local_path).expanduser()
        package_dir = raw if raw.is_absolute() else project_root / raw
        return manifest_file(package_dir).resolve()
    return manifest_file(modules_root / dependency.repo_url).resolve()


def _is_local_claude_plugin_source(dependency: LockedDependency, package_dir: Path) -> bool:
    """Return whether a local source directory is a Claude plugin without apm.yml."""
    from apm_cli.codex_plugins.manifest import is_codex_compatibility_manifest

    if dependency.package_type != PackageType.MARKETPLACE_PLUGIN.value:
        return False
    if not package_dir.is_dir() or manifest_file(package_dir).exists():
        return False
    package_type, selected_manifest = detect_package_type(package_dir)
    return package_type is PackageType.MARKETPLACE_PLUGIN and not (
        is_codex_compatibility_manifest(package_dir, selected_manifest)
    )


def _package_manifest_path(
    dependency: LockedDependency,
    lockfile: LockFile,
    modules_root: Path,
    project_root: Path,
    *,
    scratch: ExitStack,
    parent_index: dict[str, tuple[LockedDependency, ...]],
    resolved_cache: dict[str, Path],
) -> Path:
    """Resolve a locked package's canonical current manifest path."""
    if dependency.source == "local":
        package_dir = resolve_local_dep_dir(
            dependency,
            lockfile,
            project_root,
            parent_index=parent_index,
            resolved_cache=resolved_cache,
        )
        if package_dir.is_file():
            from tempfile import TemporaryDirectory

            from apm_cli.config import get_apm_temp_dir
            from apm_cli.deps.virtual_file import materialize_virtual_file_package
            from apm_cli.install.phases.local_content import read_local_package_source

            source_base = resolve_local_dep_dir(
                replace(dependency, local_path="."),
                lockfile,
                project_root,
                parent_index=parent_index,
            )
            dep_ref = dependency.to_dependency_ref()
            source = read_local_package_source(dep_ref, source_base)
            if source.content is None:
                raise ValueError(
                    f"Local single-file source changed kind during MCP inspection: {package_dir}"
                )
            stage = scratch.enter_context(
                TemporaryDirectory(prefix="apm-local-mcp-", dir=get_apm_temp_dir())
            )
            package_dir = Path(stage) / "package"
            materialize_virtual_file_package(dep_ref, package_dir, source.path.name, source.content)
        elif _is_local_claude_plugin_source(dependency, package_dir):
            # A Claude plugin has no authored apm.yml; install synthesizes the
            # canonical manifest (with its MCP servers) under apm_modules/_local.
            package_dir = dependency.to_dependency_ref().get_install_path(modules_root)
    else:
        package_dir = dependency.to_dependency_ref().get_install_path(modules_root)
    return manifest_file(package_dir).resolve()


def _allows_missing_manifest(
    dependency: LockedDependency,
    package_dir: Path,
    logger: _McpViewLogger | None = None,
    *,
    modules_root: Path | None = None,
    native_ownership_cache: dict[Path, frozenset[str]] | None = None,
    strict: bool = False,
) -> bool:
    """Return whether the package contract permits an absent apm.yml."""
    from apm_cli.codex_plugins.manifest import is_codex_compatibility_manifest

    if not strict:
        if dependency.package_type == PackageType.SKILL_BUNDLE.value:
            return True
        if not package_dir.exists():
            if dependency.source == "local":
                return False
            if dependency.package_type == PackageType.MARKETPLACE_PLUGIN.value:
                from apm_cli.codex_plugins.registrar import owns_codex_dependency

                return modules_root is not None and owns_codex_dependency(
                    modules_root,
                    dependency.get_unique_key(),
                    ownership_cache=native_ownership_cache,
                )
            accepted_types = {
                PackageType.APM_PACKAGE.value: "APM package",
                PackageType.CLAUDE_SKILL.value: "Claude skill",
                PackageType.AGENT_PLUGIN.value: "Agent plugin",
            }
            accepted_kind = accepted_types.get(dependency.package_type)
            if accepted_kind is None:
                return False
            if logger is not None:
                dep_label = dependency.name or package_dir.name
                logger.verbose_detail(
                    f"Skipping MCP check for '{dep_label}' -- locked {accepted_kind}; "
                    "package dir absent (cold cache; will hydrate from lock pins)"
                )
            return True
    elif not package_dir.exists():
        return False

    # A materialized Claude skill legitimately has no apm.yml, whether it was
    # declared at the repository root or in a virtual subdirectory. The lock
    # type and installed shape must agree before the manifest is waived.

    package_type, selected_manifest = detect_package_type(package_dir)
    if package_type is PackageType.SKILL_BUNDLE:
        return dependency.package_type == PackageType.SKILL_BUNDLE.value
    if package_type is PackageType.HOOK_PACKAGE:
        return (
            dependency.to_dependency_ref().is_virtual_subdirectory()
            and dependency.package_type == PackageType.HOOK_PACKAGE.value
        )
    if package_type is PackageType.AGENT_PLUGIN:
        return dependency.package_type == PackageType.AGENT_PLUGIN.value
    if is_codex_compatibility_manifest(package_dir, selected_manifest):
        return dependency.package_type == PackageType.MARKETPLACE_PLUGIN.value
    return (
        package_type is PackageType.CLAUDE_SKILL
        and dependency.package_type == PackageType.CLAUDE_SKILL.value
    )


@dataclass
class _CollectedDependencies:
    """Admitted dependency MCP declarations plus the evidence behind each verdict.

    ``brought_in_by`` maps each collected server to the single direct
    dependency key that brings it in, or None when that is not unique/known.
    """

    dependencies: list[MCPDependency] = field(default_factory=list)
    problems: list[McpSourceProblem] = field(default_factory=list)
    fingerprints: dict[str, str] = field(default_factory=dict)
    omissions: dict[str, dict[str, str]] = field(default_factory=dict)
    refusable: set[str] = field(default_factory=set)
    gated: set[str] = field(default_factory=set)
    pending_packages: set[str] = field(default_factory=set)
    brought_in_by: dict[str, str | None] = field(default_factory=dict)
    approval_targets: dict[str, str] = field(default_factory=dict)
    one_off: list[tuple[str, str]] = field(default_factory=list)


def _copy_contradicts_lock(
    package_key: str, dependency: LockedDependency, modules_root: Path, lockfile: LockFile
) -> bool:
    """Whether the apm_modules copy differs from its locked ``content_hash`` (#4389).

    A frozen install then judges this package's declarations from the verified
    copy it restores (the strict post-restoration check), never from these bytes.
    """
    from apm_cli.install.source_helpers import locked_content_mismatch

    try:
        package_root = dependency.to_dependency_ref().get_install_path(modules_root)
    except (OSError, ValueError):
        return False
    return package_root.is_dir() and locked_content_mismatch(
        package_key, dependency, package_root, lockfile=lockfile
    )


def _collect_locked_dependencies(
    lockfile: LockFile | None,
    modules_root: Path,
    project_root: Path,
    *,
    scratch: ExitStack,
    admission: McpAdmission,
    diagnostics: DiagnosticCollector | None,
    logger: _McpViewLogger | None = None,
    include_dependency_mcp: bool = True,
    allow_pending: bool | None = None,
    active_package_keys: frozenset[str] | None = None,
    claude_native_keys: frozenset[str] = frozenset(),
) -> _CollectedDependencies:
    """Collect MCP declarations from only package manifests named by the lockfile.

    Each server carries the runtimes that load it natively from its plugin
    (``claude_plugins.admission.native_service_runtimes``); writers skip them.
    """
    from apm_cli.claude_plugins.admission import native_service_runtimes

    result = _CollectedDependencies()
    if lockfile is None:
        return result

    collected = result.dependencies
    problems = result.problems
    fingerprints = result.fingerprints
    omissions = result.omissions
    refusable = result.refusable
    pending_packages = result.pending_packages
    direct_roots: dict[str, set[str] | None] = {}
    direct_index = _DirectDependencyIndex(lockfile)
    parent_index = build_local_parent_index(lockfile)
    resolved_cache: dict[str, Path] = {}
    native_ownership_cache: dict[Path, frozenset[str]] = {}
    for package_key, dependency in lockfile.dependencies.items():
        if package_key == "." or (
            active_package_keys is not None and package_key not in active_package_keys
        ):
            continue
        fallback_path = _fallback_manifest_path(dependency, modules_root, project_root)
        try:
            manifest_path = _package_manifest_path(
                dependency,
                lockfile,
                modules_root,
                project_root,
                scratch=scratch,
                parent_index=parent_index,
                resolved_cache=resolved_cache,
            )
        except LocalResolutionError as exc:
            problems.append(
                McpSourceProblem(
                    package_key=package_key,
                    manifest_path=fallback_path,
                    message=(
                        f"cannot resolve local package: {exc}; "
                        f"{MCP_LOCAL_PACKAGE_UNRESOLVED.clause()}"
                    ),
                )
            )
            continue
        except (OSError, ValueError) as exc:
            problems.append(
                McpSourceProblem(
                    package_key=package_key,
                    manifest_path=fallback_path,
                    message=f"cannot locate package manifest: {exc}",
                )
            )
            continue

        if (
            allow_pending
            and dependency.source == "local"
            and not manifest_path.parent.exists()
            and manifest_path.is_relative_to(modules_root.resolve())
        ):
            # A local Claude plugin's synthesized manifest is rebuilt from its
            # verified source during install; strict post-restoration checks
            # still compare it with the lockfile.
            pending_packages.add(package_key)
            if logger is not None:
                logger.verbose_detail(
                    f"Deferring cold-cache MCP verification for '{package_key}' "
                    "until its local plugin is restored."
                )
            continue

        if (
            allow_pending
            and dependency.source != "local"
            and (
                not manifest_path.parent.exists()
                or _copy_contradicts_lock(package_key, dependency, modules_root, lockfile)
            )
        ):
            from apm_cli.drift import build_download_ref

            try:
                package_type = PackageType(dependency.package_type)
                if package_type is PackageType.INVALID:
                    raise ValueError("locked package shape is invalid")
                build_download_ref(
                    dependency.to_dependency_ref(),
                    lockfile,
                    update_refs=False,
                    ref_changed=False,
                    require_locked=True,
                )
            except ValueError as exc:
                problems.append(
                    McpSourceProblem(
                        package_key,
                        manifest_path,
                        f"cannot restore missing package from lock evidence: {exc}",
                    )
                )
            else:
                pending_packages.add(package_key)
                if logger is not None:
                    logger.verbose_detail(
                        f"Deferring cold-cache MCP verification for '{package_key}' "
                        "until its locked source is restored."
                    )
            continue

        if not manifest_path.exists():
            if _allows_missing_manifest(
                dependency,
                manifest_path.parent,
                logger,
                modules_root=modules_root,
                native_ownership_cache=native_ownership_cache,
                strict=allow_pending is not None,
            ):
                continue
            message = (
                f"package manifest not found at {manifest_path}; "
                f"{MCP_PACKAGE_MANIFEST_MISSING.clause()}"
            )
            if (
                dependency.source == "local"
                and dependency.package_type == PackageType.MARKETPLACE_PLUGIN.value
            ):
                warning = (
                    f"MCP servers from local plugin '{dependency.name or package_key}' "
                    f"were not collected: {message}"
                )
                if diagnostics is not None:
                    diagnostics.warn(warning)
                elif logger is not None:
                    logger.warning(warning)
            problems.append(
                McpSourceProblem(
                    package_key=package_key,
                    manifest_path=manifest_path,
                    message=message,
                )
            )
            continue

        try:
            from apm_cli.utils.yaml_io import load_yaml

            manifest = load_yaml(manifest_path)
            if not isinstance(manifest, dict):
                raise ValueError("package manifest must be a mapping")
            # Collecting declarations only reads: never create user config.
            package = APMPackage.from_mapping(
                manifest,
                package_path=manifest_path.parent,
                source_path=manifest_path.parent,
                manifest_path=manifest_path,
                create_config=False,
            )
            authored_declarations = _authored_mcp_declarations(manifest)
        except (OSError, ValueError, UnicodeError, yaml.YAMLError) as exc:
            problems.append(
                McpSourceProblem(
                    package_key=package_key,
                    manifest_path=manifest_path,
                    message=f"cannot parse package manifest at {manifest_path}: {exc}",
                )
            )
            continue

        declarer = mcp_package_owner(
            package.name, dependency.name, package_dir=manifest_path.parent
        )
        _log_skipped_dev_mcp(package, declarer, logger)
        # Only collect prod MCP from transitive packages; devDependencies.mcp
        # are scoped to the package author's own environment (#2340).
        for mcp_dependency in package.get_mcp_dependencies():
            fingerprint = _declaration_fingerprint(
                dependency, authored_declarations[mcp_dependency.name]
            )
            fingerprints.setdefault(mcp_dependency.name, fingerprint)
            approval_keys = locked_dependency_approval_keys(dependency)
            target = _strip_version(approval_keys[0]) if approval_keys else package_key
            result.approval_targets.setdefault(mcp_dependency.name, target)
            if mcp_dependency.requires_transitive_trust and dependency.depth > 1:
                refusable.add(mcp_dependency.name)
            if mcp_dependency.is_self_defined:
                result.gated.add(mcp_dependency.name)
            if not include_dependency_mcp:
                omissions.setdefault(
                    mcp_dependency.name, {"declaration": fingerprint, "reason": "only-apm"}
                )
                continue
            # Fingerprint the authored declaration, as `apm approve` pins it,
            # not the copy with ${CLAUDE_PLUGIN_ROOT} already expanded.
            subject = admission.subject(
                dependency,
                authored_declarations.get(mcp_dependency.name, mcp_dependency),
                approval_keys,
            )
            verdict = admission.admit(subject)
            native_runtimes = native_service_runtimes(package_key, dependency, claude_native_keys)
            if not verdict.allowed:
                omissions.setdefault(
                    mcp_dependency.name,
                    {"declaration": fingerprint, "reason": str(verdict.reason)},
                )
                if native_runtimes and verdict.reason == REASON_UNTRUSTED_TRANSITIVE:
                    # The native plugin verdict applies the same refusal to
                    # the plugin Claude loads; its diagnostic is the one shown.
                    continue
                message = parked_mcp_message(
                    mcp_dependency,
                    target,
                    str(verdict.reason),
                    layer=verdict.layer,
                    unverifiable=subject.fingerprint is None,
                    user_scope=is_user_scope_root(project_root),
                )
                if diagnostics is not None:
                    diagnostics.warn(message)
                elif logger is not None:
                    logger.warning(message)
                continue
            if verdict.one_off:
                result.one_off.append((mcp_dependency.name, target))
            elif (
                logger is not None
                and mcp_dependency.is_self_defined
                and dependency.depth == 1
                and verdict.layer not in ("project-allow", "user-allow")
            ):
                _progress_trust_once(
                    logger,
                    f"Trusting direct dependency MCP '{mcp_dependency.name}' from '{declarer}'",
                )
            collected.append(
                replace(
                    mcp_dependency,
                    resolved_by=declarer,
                    approval_keys=approval_keys,
                    native_runtimes=native_runtimes,
                )
            )
            roots = direct_index.roots(package_key)
            known = direct_roots.setdefault(mcp_dependency.name, set())
            if roots is None or known is None:
                direct_roots[mcp_dependency.name] = None
            else:
                known.update(roots)
    result.brought_in_by = {
        name: next(iter(roots)) if roots is not None and len(roots) == 1 else None
        for name, roots in direct_roots.items()
    }
    return result


class _ParkedNoticeSilenced:
    """A logger view that drops parked-server notices and keeps every other line.

    ``apm install --frozen`` checks MCP drift before and after hydration; the
    MCP phase is the one printer of each parked server (#4422).
    """

    def __init__(self, logger: Any) -> None:
        self._logger = logger

    def warning(self, message: str, *args: Any, **kwargs: Any) -> None:
        if str(message).startswith("Parked MCP server"):
            return
        self._logger.warning(message, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._logger, name)


def without_parked_notices(logger: Any) -> Any:
    """Return *logger* without parked-server notices (None stays None)."""
    return None if logger is None else _ParkedNoticeSilenced(logger)


def parked_mcp_message(
    dependency: MCPDependency,
    package: str,
    reason: str,
    *,
    layer: str = "",
    unverifiable: bool = False,
    user_scope: bool = False,
) -> str:
    """Return the warning for one dependency MCP server the trust owner parked."""
    from apm_cli.security.trust import approve_command, explain_command

    name = dependency.name
    approve = approve_command(package, user_scope=user_scope)
    explain = explain_command(package, user_scope=user_scope)
    if reason == REASON_UNTRUSTED_TRANSITIVE:
        detail = (
            "self-defined"
            if dependency.is_self_defined
            else "custom registry or configuration overlay"
        )
        return (
            f"Parked MCP server '{name}' from transitive package {package} "
            f"({detail}, not approved)\n"
            f"    Approve for everyone: {approve}  |  "
            "This run only: --trust-transitive-mcp"
        )
    if reason == REASON_DEFINITION_CHANGED:
        why = (
            "its definition could not be verified (registry unreachable or unreadable)"
            if unverifiable
            else "definition changed since approval"
        )
        return (
            f"Parked MCP server '{name}' ({package}): {why}\n"
            f"    Review: {approve_command('--pending', user_scope=user_scope)}  |  "
            f"Re-approve: {approve}"
        )
    if reason == REASON_DENIED:
        return f"Parked MCP server '{name}' ({package}): denied ({layer})\n    Explain: {explain}"
    return (
        f"Parked MCP server '{name}' ({package}): executables not approved\n"
        f"    Explain: {explain}  |  Approve: {approve}"
    )


def _collect_unlocked_compat(
    modules_root: Path,
    *,
    admission: McpAdmission,
    diagnostics: DiagnosticCollector | None,
    logger: _McpViewLogger | None,
) -> list[MCPDependency]:
    """Preserve the legacy no-lock fallback for the compatibility wrapper only."""
    collected: list[MCPDependency] = []
    for manifest_path in modules_root.rglob("apm.yml"):
        try:
            package = APMPackage.from_apm_yml(manifest_path, source_path=manifest_path.parent)
        except (OSError, ValueError, UnicodeError):
            continue
        declarer = mcp_package_owner(package.name, package_dir=manifest_path.parent)
        _log_skipped_dev_mcp(package, declarer, logger)
        # Only collect prod MCP from packages; devDependencies.mcp are scoped to
        # the package author's own environment (#2340).
        for dependency in package.get_mcp_dependencies():
            if dependency.requires_transitive_trust and not admission.one_off:
                message = parked_mcp_message(dependency, declarer, REASON_UNTRUSTED_TRANSITIVE)
                if diagnostics is not None:
                    diagnostics.warn(message)
                elif logger is not None:
                    logger.warning(message)
                continue
            collected.append(replace(dependency, resolved_by=declarer))
    return collected


def _collect_transitive_compat(
    modules_root: Path,
    lock_path: Path | None,
    trust_private: bool,
    *,
    logger: _McpViewLogger | None,
    diagnostics: DiagnosticCollector | None,
) -> list[MCPDependency]:
    """Implement the legacy MCPIntegrator traversal through this owner."""
    from apm_cli.deps.lockfile import LockFile

    if not modules_root.exists():
        return []
    lockfile = (
        LockFile.read(lock_path) if lock_path is not None and lockfile_exists(lock_path) else None
    )
    if lockfile is None:
        return _collect_unlocked_compat(
            modules_root,
            admission=McpAdmission(None, one_off=trust_private),
            diagnostics=diagnostics,
            logger=logger,
        )
    project_root = lock_path.parent if lock_path is not None else Path.cwd()
    with ExitStack() as scratch:
        collected = _collect_locked_dependencies(
            lockfile,
            modules_root,
            project_root,
            scratch=scratch,
            admission=McpAdmission(None, one_off=trust_private),
            diagnostics=diagnostics,
            logger=logger,
        )
    return collected.dependencies
