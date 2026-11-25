"""Authorized package decisions shared by preflight and native deployment."""

from __future__ import annotations

import posixpath
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from apm_cli.core.scope import is_user_scope
from apm_cli.utils.path_security import (
    PathTraversalError,
    ensure_path_within,
    has_symlink_component,
    validate_path_segments,
)
from apm_cli.utils.paths import SpellingAliasIndex

from .exec_gate import check_executable_approval, plugin_bin_deployable, resolve_bin_skip
from .plan_admission import _is_transitive
from .target_filter import log_package_target_restriction, resolve_effective_package_targets

if TYPE_CHECKING:
    from apm_cli.claude_plugins.admission import ClaudeNativePlugin
    from apm_cli.codex_plugins.admission import CodexNativePlugin
    from apm_cli.core.command_logger import InstallLogger
    from apm_cli.core.scope import InstallScope
    from apm_cli.install.context import InstallContext
    from apm_cli.install.deployable_source_plan import DeployableSourcePlan
    from apm_cli.install.target_filter import EffectivePackageTargets
    from apm_cli.integration.targets import TargetProfile
    from apm_cli.utils.diagnostics import DiagnosticCollector


# Primitive kinds whose loose files run nothing; a parked plan may keep them.
_INERT_PRIMITIVES = ("skills", "instructions", "agents", "commands", "prompts")
# Plugin binaries and their activation descriptor deploy inside a skill dir;
# they are executable members, never kept as inert files.
_EXECUTABLE_SEGMENTS = frozenset({"bin", ".claude-plugin"})


@dataclass(frozen=True)
class PackageDeploymentPlan:
    """Authorized package selection shared by batch preflight and deployment."""

    target_selection: EffectivePackageTargets
    approvals: tuple[bool, bool, bool, bool, bool] = (False, False, False, False, False)
    skip_bin: bool = False
    bin_skip_reason: str | None = None
    codex_native: CodexNativePlugin | None = None
    source_plan: DeployableSourcePlan | None = None
    claude_native: ClaudeNativePlugin | None = None

    @property
    def projection_targets(self) -> list[TargetProfile]:
        """Keep native bundles out of their unauthorized loose projections."""
        if self.codex_native is not None:
            return []
        targets = list(self.target_selection.targets)
        native_target_names = self.claude_native.harnesses if self.claude_native else set()
        return [target for target in targets if target.name not in native_target_names]

    def kept_loose_values(
        self,
        prior_values: Iterable[str],
        *,
        user_scope: bool,
        live_targets: Mapping[str, TargetProfile] | None = None,
    ) -> tuple[str, ...]:
        """Prior loose values a parked native Claude plan keeps in place (#5036).

        The native route takes its harnesses out of the loose projection. When
        the native plugin is parked nothing replaces the loose files there, so
        they stay installed (and keep their lock claims) instead of being
        cleaned as stale. Nothing new is deployed loose for those harnesses.
        Claims are matched in the layout they were recorded in (#5092):
        ``live_targets`` names the profiles install deployed through when the
        plan was built for scratch-contained replay targets, and a harness
        whose recorded skills sit under its per-client root keeps that root.
        """
        native = self.claude_native
        if self.codex_native is not None or native is None or not native.parked:
            return ()
        from apm_cli.integration.skill_layout import resolve_skill_layout

        prior = tuple(prior_values)
        targets = [
            (live_targets or {}).get(target.name, target)
            for target in self.target_selection.targets
        ]
        kept = _inert_prefixes(
            resolve_skill_layout(
                (t for t in targets if t.name in native.harnesses), recorded_paths=prior
            )
        )
        # A root a loose target still serves (Codex shares .agents/skills with
        # Copilot) is rewritten by this install, so it is not kept as it was.
        loose = _inert_prefixes(t for t in targets if t.name not in native.harnesses)
        prefixes = tuple(
            p for p in kept if not any(p.startswith(o) or o.startswith(p) for o in loose)
        )
        return tuple(
            value
            for value in prior
            if prefixes
            and value.startswith(prefixes)
            and _EXECUTABLE_SEGMENTS.isdisjoint(value.split("/"))
        )


def _inert_prefixes(targets: Iterable[TargetProfile]) -> tuple[str, ...]:
    """Root-relative directory prefixes of the inert primitives a target deploys."""
    prefixes: list[str] = []
    for target in targets:
        for kind in _INERT_PRIMITIVES:
            mapping = target.primitives.get(kind)
            if mapping is not None:
                base = (
                    f"{mapping.deploy_root.rstrip('/')}/" if mapping.deploy_root else target.prefix
                )
                # Claims are POSIX: a Windows root is spelled with "/", and a
                # root of "." (CLAUDE_CONFIG_DIR=$HOME) records "skills/...".
                prefix = f"{base}{mapping.subdir}".replace("\\", "/")
                prefixes.append(f"{posixpath.normpath(prefix)}/")
    return tuple(prefixes)


