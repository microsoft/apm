"""Package integration services.

The two functions in this module own the *integration template* for a single
package -- looping over the resolved targets, dispatching primitives to their
integrators, accumulating counters, and recording deployed file paths.

Moved here from ``apm_cli.commands.install`` so that the install engine
package owns its own integration logic.  ``commands/install`` keeps thin
underscore-prefixed re-exports for backward compatibility with existing
``@patch`` sites and direct imports.

Design notes
------------
``integrate_local_content()`` calls ``integrate_package_primitives()`` via a
bare-name lookup so that ``@patch`` of either symbol on this module's
namespace intercepts both call paths consistently.
"""

from __future__ import annotations

import builtins
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..core.command_logger import InstallLogger
    from ..core.scope import InstallScope
    from ..install.context import InstallContext
    from ..integration.base_integrator import BaseIntegrator
    from ..utils.diagnostics import DiagnosticCollector

from ._services_helpers import (
    _apply_canvas_gate,
    _check_executable_approval,
    _label_and_deploy_dir,
    _load_bundle_pack_files,
    _log_canvas_skip,
    _log_hooks_skip,
    _resolve_bundle_file_dest,
)
from .deployed_paths import deployed_path_entry as _deployed_path_entry
from .deployed_paths import (
    skill_bundle_file_entries as _skill_bundle_file_entries,
)
from .services_integrate import (
    _format_target_collapse,
    _log_per_kind_results,
)
from .services_integrate import _log_hook_display_payloads as _log_hook_display_payloads
from .target_filter import filter_targets_for_dependency

# CRITICAL: Shadow Python builtins that share names with Click commands so
# ``set()`` / ``list()`` / ``dict()`` resolve to the builtins, not Click
# subcommand objects.  ``commands/install`` and ``install/pipeline`` do the
# same dance for the same reason.
set = builtins.set
list = builtins.list
dict = builtins.dict


@dataclass(frozen=True)
class IntegratorBundle:
    """Groups the six primitive integrators passed to ``integrate_package_primitives``.

    Using a bundle reduces the public argument count of
    ``integrate_package_primitives`` below the PLR0913 threshold while
    keeping the integrator objects strongly typed and discoverable.
    """

    prompt: BaseIntegrator
    agent: BaseIntegrator
    skill: BaseIntegrator
    instruction: BaseIntegrator
    command: BaseIntegrator
    hook: BaseIntegrator
    # Optional so the ~16 existing test/prod construction sites that omit it
    # keep working. Production sites (template.py, integrate_local_content,
    # drift.py) pass a real CanvasIntegrator; when None the loop skips canvas.
    canvas: BaseIntegrator | None = None

    @classmethod
    def from_mapping(cls, integrators: dict[str, BaseIntegrator]) -> IntegratorBundle:
        """Build a bundle from a ``ctx.integrators`` mapping.

        Centralises the six required integrators plus the optional canvas
        so call sites (template.py, phases/integrate.py, drift.py) share a
        single construction path instead of duplicating the kwargs block.
        The ``canvas`` key is optional; when absent the field defaults to
        ``None`` and the integration loop skips canvas deployment.
        """
        return cls(
            prompt=integrators["prompt"],
            agent=integrators["agent"],
            skill=integrators["skill"],
            instruction=integrators["instruction"],
            command=integrators["command"],
            hook=integrators["hook"],
            canvas=integrators.get("canvas"),
        )


@dataclass(frozen=True)
class IntegrationOptions:
    """Optional configuration knobs for ``integrate_package_primitives``.

    Grouping these advanced/optional parameters into a single object reduces
    the public argument count of ``integrate_package_primitives`` without
    losing expressiveness at call sites.

    Attributes
    ----------
    skill_subset:
        When set, only skills whose names appear in this tuple are deployed.
    scratch_root:
        When set, the caller is replaying integration into an isolated
        directory (drift-replay).  Must be a parent of *project_root*.
    policy:
        Enterprise security policy object forwarded to the skill integrator.
    """

    skill_subset: tuple | None = None
    scratch_root: Path | None = None
    policy: Any = field(default=None, compare=False)


