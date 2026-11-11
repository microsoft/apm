"""Target profiles for multi-tool integration.

Harness descriptors register where APM primitives should land. Built-ins and
installed ``apm.harnesses`` entry points share the same profile resolver.

Resolver invariant (#820): both :func:`active_targets` and
:func:`active_targets_user_scope` accept ``Union[str, List[str]]`` for
``explicit_target`` but treat the two shapes identically -- string inputs
are wrapped to a one-element list before the resolution loop.  Validity
is enforced *upstream* by
:func:`apm_cli.core.target_detection.parse_target_field`, which is the
shared gatekeeper for both ``--target`` and ``apm.yml``'s ``target:``
field.  Unknown tokens never reach these functions in normal flow; if
one does, it falls through the loop without matching any profile and
the result is an empty list (no silent ``[copilot]`` fallback).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from click import ClickException

from apm_cli.core.target_catalog import TARGET_CAPABILITIES, expand_all, normalize_target_name
from apm_cli.harnesses.registry import get_harness_registry
from apm_cli.harnesses.types import (
    USER_ROOT_ENV_VARS,
    PrimitiveMapping,
    TargetProfile,
)
from apm_cli.utils.path_security import (
    PathTraversalError,
    ensure_path_within,
    has_symlink_component,
)

if TYPE_CHECKING:
    from apm_cli.deps.lockfile import LockFile


def _encode_cowork_locator(path: Path, deploy_root: Path) -> str:
    """Translate a Cowork path through its native target adapter."""
    from apm_cli.integration.copilot_cowork_paths import to_lockfile_path

    return to_lockfile_path(path, deploy_root)


def _decode_cowork_locator(locator: str, deploy_root: Path) -> Path:
    """Translate a Cowork lockfile URI through its native target adapter."""
    from apm_cli.integration.copilot_cowork_paths import from_lockfile_path

    return from_lockfile_path(locator, deploy_root)


def _encode_copilot_app_locator(path: Path) -> str:
    """Translate an app workflow row through its native target adapter."""
    from apm_cli.integration.copilot_app_db import to_lockfile_uri

    return to_lockfile_uri(path.name)


# ------------------------------------------------------------------
# Runtime -> canonical target alias map
# ------------------------------------------------------------------
#
# Several runtime identifiers used at the MCP-config layer (e.g. ``vscode``,
# ``agents``) emit configuration that lands inside the ``copilot`` target's
# tree.  The MCP gate (``mcp_integrator._gate_project_scoped_runtimes``) and
# the explicit-target resolution branch in :func:`active_targets` both need
# to map runtime -> canonical-target name in the same way.  Hold the table
# in one place to prevent the two sites drifting -- a silent drift would
# strip a runtime even when its canonical target is active (the same class
# of bug as #1335).
class _RuntimeProjection(Mapping[str, str]):
    def __getitem__(self, runtime: str) -> str:
        for capability in TARGET_CAPABILITIES.values():
            if runtime in capability.runtimes and capability.primitive_profile is not None:
                return capability.primitive_profile
        raise KeyError(runtime)

    def __iter__(self):
        return iter(
            dict.fromkeys(
                runtime
                for capability in TARGET_CAPABILITIES.values()
                if capability.primitive_profile is not None
                for runtime in capability.runtimes
            )
        )

    def __len__(self) -> int:
        return sum(
            len(capability.runtimes)
            for capability in TARGET_CAPABILITIES.values()
            if capability.primitive_profile is not None
        )


class _ProfileProjection(Mapping[str, TargetProfile]):
    def __getitem__(self, name: str) -> TargetProfile:
        return get_harness_registry().profiles[name]

    def __iter__(self):
        return iter(get_harness_registry().profiles)

    def __len__(self) -> int:
        return len(get_harness_registry().profiles)


RUNTIME_TO_CANONICAL_TARGET: Mapping[str, str] = _RuntimeProjection()
KNOWN_TARGETS: Mapping[str, TargetProfile] = _ProfileProjection()


def encode_external_target_locator(target: object, path: Path) -> str | None:
    """Encode an external path using profile metadata.

    Lightweight target stand-ins used by compatibility callers inherit the
    canonical profile's adapter while supplying their own managed root.
    """
    if isinstance(target, TargetProfile):
        return target.encode_external_locator(path)
    name = getattr(target, "name", None)
    profile = KNOWN_TARGETS.get(name) if isinstance(name, str) else None
    deploy_root = getattr(target, "managed_deploy_root", None)
    if (
        profile is None
        or profile.external_locator_encoder is None
        or not isinstance(deploy_root, Path)
    ):
        return None
    return profile.external_locator_encoder(path, deploy_root)


def target_lockfile_uri_schemes(target: object) -> tuple[str, ...]:
    """Return governed URI schemes from canonical target metadata."""
    if isinstance(target, TargetProfile):
        return target.lockfile_uri_schemes
    name = getattr(target, "name", None)
    profile = KNOWN_TARGETS.get(name) if isinstance(name, str) else None
    return profile.lockfile_uri_schemes if profile is not None else ()


def target_warns_unsupported_primitives(target: object) -> bool:
    """Return the unsupported-primitive warning capability."""
    if isinstance(target, TargetProfile):
        return target.warn_unsupported_primitives
    name = getattr(target, "name", None)
    profile = KNOWN_TARGETS.get(name) if isinstance(name, str) else None
    return bool(profile and profile.warn_unsupported_primitives)


def target_supports_primitive(target: object, primitive: str) -> bool:
    """Read primitive support from a concrete or lightweight profile."""
    primitives = getattr(target, "primitives", None)
    if isinstance(primitives, dict):
        return primitive in primitives
    name = getattr(target, "name", None)
    profile = KNOWN_TARGETS.get(name) if isinstance(name, str) else None
    return bool(profile and profile.supports(primitive))


def skills_root_prefixes() -> frozenset[str]:
    """Return every static project/user skills root (``<root>/<subdir>``, POSIX).

    Derived from :data:`KNOWN_TARGETS` via :attr:`TargetProfile.skills_rel_root`
    so callers recognising "a skill directory deployed by some target" never
    hard-code the ``skills`` directory name.  Dynamic-root targets are omitted
    (they use URI locators).
    """
    roots: set[str] = set()
    for profile in KNOWN_TARGETS.values():
        if not profile.supports("skills") or profile.user_root_resolver is not None:
            continue
        roots.add(profile.skills_rel_root)
        user_profile = profile.for_scope(user_scope=True)
        if user_profile is not None and user_profile.supports("skills"):
            roots.add(user_profile.skills_rel_root)
    return frozenset(roots)


def target_name_for_locator(locator: str) -> str | None:
    """Resolve a native target name from a registered locator URI."""
    for profile in KNOWN_TARGETS.values():
        if any(locator.startswith(scheme) for scheme in profile.lockfile_uri_schemes):
            return profile.name
    return None


def apply_legacy_skill_paths(profiles: list[TargetProfile]) -> list[TargetProfile]:
    """Reset ``deploy_root`` on every ``skills`` primitive to ``None``.

    When ``--legacy-skill-paths`` (or ``APM_LEGACY_SKILL_PATHS=1``) is
    active, this restores pre-convergence per-client routing so skills
    land in ``.github/skills/``, ``.cursor/skills/``, etc. instead of
    the default ``.agents/skills/``.

    Returns a NEW list of (possibly replaced) profiles — the global
    ``KNOWN_TARGETS`` dict is never mutated.
    """
    from dataclasses import replace

    result: list[TargetProfile] = []
    for profile in profiles:
        skills_pm = profile.primitives.get("skills")
        if skills_pm and skills_pm.deploy_root is not None:
            new_pm = PrimitiveMapping(
                subdir=skills_pm.subdir,
                extension=skills_pm.extension,
                format_id=skills_pm.format_id,
                deploy_root=None,
            )
            new_primitives = {**profile.primitives, "skills": new_pm}
            profile = replace(profile, primitives=new_primitives)
        result.append(profile)
    return result


def should_use_legacy_skill_paths() -> bool:
    """Return ``True`` when the ``APM_LEGACY_SKILL_PATHS`` env var is set.

    Recognised truthy values: ``1``, ``true``, ``yes`` (case-insensitive).
    """
    import os

    val = os.environ.get("APM_LEGACY_SKILL_PATHS", "").strip().lower()
    return val in ("1", "true", "yes")


def _resolve_copilot_cowork_root() -> Path | None:
    """Thin wrapper around ``copilot_cowork_paths.resolve_copilot_cowork_skills_dir()``.

    Used as the ``user_root_resolver`` callable for the cowork target.
    Exceptions propagate to the caller (``for_scope`` / install pipeline).
    """
    from apm_cli.integration.copilot_cowork_paths import resolve_copilot_cowork_skills_dir

    return resolve_copilot_cowork_skills_dir()


def _resolve_copilot_app_root() -> Path | None:
    """Thin wrapper around ``copilot_app_db.resolve_copilot_app_root()``.

    Used as the ``user_root_resolver`` callable for the ``copilot-app``
    target.  Returns ``~/.copilot/`` only when the app's SQLite DB is
    present, so the target is invisible on machines without the app
    installed.
    """
    from apm_cli.integration.copilot_app_db import resolve_copilot_app_root

    return resolve_copilot_app_root()


def _is_flag_enabled(flag_name: str, *, create_config: bool = True) -> bool:
    """Check whether an experimental flag is enabled.

    Lazy import to avoid config I/O at module load time.
    """
    from apm_cli.core.experimental import is_enabled

    return is_enabled(flag_name, create_config=create_config)


def resolve_hermes_root() -> Path:
    """Resolve the Hermes home directory.

    Honors ``$HERMES_HOME`` (default ``~/.hermes``).  Returns an expanded,
    normalized ``Path`` (``..`` segments collapsed via ``resolve``) so traversal
    in ``$HERMES_HOME`` cannot create unintended intermediate directories during
    ``mkdir(parents=True)``; the directory is not required to exist.  Mirrors the
    normalization in ``TargetProfile.for_scope``.  Used both by the user-scope
    skills deploy path and by ``HermesClientAdapter`` to locate ``config.yaml``
    for MCP writes.
    """
    import os
    from pathlib import Path

    env = os.environ.get("HERMES_HOME", "").strip()
    if env:
        return Path(env).expanduser().resolve(strict=False)
    return (Path.home() / ".hermes").resolve(strict=False)


def _flag_gated(profile: TargetProfile, *, create_config: bool = True) -> bool:
    """Return ``True`` if *profile* passes its flag gate (or has none)."""
    if profile.requires_flag is None:
        return True
    return _is_flag_enabled(profile.requires_flag, create_config=create_config)


def get_integration_prefixes(targets=None, *, user_scope: bool = False) -> tuple:
    """Return all known target root prefixes as a tuple.

    Used by ``BaseIntegrator.validate_deploy_path`` so the allow-list
    stays in sync with registered targets.

    When *targets* is provided, prefixes are derived from those
    (already scope-resolved) profiles.  Otherwise falls back to
    ``KNOWN_TARGETS`` for backward compatibility. When *user_scope* is
    true, that fallback also includes static ``user_root_dir`` prefixes and
    the profile root an environment variable in :data:`USER_ROOT_ENV_VARS`
    currently selects, so user-scope cleanup can reach the files it deployed.

    Includes prefixes from ``deploy_root`` overrides (e.g. ``.agents/``
    for Codex skills) so cross-root paths pass security validation.
    """
    source = targets if targets is not None else KNOWN_TARGETS.values()
    prefixes: list[str] = []
    seen: set[str] = set()
    if targets is None and user_scope:
        for profile in selected_user_profiles().values():
            if profile.prefix not in seen:
                seen.add(profile.prefix)
                prefixes.append(profile.prefix)
    for t in source:
        # Dynamic-root targets (cowork) use cowork:// prefix in lockfile.
        # Check the *capability* (user_root_resolver is not None) rather
        # than the *run-time state* (resolved_deploy_root is not None).
        # The static KNOWN_TARGETS registry always has resolved_deploy_root
        # = None (the resolver fires only on per-install copies created by
        # for_scope()), but cleanup code passes targets=None which falls
        # back to the static registry.  Using the capability flag ensures
        # cowork:// entries pass prefix validation during cleanup/uninstall.
        if t.user_root_resolver is not None:
            from apm_cli.integration.copilot_cowork_paths import COWORK_LOCKFILE_PREFIX

            if COWORK_LOCKFILE_PREFIX not in seen:
                seen.add(COWORK_LOCKFILE_PREFIX)
                prefixes.append(COWORK_LOCKFILE_PREFIX)
            continue
        if t.prefix not in seen:
            seen.add(t.prefix)
            prefixes.append(t.prefix)
        if targets is None and user_scope and t.user_root_dir is not None:
            user_prefix = f"{t.user_root_dir}/"
            if user_prefix not in seen:
                seen.add(user_prefix)
                prefixes.append(user_prefix)
        for m in t.primitives.values():
            if m.deploy_root is not None:
                dp = f"{m.deploy_root}/"
                if dp not in seen:
                    seen.add(dp)
                    prefixes.append(dp)
    return tuple(prefixes)


def selected_user_profiles() -> dict[str, TargetProfile]:
    """Return the user-scope profile each :data:`USER_ROOT_ENV_VARS` target selects.

    Without the variable the default root (``~/.claude``, ``~/.hermes``) is
    the selected profile.
    """
    profiles: dict[str, TargetProfile] = {}
    for name in USER_ROOT_ENV_VARS:
        base = KNOWN_TARGETS.get(name)
        scoped = base.for_scope(user_scope=True) if base is not None else None
        if scoped is not None:
            profiles[name] = scoped
    return profiles


def _signal_detected(profile: TargetProfile, root: Path) -> bool:
    adapter = get_harness_registry().adapters.get(profile.name)
    if adapter is None:
        return False
    return any(
        (root / relative_path).is_dir() if kind == "dir" else (root / relative_path).is_file()
        for kind, relative_path in adapter.signals
    )


def _profile_detected(profile: TargetProfile, root: Path, *, user_scope: bool) -> bool:
    if profile.name in get_harness_registry().external_names:
        if profile.capability.explicit_only:
            return False
        return _signal_detected(profile, root)
    if not profile.detect_by_dir:
        return False
    return (root / profile.effective_root(user_scope=user_scope)).is_dir()


def is_other_profile_path(profile: TargetProfile, value: str) -> bool:
    """Return True when *value* was deployed to another profile of *profile*.

    Profiles of a target listed in :data:`USER_ROOT_ENV_VARS` share one user
    lockfile, so its ``deployed_files`` can list paths under the roots of
    profiles other than the selected one.  Such a path is not stale for an
    install into the selected profile.  The check is a lexical prefix match
    against the selected root; it neither resolves nor follows symlinks.
    Separators are compared in POSIX form: an absolute root keeps native
    separators (``D:\\profiles\\b``) while lockfile values use ``/``.
    """
    if profile.name not in USER_ROOT_ENV_VARS or "://" in value:
        return False
    prefixes = tuple(
        prefix.replace("\\", "/") for prefix in get_integration_prefixes(targets=[profile])
    )
    return not value.replace("\\", "/").startswith(prefixes)


def active_targets_user_scope(
    explicit_target: str | list[str] | None = None,
    *,
    create_config: bool = True,
) -> list:
    """Return ``TargetProfile`` instances for user-scope deployment.

    Mirrors ``active_targets()`` but operates against ``~/`` and filters
    out targets that do not support user scope.

    Resolution order:

    1. **Explicit target** (``--target``): returns the matching profile(s)
       that support user scope.  ``"all"`` returns every user-capable
       target.  Validity is enforced upstream by
       :func:`apm_cli.core.target_detection.parse_target_field`; this
       function does not silently fall back when given unknown tokens.
    2. **Directory detection**: profiles whose ``effective_root(user_scope=True)``
       directory exists under ``~/``.
    3. **Fallback**: ``[copilot]`` -- same default as project scope.
    """
    from pathlib import Path

    home = Path.home()

    # --- explicit target ---
    if explicit_target:
        # See module docstring on the parse_target_field gate-keeping contract.
        raw = [explicit_target] if isinstance(explicit_target, str) else list(explicit_target)
        profiles: list = []
        seen: set = set()
        for t in raw:
            try:
                canonical = normalize_target_name(t)
            except KeyError:
                continue
            if canonical == "all":
                all_targets = {normalize_target_name(target) for target in expand_all("install")}
                all_targets.update(
                    capability.name
                    for capability in TARGET_CAPABILITIES.values()
                    if capability.experimental_flag is not None
                )
                return [
                    p
                    for p in KNOWN_TARGETS.values()
                    if p.name in all_targets
                    and not p.capability.explicit_only
                    and p.user_supported
                    and _flag_gated(p, create_config=create_config)
                ]
            profile = KNOWN_TARGETS.get(canonical)
            if (
                profile
                and profile.user_supported
                and _flag_gated(profile, create_config=create_config)
                and profile.name not in seen
            ):
                seen.add(profile.name)
                profiles.append(profile)
        return profiles

    # --- auto-detect by directory presence at ~/ ---
    # Targets with detect_by_dir=False (cowork) are never auto-detected.
    detected = [
        p
        for p in KNOWN_TARGETS.values()
        if p.user_supported
        and _flag_gated(p, create_config=create_config)
        and _profile_detected(p, home, user_scope=True)
    ]
    if detected:
        return detected

    # --- fallback: copilot is the universal default ---
    return [KNOWN_TARGETS["copilot"]]


def active_targets(
    project_root,
    explicit_target: str | list[str] | None = None,
    *,
    create_config: bool = True,
) -> list:
    """Return the list of ``TargetProfile`` instances that should be
    deployed into *project_root*.

    Resolution order:

    1. **Explicit target** (``--target`` flag or ``apm.yml target:``):
       returns the matching profile(s).  ``"all"`` returns every known
       target.  Validity is enforced upstream by
       :func:`apm_cli.core.target_detection.parse_target_field`; unknown
       tokens never reach here, so this branch never silently falls back
       to ``[copilot]``.
    2. **Directory detection**: profiles whose ``root_dir`` already
       exists under *project_root*.
    3. **Fallback**: when nothing is detected, returns ``[copilot]``
       so greenfield projects get a default skills root.

    Args:
        project_root: The workspace root ``Path``.
        explicit_target: Canonical target name, list of canonical names,
            or ``"all"``/``None``.  ``None`` means auto-detect.
    """
    from pathlib import Path

    root = Path(project_root)

    # --- explicit target ---
    if explicit_target:
        # See module docstring on the parse_target_field gate-keeping contract.
        raw = [explicit_target] if isinstance(explicit_target, str) else list(explicit_target)
        profiles: list = []
        seen: set = set()
        for t in raw:
            try:
                canonical = normalize_target_name(t)
            except KeyError:
                continue
            if canonical == "all":
                # Exclude explicit-only targets (agent-skills) -- they must
                # be requested individually.
                # Exclude experimental targets (copilot-cowork) -- they must
                # be opted into explicitly via `--target copilot-cowork`,
                # matching the documented contract on EXPERIMENTAL_TARGETS in
                # core/target_detection.py. Including cowork in `all` for
                # project scope hits the unconditional project-scope gate in
                # phases/targets.py and aborts the entire install (#1185 b).
                all_targets = {normalize_target_name(target) for target in expand_all("install")}
                return [p for p in KNOWN_TARGETS.values() if p.name in all_targets]
            profile = KNOWN_TARGETS.get(canonical)
            if (
                profile
                and _flag_gated(profile, create_config=create_config)
                and profile.name not in seen
            ):
                seen.add(profile.name)
                profiles.append(profile)
        return profiles

    # --- auto-detect by directory presence ---
    # Targets with detect_by_dir=False (cowork) are never auto-detected.
    detected = [
        p
        for p in KNOWN_TARGETS.values()
        if _flag_gated(p, create_config=create_config)
        and _profile_detected(p, root, user_scope=False)
    ]
    if detected:
        return detected

    # --- fallback: copilot is the universal default ---
    return [KNOWN_TARGETS["copilot"]]


def _validate_project_target_root(project_root: Path, profile: TargetProfile) -> Path:
    """Return a contained project target root without following symlinks."""
    deploy_path = profile.deploy_path(project_root)
    if profile.resolved_deploy_root is not None:
        return deploy_path
    if has_symlink_component(project_root, deploy_path):
        raise PathTraversalError(
            f"Refusing deployment through symlinked target root: {profile.root_dir}"
        )
    return ensure_path_within(deploy_path, project_root)


def resolve_targets(
    project_root,
    user_scope: bool = False,
    explicit_target: str | list[str] | None = None,
    *,
    create_config: bool = True,
) -> list:
    """Return scope-resolved ``TargetProfile`` instances.

    This is the **single entry point** for obtaining deployment targets.
    It combines target detection (or explicit selection), scope resolution
    (``for_scope``), and primitive filtering into one call.

    Callers receive profiles where ``root_dir`` is already correct for
    the requested scope -- no ``effective_root()`` calls needed.

    Args:
        project_root: Workspace root (``Path.cwd()`` or ``Path.home()``).
        user_scope: When ``True``, resolve for user-level deployment.
        explicit_target: Canonical target name, list of canonical names,
            or ``"all"``.  ``None`` means auto-detect.
    """
    if user_scope:
        raw = active_targets_user_scope(explicit_target, create_config=create_config)
    else:
        raw = active_targets(project_root, explicit_target, create_config=create_config)

    resolved = []
    for t in raw:
        scoped = t.for_scope(user_scope=user_scope)
        if scoped is not None:
            if not user_scope:
                _validate_project_target_root(Path(project_root), scoped)
            resolved.append(scoped)
    return resolved


def materialize_project_target_profiles(
    project_root: Path,
    target_names: list[str] | tuple[str, ...],
) -> list[TargetProfile]:
    """Create project target roots and return their canonical profiles."""
    profiles: list[TargetProfile] = []
    for target_name in target_names:
        profile = KNOWN_TARGETS.get(target_name)
        if profile is None:
            continue
        deploy_path = _validate_project_target_root(project_root, profile)
        # Skills-only roots are created by the skill integrator on first write,
        # so an install that deploys nothing leaves no empty directory behind.
        if not deploy_path.is_dir() and not profile.skills_only:
            deploy_path.mkdir(parents=True, exist_ok=True)
        profiles.append(profile)
    return profiles


class AuditTargetDisabledError(ClickException):
    """Report an unavailable audit target as a configuration error."""

    exit_code = 3


def resolve_audit_targets(
    project_root: Path,
    *,
    user_scope: bool = False,
    lockfile: LockFile | None = None,
) -> list[TargetProfile]:
    """Resolve read-only audit profiles through target and deployment owners."""
    from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
    from apm_cli.core.scope import get_workspace_deploy_root
    from apm_cli.core.target_detection import (
        read_manifest_target,
        resolve_audit_target_decision,
    )
    from apm_cli.deps.lockfile import LockFile, get_lockfile_path

    lock = lockfile if lockfile is not None else LockFile.read(get_lockfile_path(project_root))
    recorded = (
        DeploymentLedgerCodec.recorded_target_names(
            lock,
            scope="user" if user_scope else "project",
            supported_targets=KNOWN_TARGETS,
        )
        if lock is not None
        else ()
    )
    decision = resolve_audit_target_decision(
        manifest_target=read_manifest_target(project_root),
        recorded_targets=recorded,
    )
    for name in decision.canonical_targets or ():
        profile = KNOWN_TARGETS.get(name)
        if (
            profile is not None
            and profile.requires_flag is not None
            and not _flag_gated(profile, create_config=False)
        ):
            flag = profile.requires_flag.replace("_", "-")
            raise AuditTargetDisabledError(
                f"Cannot audit target '{name}': experimental feature "
                f"'{flag}' is disabled. Run "
                f"'apm experimental enable {flag}' to audit this installation."
            )
    return resolve_targets(
        get_workspace_deploy_root(project_root),
        user_scope=user_scope,
        explicit_target=decision.value,
        create_config=False,
    )