def parked_kept_values_after_writes(ctx: InstallContext) -> frozenset[str]:
    """Parked kept values no package wrote in this install (#5108).

    A path another package wrote takes that writer's claim and new hash, so
    the parked package's previous claim on it is not kept. A spelling the
    volume proves is the same entry (case or Unicode normalization, #5150)
    counts as written; a case-sensitive volume keeps both claims.
    """
    kept = getattr(ctx, "parked_native_kept_values", frozenset())
    written = {path for paths in ctx.package_deployed_files.values() for path in paths}
    remaining = kept - written
    if not remaining:
        return frozenset()
    spellings = SpellingAliasIndex(written)
    root = Path(ctx.project_root)
    return frozenset(value for value in remaining if spellings.alias(value, root) is None)


def keep_loose_for_parked_native(
    plan: PackageDeploymentPlan,
    ctx: InstallContext | None,
    package_name: str,
    *,
    diagnostics: DiagnosticCollector | None,
    logger: InstallLogger | None,
) -> tuple[str, ...]:
    """Keep and name a package's loose files a parked native replacement leaves usable (#5036)."""
    if plan.claude_native is None or not plan.claude_native.parked:
        return ()
    lockfile = getattr(ctx, "existing_lockfile", None)
    prior = lockfile.get_dependency(package_name) if lockfile is not None else None
    if ctx is None or prior is None:
        return ()
    user_scope = is_user_scope(getattr(ctx, "scope", None))
    kept = plan.kept_loose_values(prior.deployed_files, user_scope=user_scope)
    if not kept:
        return ()
    ctx.parked_native_kept_values = ctx.parked_native_kept_values | frozenset(kept)
    from apm_cli.core.remediation import kept_note

    roots = sorted(
        value
        for value in kept
        if not any(value.startswith(f"{other}/") for other in kept if other != value)
    )
    shown = ", ".join(
        f"{'~/' if user_scope and not Path(value).is_absolute() else ''}{value}" for value in roots
    )
    message = kept_note(
        shown,
        f"the native plugin that replaces it was parked, so the previous install of "
        f"{package_name} stays as it was and is not updated",
        "Resolve the parked plugin as its warning says; the next install then replaces it.",
    )
    if diagnostics is not None:
        diagnostics.warn(message)
    elif logger is not None:
        logger.warning(message)
    return kept


def _native_skill_selection(
    skill_subset: tuple | None, skill_pairing: Any, targets: Any
) -> tuple | None:
    """The skill selection a whole-plugin load must honor.

    A per-target pairing (req-rs-018) can restrict one target while the flat
    subset is ``None`` (all); a native load cannot honor that, so any target
    restriction counts as a selection.
    """
    if skill_pairing is None:
        return skill_subset
    restricted = {
        skill
        for target in targets
        if skill_pairing.selects_target(target.name)
        for skill in skill_pairing.skills_for_target(target.name) or ()
    }
    return tuple(sorted(restricted)) or skill_subset


def _warn_parked_plugin(package_name, package_info, allow_executables, diagnostics, logger, ctx):
    """Name why the trust owner parked a native plugin, with the next step.

    A plugin that is merely not approved yet is reported by the registrar's
    own refusal, so only changed, denied, transitive, APM-owned-path and
    incomplete-grant parking is named here.
    """
    from .exec_gate import plugin_admission, resolve_package_key

    verdict = plugin_admission(package_name, package_info, allow_executables, ctx=ctx)
    if (
        verdict is None
        or verdict.allowed
        or (verdict.reason == "unapproved-executables" and not verdict.detail)
    ):
        return verdict
    from apm_cli.security.executables import _strip_version
    from apm_cli.security.trust import parked_plugin_message

    package = _strip_version(resolve_package_key(package_info, package_name))
    message = parked_plugin_message(
        package,
        str(verdict.reason),
        verdict.detail,
        user_scope=is_user_scope(getattr(ctx, "scope", None)),
    )
    if diagnostics is not None:
        diagnostics.warn(message)
    elif logger is not None:
        logger.warning(message)
    return verdict