def _log_skill_result(
    skill_result: Any,
    result: dict,
    project_root: Path,
    targets: Any,
    verbose: bool,
    logger: Any,
    *,
    package_name: str = "",
    package_info: Any = None,
    log_integration: Any | None = None,
) -> None:
    """Process skill integration result: update counters and emit log lines.

    Lives in services.py (not services_integrate.py) so that the module-level
    ``_deployed_path_entry`` alias is used directly -- preserving the
    ``@patch("apm_cli.install.services._deployed_path_entry")`` test seam.
    """
    log_fn = log_integration
    if log_fn is None and logger is not None:
        log_fn = logger.tree_item

    _skill_target_dirs: builtins.set = builtins.set()
    for tp in skill_result.target_paths:
        try:
            rel = tp.relative_to(project_root)
            if rel.parts:
                _skill_target_dirs.add(rel.parts[0])
        except ValueError:
            try:
                from apm_cli.integration.targets import target_name_for_locator

                owner = next(
                    (
                        target
                        for target in targets
                        if target.managed_deploy_root is not None
                        and tp.is_relative_to(target.managed_deploy_root)
                    ),
                    None,
                )
                locator_name = target_name_for_locator(
                    _deployed_path_entry(tp, project_root, targets)
                )
                _skill_target_dirs.add(
                    owner.name
                    if owner is not None
                    else locator_name
                    if locator_name is not None
                    else "external"
                )
            except (ImportError, AttributeError):
                _skill_target_dirs.add("copilot-cowork")
    _skill_target_paths = [f"{d}/skills/" for d in sorted(_skill_target_dirs)]
    if not _skill_target_paths:
        _skill_target_paths = ["skills/"]
    _skill_suffix, _skill_expansion = _format_target_collapse(_skill_target_paths, verbose)

    if skill_result.skill_created:
        result["skills"] += 1
        if logger:
            if _skill_expansion:
                logger.tree_item("  |-- Skill integrated:")
                for line in _skill_expansion:
                    logger.tree_item(line)
            else:
                logger.tree_item(f"  |-- Skill integrated -> {_skill_suffix}")

    if skill_result.sub_skills_promoted > 0:
        result["sub_skills"] += skill_result.sub_skills_promoted
        if logger:
            if _skill_expansion:
                logger.tree_item(f"  |-- {skill_result.sub_skills_promoted} skill(s) integrated:")
                for line in _skill_expansion:
                    logger.tree_item(line)
            else:
                logger.tree_item(
                    f"  |-- {skill_result.sub_skills_promoted} skill(s) integrated"
                    f" -> {_skill_suffix}"
                )

    if (skill_result.bin_deployed > 0 or skill_result.bin_skipped_reason) and log_fn is not None:
        from apm_cli.install.exec_gate import log_bin_status

        log_bin_status(skill_result, _skill_suffix, package_name, package_info, log_fn)

    for tp in skill_result.target_paths:
        result["deployed_files"].append(_deployed_path_entry(tp, project_root, targets))
        # #1716: also record the bundle's contained files so per-file
        # content hashes cover SKILL.md / assets / scripts. The directory
        # entry above is retained (cleanup's directory-rejection gate and
        # the manifest dir-exclusion contract depend on it); the file
        # entries give ``content-integrity`` its per-file coverage so skill
        # drift is caught under ``apm audit --ci --no-drift``.
        result["deployed_files"].extend(_skill_bundle_file_entries(tp, project_root, targets))


def integrate_package_primitives(  # noqa: PLR0913
    package_info: Any,
    project_root: Path,
    *,
    targets: Any,
    integrators: IntegratorBundle,
    force: bool,
    managed_files: Any,
    diagnostics: DiagnosticCollector,
    package_name: str = "",
    logger: InstallLogger | None = None,
    scope: InstallScope | None = None,
    ctx: InstallContext | None = None,
    options: IntegrationOptions | None = None,
    is_first_party: bool = False,
    allow_executables: builtins.dict[str, builtins.dict[str, bool]] | None = None,
    dep_target_subset: builtins.list[str] | None = None,
) -> dict:
    """Run the full integration pipeline for a single package.

    Iterates over *targets* (``TargetProfile`` list) and dispatches each
    primitive to the appropriate integrator via the target-driven API.
    Skills are handled separately because ``SkillIntegrator`` already
    routes across all targets internally.

    When *scope* is ``InstallScope.USER``, targets and primitives that
    do not support user-scope deployment are silently skipped.

    When *ctx* is provided, the cowork non-skill primitive warning
    (Amendment 6) is emitted once per install run for packages that
    contain non-skill primitives when the cowork target is active.

    Advanced options (skill filtering, drift-replay scratch root, policy)
    are grouped in the optional *options* parameter.

    When *allow_executables* is provided, executable primitives (hooks,
    bin/) are only deployed for packages whose key appears in the dict
    with the matching type set to ``True``.  Local project content
    (``package_name == "_local"``) is always trusted.

    Returns a dict with integration counters and the list of deployed file paths.
    """
    from apm_cli.integration.dispatch import get_dispatch_table

    from ..core.scope import InstallScope

    _opts = options or IntegrationOptions()
    skill_subset = _opts.skill_subset
    scratch_root = _opts.scratch_root
    policy = _opts.policy

    _dispatch = get_dispatch_table()
    result = {
        "prompts": 0,
        "agents": 0,
        "skills": 0,
        "sub_skills": 0,
        "instructions": 0,
        "commands": 0,
        "hooks": 0,
        "canvases": 0,
        "links_resolved": 0,
        "deployed_files": [],
    }

    deployed = result["deployed_files"]

    # SECURITY: dep_target_subset comes from CONSUMER manifest only.
    # Package-side targets are advisory metadata; never a routing input.
    targets, allowed_dep_targets, dep_targets_active = filter_targets_for_dependency(
        targets,
        dep_target_subset,
        diagnostics,
        package_name,
    )
    if not targets:
        return result

    # ------------------------------------------------------------------
    # Drift-replay safety guard (#drift): when ``scratch_root`` is set,
    # the caller is replaying integration into an isolated directory.
    # We assert it exists and is NOT inside ``project_root`` to keep the
    # read-only contract of ``apm audit --check drift`` enforceable.
    # ------------------------------------------------------------------
    if scratch_root is not None:
        from apm_cli.utils.path_security import ensure_path_within

        scratch_root = Path(scratch_root).resolve()
        ensure_path_within(Path(project_root).resolve(), scratch_root)

    # Executable approval gate (npm v12-style default-deny). hooks/bin gate
    # below; mcp/canvas unused (mcp filtered upstream, canvas re-derived below).
    _hooks_approved, _bin_approved, _mcp_approved, _canvas_approved = _check_executable_approval(
        package_name, package_info, allow_executables, ctx=ctx
    )

    # Amendment 6: cowork non-skill primitive warning (once per run).
    from apm_cli.install.target_warnings import warn_unsupported_primitives

    warn_unsupported_primitives(package_info, package_name, targets, ctx, diagnostics, logger)

    def _log_integration(msg: str) -> None:
        if logger:
            logger.tree_item(msg)

    _verbose = bool(getattr(ctx, "verbose", False)) if ctx is not None else False

    _INTEGRATOR_KWARGS = {
        "prompts": integrators.prompt,
        "agents": integrators.agent,
        "commands": integrators.command,
        "instructions": integrators.instruction,
        "hooks": integrators.hook,
        "canvas": integrators.canvas,
        "skills": integrators.skill,
    }

    # Aggregate per-primitive across targets so we emit ONE line per kind
    # (per the 1/2/3+ collapse rule), not one per target.
    # Structure: { prim_name: {"files": int, "adopted": int, "label": str, "paths": [str]} }
    _per_kind: dict[str, dict[str, Any]] = {}

    for _prim_name, _entry in _dispatch.items():
        if _entry.multi_target:
            continue  # skills handled separately
        # Executable approval gate: skip hooks if not approved.
        if _prim_name == "hooks" and not _hooks_approved:
            _log_hooks_skip(package_name, package_info, targets, logger)
            continue
        # Executable approval gate: skip canvas if not approved.
        # First-party (is_first_party=True) always deploys.
        if _prim_name == "canvas" and not is_first_party:
            if allow_executables is None:
                _dep_canvas_ok = True
            else:
                from apm_cli.install.exec_gate import resolve_package_key as _rpk
                from apm_cli.security.executables import EXEC_TYPE_CANVAS, is_package_approved

                _dep_canvas_ok = is_package_approved(
                    allow_executables, _rpk(package_info, package_name), EXEC_TYPE_CANVAS
                ) or is_package_approved(allow_executables, package_name, EXEC_TYPE_CANVAS)
            if not _dep_canvas_ok:
                _log_canvas_skip(package_name, package_info, logger)
                continue
        _integrator = _INTEGRATOR_KWARGS[_prim_name]
        # A primitive can be statically present on a target (e.g. the
        # copilot canvas mapping) while a given IntegratorBundle omits its
        # integrator (None). Skip rather than crash so test/replay bundles
        # that don't wire the integrator simply no-op that primitive.
        if _integrator is None:
            continue
        _agg_files = 0
        _agg_adopted = 0
        _agg_paths: list[str] = []
        _agg_hook_payloads: list = []
        _label = _prim_name
        for _target in targets:
            _mapping = _target.primitives.get(_prim_name)
            if _mapping is None:
                continue
            _call_kwargs: dict[str, Any] = {
                "force": force,
                "managed_files": managed_files,
                "diagnostics": diagnostics,
                "scope": scope,
            }
            # Hook integrator alone needs the scope signal and dep target info.
            if _prim_name == "hooks":
                _call_kwargs["user_scope"] = scope is InstallScope.USER
                _call_kwargs["dep_targets_active"] = dep_targets_active
                _call_kwargs["allowed_targets"] = allowed_dep_targets
            # Canvas integration: approval is enforced by the gate above,
            # so always pass trust_canvas=True to let the integrator proceed.
            if _prim_name == "canvas":
                _call_kwargs["trust_canvas"] = True
                _call_kwargs["is_first_party"] = is_first_party
                _call_kwargs["package_name"] = package_name
            _int_result = getattr(_integrator, _entry.integrate_method)(
                _target,
                package_info,
                project_root,
                **_call_kwargs,
            )
            result["links_resolved"] += _int_result.links_resolved
            for tp in _int_result.target_paths:
                deployed.append(_deployed_path_entry(tp, project_root, targets))
            _adopted_attr = getattr(_int_result, "files_adopted", 0)
            # Coerce defensively: subclasses (e.g. HookIntegrationResult)
            # always set this, but tests use MagicMock results which
            # auto-attribute to MagicMock objects whose ``__int__`` is 1.
            # Treat anything that is not a real int as 0 so we never
            # invent fake adopt counts.
            _adopted = _adopted_attr if isinstance(_adopted_attr, int) else 0
            # Show the per-kind line whenever ANY work happened.
            if _int_result.files_integrated <= 0 and _adopted <= 0:
                continue
            _agg_files += _int_result.files_integrated
            _agg_adopted += _adopted
            result[_entry.counter_key] += _int_result.files_integrated
            _effective_root = _mapping.deploy_root or _target.root_dir
            _deploy_dir = (
                f"{_effective_root}/{_mapping.subdir}/"
                if _mapping.subdir
                else f"{_effective_root}/"
            )
            _label, _deploy_dir = _label_and_deploy_dir(_prim_name, _mapping, _target, _deploy_dir)
            if _prim_name == "hooks":
                _agg_hook_payloads.extend(
                    p for p in getattr(_int_result, "display_payloads", []) or []
                )
            _agg_paths.append(_deploy_dir)

        if _agg_files > 0 or _agg_adopted > 0:
            _per_kind[_prim_name] = {
                "files": _agg_files,
                "adopted": _agg_adopted,
                "label": _label,
                "paths": _agg_paths,
                "hook_payloads": _agg_hook_payloads,
            }

    _log_per_kind_results(
        _per_kind,
        _dispatch,
        _verbose,
        logger,
        log_integration=_log_integration,
    )

    skill_result = integrators.skill.integrate_package_skill(
        package_info,
        project_root,
        diagnostics=diagnostics,
        managed_files=managed_files,
        force=force,
        targets=targets,
        skill_subset=skill_subset,
        scope=scope,
        policy=policy,
        skip_bin=not _bin_approved,
    )
    _log_skill_result(
        skill_result,
        result,
        project_root,
        targets,
        _verbose,
        logger,
        package_name=package_name,
        package_info=package_info,
        log_integration=_log_integration,
    )

    # A3: warm-cache visibility.
    _total_integrated = sum(_info["files"] for _info in _per_kind.values())
    _total_integrated += int(skill_result.skill_created)
    _total_integrated += int(skill_result.sub_skills_promoted)
    _total_integrated += int(skill_result.bin_deployed)
    if _total_integrated == 0 and logger:
        logger.tree_item("  |-- (files unchanged)")

    return result