def prepare_package_deployment(  # noqa: PLR0913
    package_info: Any,
    project_root: Path,
    *,
    targets: Any,
    diagnostics: DiagnosticCollector | None,
    package_name: str = "",
    logger: InstallLogger | None = None,
    scope: InstallScope | None = None,
    skill_subset: tuple | None,
    skill_pairing: Any = None,
    ctx: InstallContext | None = None,
    policy: Any = None,
    is_first_party: bool = False,
    allow_executables: dict[str, dict[str, bool]] | None = None,
    dep_target_subset: list[str] | None = None,
    trust_bin: bool | None = None,
    bin_skip_reason_override: str | None = None,
    read_only: bool = False,
    native_claude_user_scope: bool = False,
) -> PackageDeploymentPlan:
    """Resolve deployment authorization without writing native target content."""
    import sys

    from apm_cli.claude_plugins.admission import plan_native_claude_plugin
    from apm_cli.codex_plugins.admission import plan_native_codex_plugin
    from apm_cli.core.scope import InstallScope
    from apm_cli.install.deployable_source_plan import DeployableSourcePlan

    selection = resolve_effective_package_targets(
        targets, dep_target_subset, package_info, diagnostics, package_name
    )
    log_package_target_restriction(logger, selection, package_name)
    if not selection.targets:
        return PackageDeploymentPlan(selection)
    native_subset = _native_skill_selection(skill_subset, skill_pairing, selection.targets)
    approvals = check_executable_approval(package_name, package_info, allow_executables, ctx=ctx)
    hooks, bin_approved, mcp, canvas, lsp = approvals
    from .exec_gate import bin_deploy_denial, hook_file_admission, native_plugin_verdict

    hook_filter = hook_file_admission(package_name, package_info, allow_executables)
    verdict = _warn_parked_plugin(
        package_name, package_info, allow_executables, diagnostics, logger, ctx
    )
    # The trust owner already named a deny, change or missing grant (#4154).
    parked_reported = bool(
        verdict is not None
        and not verdict.allowed
        and (verdict.reason != "unapproved-executables" or verdict.detail)
    )
    skip_bin, reason = resolve_bin_skip(
        bin_approved, trust_bin, non_interactive=not sys.stdout.isatty()
    )
    if skip_bin and bin_skip_reason_override is not None:
        reason = bin_skip_reason_override
    # The org policy the install gate fetched; callers that pass none
    # (preflight) defer to it, as effective_allow_map does for trust.
    org_policy = (
        policy
        if policy is not None
        else getattr(getattr(ctx, "policy_fetch", None), "policy", None)
    )
    transitive = _is_transitive(package_info, ctx)
    codex_native = plan_native_codex_plugin(
        package_info,
        package_name,
        list(selection.targets),
        scope=scope,
        skill_subset=native_subset,
        approvals={
            # A native plugin loads every hook file live: per-file pins never
            # admit it; a whole-plugin pin (or a type-level grant) does.
            "hooks": hooks,
            "mcp": mcp,
            "bin": bin_approved and not skip_bin,
            "canvas": canvas,
            "lsp": lsp,
        },
        ctx=ctx,
        read_only=read_only,
        parked_reported=parked_reported,
        policy=org_policy,
        transitive=transitive,
    )
    # The project's own content is never deployed as one of its native plugins.
    native_claude_plugin = (
        None
        if codex_native is not None or is_first_party
        else plan_native_claude_plugin(
            package_info,
            package_name=package_name,
            targets=list(selection.targets),
            scope=InstallScope.USER if native_claude_user_scope else scope,
            skill_subset=native_subset,
            admit=lambda: native_plugin_verdict(
                package_name, package_info, allow_executables, ctx=ctx
            ),
            # The whole-plugin verdict approves bin/; only the run's posture applies.
            skip_bin=resolve_bin_skip(True, trust_bin, non_interactive=not sys.stdout.isatty())[0],
            policy=org_policy,
            bin_denied=bin_deploy_denial(package_info, org_policy),
            transitive=transitive,
        )
    )
    projection_targets = PackageDeploymentPlan(
        selection, codex_native=codex_native, claude_native=native_claude_plugin
    ).projection_targets
    # A parked plugin deploys nothing natively; only its loose targets are planned.
    native_plugin = codex_native or (
        native_claude_plugin
        if native_claude_plugin is not None and not native_claude_plugin.parked
        else None
    )
    source_plan = DeployableSourcePlan.create(
        package_info,
        projection_targets,
        skill_subset=skill_subset,
        hooks_approved=hooks,
        hook_file_admitted=hook_filter,
        canvas_approved=canvas or is_first_party,
        skip_bin=skip_bin,
        diagnostics=diagnostics,
        package_name=package_name,
        native_plugin_root=native_plugin.source_root if native_plugin else None,
        plugin_bin_deployable=plugin_bin_deployable(
            package_info,
            projection_targets,
            project_root=project_root,
            scope=scope,
            policy=policy,
            skip_bin=skip_bin,
        ),
    )
    if hook_filter is not None and not hooks:
        # No pinned hook file was admitted, so the source plan never asks the
        # filter: name the parked files here. A deny is named by the skip line.
        hook_filter.admits_any()
    if hook_filter is not None and hook_filter.parked:
        from apm_cli.security.executables import _strip_version
        from apm_cli.security.trust import REASON_DENIED, parked_hook_message

        from .exec_gate import resolve_package_key

        package = _strip_version(resolve_package_key(package_info, package_name))
        for item, parked_reason in hook_filter.parked:
            if not hooks and parked_reason == REASON_DENIED:
                continue
            message = parked_hook_message(
                package, item, parked_reason, user_scope=is_user_scope(scope)
            )
            if diagnostics is not None:
                diagnostics.warn(message)
            elif logger is not None:
                logger.warning(message)
    return PackageDeploymentPlan(
        selection, approvals, skip_bin, reason, codex_native, source_plan, native_claude_plugin
    )