def integrate_local_content(
    project_root: Path,
    *,
    targets: Any,
    integrators: IntegratorBundle,
    force: bool,
    managed_files: Any,
    diagnostics: DiagnosticCollector,
    logger: InstallLogger | None = None,
    scope: InstallScope | None = None,
    source_root: Path | None = None,
    ctx: InstallContext | None = None,
) -> dict:
    """Integrate primitives from the project's own .apm/ directory.

    This treats the project root as a synthetic package so that local
    skills, instructions, agents, prompts, hooks, and commands in .apm/
    are deployed to target directories exactly like dependency primitives.

    Only .apm/ sub-directories are processed.  A root-level SKILL.md is
    intentionally ignored (it describes the project itself, not a
    deployable skill).

    Args:
        project_root: Deploy root -- where ``.claude/``, ``.codex/``,
            etc. are written.  Also used to compute relative paths for
            tracking deployed files.
        integrators: Bundle of the six primitive integrators.
        source_root: Where to discover the synthetic local package's
            ``.apm/`` content.  Defaults to ``project_root`` when not
            provided.  When ``apm install --root`` is in play,
            ``source_root`` stays at ``$PWD`` while ``project_root``
            points to the override.

    Returns a dict with integration counters and deployed file paths,
    same shape as ``integrate_package_primitives()``.
    """
    from ..integration.canvas_integrator import CanvasIntegrator
    from ..models.apm_package import APMPackage, PackageInfo, PackageType

    if source_root is None:
        source_root = project_root

    local_pkg = APMPackage(
        name="_local",
        version="0.0.0",
        package_path=source_root,
        source="local",
    )
    local_info = PackageInfo(
        package=local_pkg,
        install_path=source_root,
        package_type=PackageType.APM_PACKAGE,
    )

    if integrators.canvas is None:
        integrators = IntegratorBundle(
            prompt=integrators.prompt,
            agent=integrators.agent,
            skill=integrators.skill,
            instruction=integrators.instruction,
            command=integrators.command,
            hook=integrators.hook,
            canvas=CanvasIntegrator(),
        )

    return integrate_package_primitives(
        local_info,
        project_root,
        targets=targets,
        integrators=integrators,
        force=force,
        managed_files=managed_files,
        diagnostics=diagnostics,
        package_name="_local",
        logger=logger,
        scope=scope,
        ctx=ctx,
        is_first_party=True,
    )