def carry_kept_loose_into_replay(
    result: dict,
    lock_dep: Any,
    deploy_root: Path,
    scratch_root: Path,
    user_scope: bool,
    live_targets: Iterable[TargetProfile] = (),
) -> None:
    """Carry the loose files a parked native replacement keeps into a drift replay (#5036).

    Install keeps them as they were; the replay renders no loose copy for a
    natively routed harness, so their live bytes stand in for it. Their lock
    hashes still bind them through content-integrity. Claims are selected
    through the live target profiles, and a claim under an external root
    (``CLAUDE_CONFIG_DIR`` outside HOME) lands in that root's replay
    projection (#5092).
    """
    import shutil

    plan = result.get("deployment_plan")
    if not isinstance(plan, PackageDeploymentPlan):
        return
    live = {target.name: target for target in live_targets}
    hashed = getattr(lock_dep, "deployed_file_hashes", None) or {}
    kept = plan.kept_loose_values(lock_dep.deployed_files, user_scope=user_scope, live_targets=live)
    # Only the parked harnesses selected these claims, so only they frame them (#5162).
    harnesses = plan.claude_native.harnesses if plan.claude_native is not None else frozenset()
    framing = [target for name, target in live.items() if name in harnesses]
    for value in kept:
        if value not in hashed:
            continue  # only bytes the lock binds by hash stand in for the replay
        try:
            live_root, replay_root, relative = _replay_frame(
                value, framing, deploy_root, scratch_root
            )
            validate_path_segments(relative, context="kept lock claim", reject_empty=True)
            live_file = ensure_path_within(live_root / relative, live_root)
            # Contain the copy at scratch_root: a symlinked replay root never redirects it.
            replayed = ensure_path_within(replay_root / relative, scratch_root)
        except PathTraversalError:
            continue  # a claim outside its root is never carried; drift reports it
        if has_symlink_component(live_root, live_root / relative) or not live_file.is_file():
            continue
        if replayed.exists() or replayed.is_symlink():
            continue  # another package rendered this path; its copy stands (#5108)
        replayed.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(live_file, replayed)


def _replay_frame(
    value: str, live_targets: Iterable[TargetProfile], deploy_root: Path, scratch_root: Path
) -> tuple[Path, Path, str]:
    """Return ``(live root, replay root, relative path)`` for one kept lock claim."""
    from apm_cli.install.audit_target_roots import external_replay_root

    if Path(value).is_absolute():
        # Among the harnesses that selected the claim, the deepest enclosing
        # root frames it, whatever the target order.
        frames = []
        for target in live_targets:
            root = target.managed_deploy_root
            if root is None:
                continue
            try:
                relative = Path(value).relative_to(root)
            except ValueError:
                continue
            frames.append((len(root.parts), root, target, relative))
        if not frames:
            raise PathTraversalError(f"kept lock claim {value!r} is outside every target root")
        _depth, root, target, relative = max(frames, key=lambda frame: frame[0])
        return root, external_replay_root(scratch_root, target), relative.as_posix()
    return deploy_root, scratch_root, value


def finish_replayed_package(
    result: dict, lock_dep: Any, frame: tuple, config: Any, live_targets: Iterable[TargetProfile]
) -> None:
    """Carry kept loose files into the replay, then report the package's output (#5048).

    *frame* is ``(package key, deploy root, scratch root)``; ``config.on_package``
    reads each package's own output before a later package overwrites it.
    """
    dep_key, deploy_root, scratch_root = frame
    carry_kept_loose_into_replay(
        result, lock_dep, deploy_root, scratch_root, config.user_scope, live_targets
    )
    if config.on_package is not None:
        config.on_package(dep_key, list(result.get("deployed_files") or ()), scratch_root)