# Underscore-prefixed aliases for backward compatibility with existing
# imports/patches in tests and elsewhere that use the old names.
_integrate_package_primitives = integrate_package_primitives
_integrate_local_content = integrate_local_content


# ---------------------------------------------------------------------------
# Local bundle integration (issue #1098)
# ---------------------------------------------------------------------------


def integrate_local_bundle(
    bundle_info: Any,
    project_root: Path,
    *,
    targets: Any,
    force: bool = False,
    dry_run: bool = False,
    diagnostics: DiagnosticCollector | None = None,
    logger: InstallLogger | None = None,
    scope: InstallScope | None = None,
    alias: str | None = None,
    allow_executables: builtins.dict[str, builtins.dict[str, bool]] | None = None,
) -> dict:
    """Integrate a detected local bundle into project / user scope.

    Local bundles (``apm pack`` output) are deployed file-by-file into each
    active target's deploy root.  Returns a result dict mirroring
    ``integrate_local_content()`` so the caller can persist
    ``local_deployed_files`` / ``local_deployed_file_hashes`` into the lockfile.

    Does NOT mutate ``apm.yml``.  *allow_executables* gates canvas extensions.
    """
    import hashlib
    import shutil

    from apm_cli.utils.atomic_io import normalize_crlf_to_lf, write_text_lf
    from apm_cli.utils.content_hash import compute_file_hash

    from ..core.scope import InstallScope
    from ..utils.path_security import PathTraversalError, ensure_path_within, validate_path_segments

    pack_files = _load_bundle_pack_files(bundle_info)
    slug = alias or bundle_info.package_id
    pack_files, canvas_skipped = _apply_canvas_gate(
        pack_files, slug, allow_executables, logger=logger, diagnostics=diagnostics
    )
    skipped = canvas_skipped

    text_bundle_suffixes = {".json", ".md", ".toml", ".txt", ".yaml", ".yml"}

    def _normalized_bundle_text(path: Path) -> str | None:
        if path.suffix.lower() not in text_bundle_suffixes:
            return None
        try:
            return normalize_crlf_to_lf(path.read_bytes().decode("utf-8"))
        except UnicodeDecodeError:
            return None

    if logger:
        logger.verbose_detail(
            f"Integrating local bundle '{slug}' "
            f"({len(pack_files)} file(s), targets={[t.name for t in targets]})"
        )

    bundle_dir: Path = bundle_info.source_dir
    deployed_files: list[str] = []
    deployed_hashes: dict[str, str] = {}

    for target in targets:
        resolved_root = getattr(target, "resolved_deploy_root", None)
        default_deploy_root = (
            Path(resolved_root) if resolved_root is not None else project_root / target.root_dir
        )

        _primitive_roots: dict[str, Path] = {}
        for prim_name, prim_mapping in (target.primitives or {}).items():
            if getattr(prim_mapping, "deploy_root", None) and resolved_root is None:
                _primitive_roots[prim_name] = project_root / prim_mapping.deploy_root

        for rel, expected_hash in sorted(pack_files.items()):
            try:
                validate_path_segments(str(rel), context="bundle_files key")
            except PathTraversalError as exc:
                if logger is not None:
                    logger.warning(f"Skipped unsafe bundle entry {rel!r}: {exc}")
                skipped += 1
                continue
            src = bundle_dir / rel
            if not src.is_file() or src.is_symlink():
                skipped += 1
                continue

            dest_pair = _resolve_bundle_file_dest(
                rel,
                target,
                default_deploy_root,
                project_root,
                slug,
                _primitive_roots,
                logger=logger,
            )
            if dest_pair is None:
                skipped += 1
                continue
            dest, deploy_root = dest_pair

            try:
                ensure_path_within(dest, deploy_root)
            except PathTraversalError as exc:
                if logger is not None:
                    logger.warning(f"Skipped unsafe bundle entry {rel!r}: {exc}")
                skipped += 1
                continue

            try:
                if scope == InstallScope.USER:
                    record = dest.as_posix()
                else:
                    record = (
                        dest.relative_to(project_root).as_posix()
                        if dest.is_relative_to(project_root)
                        else dest.as_posix()
                    )
            except ValueError:
                record = dest.as_posix()

            normalized_text = _normalized_bundle_text(src)
            desired_hash = (
                hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()
                if normalized_text is not None
                else expected_hash
            )
            if dry_run:
                deployed_files.append(record)
                deployed_hashes[record] = f"sha256:{desired_hash}"
                if logger:
                    logger.verbose_detail(f"[dry-run] would deploy {record}")
                continue

            if dest.exists() and not force:
                try:
                    existing_hash = hashlib.sha256(dest.read_bytes()).hexdigest()
                except OSError:
                    existing_hash = None
                if existing_hash and existing_hash != desired_hash:
                    skipped += 1
                    msg = (
                        f"Skipped {record}: file exists with different "
                        "content. Re-run with --force to overwrite."
                    )
                    if diagnostics is not None:
                        diagnostics.warn(msg)
                    elif logger is not None:
                        logger.warning(msg)
                    continue

            dest.parent.mkdir(parents=True, exist_ok=True)
            if normalized_text is None:
                shutil.copy2(src, dest, follow_symlinks=False)
            else:
                write_text_lf(dest, normalized_text)
            deployed_files.append(record)
            deployed_hashes[record] = compute_file_hash(dest)
            if logger:
                logger.verbose_detail(f"deployed {record}")

    return {
        "deployed_files": deployed_files,
        "deployed_file_hashes": deployed_hashes,
        "skipped": skipped,
        "skills": 0,
        "agents": 0,
        "commands": 0,
        "hooks": 0,
        "instructions": 0,
        "prompts": 0,
        "sub_skills": 0,
    }
