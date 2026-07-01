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
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .deployed_paths import deployed_path_entry as _deployed_path_entry
from .deployed_paths import skill_bundle_file_entries as _skill_bundle_file_entries
from .target_filter import filter_targets_for_dependency

if TYPE_CHECKING:
    from ..core.command_logger import InstallLogger
    from ..core.scope import InstallScope
    from ..install.context import InstallContext
    from ..integration.base_integrator import BaseIntegrator
    from ..utils.diagnostics import DiagnosticCollector


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
    ``integrate_package_primitives`` below the PLR0913 threshold (≤15) while
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


def _log_hook_display_payloads(
    payloads: list,
    verbose: bool,
    log_fn: Any,
    logger: Any,
) -> None:
    """Emit per-hook-file action summaries for hook transparency."""
    for _payload in payloads:
        _src = _payload.get("source_hook_file", "hook file")
        _actions = _payload.get("actions", [])
        if _actions:
            for _act in _actions:
                log_fn(f"  |   {_act.get('event', '?')}: {_act.get('summary', '?')} ({_src})")
        else:
            log_fn(f"  |   Hook file integrated: {_src}")
        if verbose and logger is not None:
            _out_path = _payload.get("output_path", "")
            logger.verbose_detail(f"  |   Hook JSON ({_src} -> {_out_path}):")
            for _jline in _payload.get("rendered_json", "").splitlines():
                logger.verbose_detail(f"  |     {_jline}")


def _label_and_deploy_dir(prim_name: str, mapping, target, deploy_dir: str) -> tuple[str, str]:
    """Return ``(label, deploy_dir)`` for a per-kind integration line."""
    if prim_name == "instructions" and mapping.output_compare:
        # Rule-dir formats (cursor/claude/windsurf) are the output_compare
        # set; derive the label from the same flag so a new rule format
        # needs no edit here.
        return "rule(s)", deploy_dir
    if prim_name == "instructions":
        return "instruction(s)", deploy_dir
    if prim_name == "hooks":
        if target.hooks_config_display:
            deploy_dir = target.hooks_config_display
        return "hook(s)", deploy_dir
    if prim_name == "canvas":
        return "canvas extension(s)", deploy_dir
    return prim_name, deploy_dir


def _emit_integration_hints(prim_name: str, info: dict, log_integration) -> None:
    """Emit per-primitive 'next step' hints after an integration line."""
    # copilot-app workflows arrive disabled: the row lands enabled=0 and the
    # user must flip the toggle in the Copilot App's Workflows tab before the
    # schedule fires.
    if any(p.startswith("copilot-app/") for p in info["paths"]) and info["files"] > 0:
        log_integration(
            "  |-- workflows arrive disabled; enable from the Copilot App's Workflows tab"
        )
    # Canvas extensions are discovered by Copilot CLI at session start, so a
    # freshly-deployed canvas is not picked up mid-session.
    if prim_name == "canvas" and (info["files"] > 0 or info["adopted"] > 0):
        log_integration("  |-- reload the Copilot session (/clear) or restart to load the canvas")


def _check_executable_approval(
    package_name: str,
    package_info: Any,
    allow_executables: builtins.dict[str, builtins.dict[str, bool]] | None,
    *,
    ctx: InstallContext | None = None,
) -> tuple[bool, bool, bool, bool]:
    """Delegate to ``exec_gate.check_executable_approval``."""
    from apm_cli.install.exec_gate import check_executable_approval

    return check_executable_approval(package_name, package_info, allow_executables, ctx=ctx)


def _resolve_package_key(package_info: Any, package_name: str) -> str:
    """Delegate to ``exec_gate.resolve_package_key``."""
    from apm_cli.install.exec_gate import resolve_package_key

    return resolve_package_key(package_info, package_name)


def _log_hooks_skip(
    package_name: str, package_info: Any, targets: Any, logger: InstallLogger | None
) -> None:
    """Warn about skipped hooks only when the package actually ships them.

    Aligned with :meth:`HookIntegrator.find_hook_files`: checks for
    ``*.json`` in ``.apm/hooks/`` and ``hooks/``.
    """
    _install = Path(package_info.install_path)
    has_hooks = False
    for hook_dir in [_install / ".apm" / "hooks", _install / "hooks"]:
        if hook_dir.is_dir() and any(hook_dir.glob("*.json")):
            has_hooks = True
            break
    if not has_hooks:
        return
    _pkg_label = package_name or getattr(package_info, "name", "unknown")
    if logger:
        logger.warning(
            f"{_pkg_label}: hooks skipped (not approved in allowExecutables). "
            f"Run 'apm approve {_pkg_label}' to approve.",
            symbol="warning",
        )


def _log_canvas_skip(package_name: str, package_info: Any, logger: InstallLogger | None) -> None:
    """Warn about skipped canvas extensions when the package ships them."""
    _install = Path(package_info.install_path)
    extensions_root = _install / ".apm" / "extensions"
    try:
        has_canvas = extensions_root.is_dir() and any(
            (d / "extension.mjs").is_file() for d in extensions_root.iterdir() if d.is_dir()
        )
    except OSError:
        has_canvas = False
    if not has_canvas:
        return
    _pkg_label = package_name or getattr(package_info, "name", "unknown")
    if logger:
        logger.warning(
            f"{_pkg_label}: canvas extension(s) skipped (not approved in allowExecutables). "
            f"Run 'apm approve {_pkg_label}' to approve.",
            symbol="warning",
        )


def integrate_package_primitives(  # noqa: PLR0913, C901
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
    skill_subset: tuple | None = None,
    ctx: InstallContext | None = None,
    scratch_root: Path | None = None,
    policy: Any = None,
    is_first_party: bool = False,
    allow_executables: builtins.dict[str, builtins.dict[str, bool]] | None = None,
    dep_target_subset: list[str] | None = None,
    real_project_root: Path | None = None,
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

    When *allow_executables* is provided, executable primitives (hooks,
    bin/, MCP servers, canvas extensions) are only deployed for packages
    whose key appears in the dict with the matching type set to ``True``.
    Local project content (``package_name == "_local"``) is always trusted.

    Returns a dict with integration counters and the list of deployed file paths.
    """
    from apm_cli.integration.dispatch import get_dispatch_table

    from ..core.scope import InstallScope

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
    # The ``project_root`` passed in will already point at ``scratch_root``
    # (so all writes redirect via target.deploy_path), so this check is
    # purely defense-in-depth against accidental misuse.
    # ------------------------------------------------------------------
    if scratch_root is not None:
        from apm_cli.utils.path_security import ensure_path_within

        scratch_root = Path(scratch_root).resolve()
        # ``project_root`` is the redirect target; it must equal scratch_root
        # OR sit inside it.  ensure_path_within(child, parent) raises if not.
        ensure_path_within(Path(project_root).resolve(), scratch_root)

    # Executable approval gate (npm v12-style default-deny). hooks/bin gate
    # below (~424, ~585); mcp/canvas unused (mcp filtered upstream, canvas re-derived ~433).
    _hooks_approved, _bin_approved, _mcp_approved, _canvas_approved = _check_executable_approval(
        package_name, package_info, allow_executables, ctx=ctx
    )

    # --- Amendment 6: cowork non-skill primitive warning (once per run) ---
    _cowork_active = any(t.name == "copilot-cowork" for t in targets)
    if _cowork_active and ctx is not None and not ctx.cowork_nonsupported_warned:
        _apm_dir = Path(package_info.install_path) / ".apm"
        _NON_SKILL_DIRS = {
            "agents": "agents",
            "prompts": "prompts",
            "instructions": "instructions",
            "hooks": "hooks",
            # Commands live under ``.apm/prompts/`` and cannot be
            # distinguished from general prompts at directory level
            # without inspecting frontmatter.  Omitted to avoid
            # misleading duplicate warnings.
        }
        _found_types = [
            ptype
            for ptype, subdir in _NON_SKILL_DIRS.items()
            if (_apm_dir / subdir).is_dir() and any((_apm_dir / subdir).iterdir())
        ]
        if _found_types:
            _pkg_label = package_name or getattr(package_info, "name", "unknown")
            _types_str = ", ".join(sorted(builtins.set(_found_types)))
            _warn_msg = (
                f"copilot-cowork target only supports skills; "
                f"non-skill primitives in {_pkg_label} "
                f"({_types_str}) will not deploy to cowork"
            )
            if logger:
                logger.warning(_warn_msg, symbol="warning")
            diagnostics.warn(_warn_msg)
            ctx.cowork_nonsupported_warned = True

    def _log_integration(msg):
        if logger:
            logger.tree_item(msg)

    def _format_target_collapse(paths: list[str], verbose: bool) -> tuple[str, list[str]]:
        """Apply the 1/2/3+ multi-target collapse rule.

        Returns a tuple ``(suffix, expansion_lines)``:

        * ``suffix`` -- the text appended after ``-> `` on the aggregate line.
        * ``expansion_lines`` -- extra ``  |     -> <path>`` lines emitted
          AFTER the aggregate line when ``verbose`` is True. Empty list when
          collapsed.

        The rule:
          1 target  -> ``<path1>``
          2 targets -> ``<path1>, <path2>``
          3+        -> ``N targets`` (verbose forces full enumeration)
        """
        deduped: list[str] = []
        seen: set = builtins.set()
        for p in paths:
            if p not in seen:
                seen.add(p)
                deduped.append(p)
        if verbose and len(deduped) >= 2:
            return "", [f"  |     -> {p}" for p in deduped]
        if len(deduped) == 0:
            return "", []
        if len(deduped) == 1:
            return deduped[0], []
        if len(deduped) == 2:
            return f"{deduped[0]}, {deduped[1]}", []
        return f"{len(deduped)} targets", []

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
        # Dependency canvas requires allowExecutables approval; the _local
        # name shortcut in check_executable_approval does NOT bypass this
        # gate when is_first_party=False (defence-in-depth: a malicious dep
        # named _local must not bypass canvas trust via the name alone).
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
            # Hook integrator alone needs the scope signal: project-scope
            # deploys keep ``command`` paths repo-relative (#1394), user-scope
            # deploys absolutize them (#1310 / #1354).  Sibling integrators
            # don't accept this kwarg, so include it only for hooks.
            if _prim_name == "hooks":
                _call_kwargs["user_scope"] = scope is InstallScope.USER
                _call_kwargs["dep_targets_active"] = dep_targets_active
                _call_kwargs["allowed_targets"] = allowed_dep_targets
                # During drift replay, project_root points at the scratch tmpdir
                # while the real project lives elsewhere.  Thread the real root so
                # _is_root_local_package evaluates correctly (#1978).
                if real_project_root is not None:
                    _call_kwargs["real_project_root"] = real_project_root
            # Canvas integration: always pass is_first_party.  Approval
            # is enforced by the gate above (canvas already skipped if
            # not approved and not is_first_party), so here we always
            # pass trust_canvas=True to let the integrator proceed.
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
            # Show the per-kind line whenever ANY work happened -- either
            # a fresh integrate or a silent adopt of pre-existing
            # byte-identical files. Adopt-only runs (e.g. re-install
            # after lockfile wipe) used to print nothing here, which made
            # the install summary look like a no-op even though the
            # lockfile WAS being repopulated. Surfacing adopt counts
            # restores operator trust in CI.
            if _int_result.files_integrated <= 0 and _adopted <= 0:
                continue
            _agg_files += _int_result.files_integrated
            _agg_adopted += _adopted
            # Only count fresh integrations against the package counter
            # so totals like "3 prompts integrated" stay truthful;
            # adopted files are surfaced separately in the per-kind
            # line.
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

    # Emit aggregated per-kind lines in dispatch order so output is stable.
    for _prim_name in _dispatch:
        if _prim_name not in _per_kind:
            continue
        _info = _per_kind[_prim_name]
        _suffix, _expansion = _format_target_collapse(_info["paths"], _verbose)
        # Build the verb + count phrase. When at least one file was
        # freshly integrated we lead with "N X integrated"; pure-adopt
        # runs (no fresh writes) lead with "N X adopted" so the line
        # still appears and the count is truthful.
        _files = _info["files"]
        _adopted = _info["adopted"]
        if _files > 0:
            _verb_phrase = f"{_files} {_info['label']} integrated"
            if _adopted > 0:
                _verb_phrase = f"{_verb_phrase} ({_adopted} adopted)"
        else:
            _verb_phrase = f"{_adopted} {_info['label']} adopted"
        if _expansion:
            _log_integration(f"  |-- {_verb_phrase}:")
            for line in _expansion:
                _log_integration(line)
        else:
            _log_integration(f"  |-- {_verb_phrase} -> {_suffix}")
        # Emit per-hook-file action summaries for the hooks primitive.
        # display_payloads reflects post-path-rewrite data (what is
        # actually written to disk and executed), so this is faithful.
        if _prim_name == "hooks" and _info["files"] > 0:
            _hook_verbose = _verbose or (
                bool(getattr(logger, "verbose", False)) if logger is not None else False
            )
            _log_hook_display_payloads(
                _info.get("hook_payloads", []),
                _hook_verbose,
                _log_integration,
                logger,
            )
        _emit_integration_hints(_prim_name, _info, _log_integration)

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
    _skill_target_dirs: set = builtins.set()
    for tp in skill_result.target_paths:
        try:
            rel = tp.relative_to(project_root)
            if rel.parts:
                _skill_target_dirs.add(rel.parts[0])
        except ValueError:
            # Dynamic-root target (copilot-cowork) -- path is outside project tree.
            _skill_target_dirs.add("copilot-cowork")
    _skill_target_paths = [f"{d}/skills/" for d in sorted(_skill_target_dirs)]
    if not _skill_target_paths:
        _skill_target_paths = ["skills/"]
    _skill_suffix, _skill_expansion = _format_target_collapse(_skill_target_paths, _verbose)
    if skill_result.skill_created:
        result["skills"] += 1
        if _skill_expansion:
            _log_integration("  |-- Skill integrated:")
            for line in _skill_expansion:
                _log_integration(line)
        else:
            _log_integration(f"  |-- Skill integrated -> {_skill_suffix}")
    if skill_result.sub_skills_promoted > 0:
        result["sub_skills"] += skill_result.sub_skills_promoted
        if _skill_expansion:
            _log_integration(f"  |-- {skill_result.sub_skills_promoted} skill(s) integrated:")
            for line in _skill_expansion:
                _log_integration(line)
        else:
            _log_integration(
                f"  |-- {skill_result.sub_skills_promoted} skill(s) integrated -> {_skill_suffix}"
            )
    if skill_result.bin_deployed > 0 or skill_result.bin_skipped_reason:
        from apm_cli.install.exec_gate import log_bin_status

        log_bin_status(skill_result, _skill_suffix, package_name, package_info, _log_integration)
    for tp in skill_result.target_paths:
        deployed.append(_deployed_path_entry(tp, project_root, targets))
        # #1716: also record the bundle's contained files so per-file
        # content hashes cover SKILL.md / assets / scripts. The directory
        # entry above is retained (cleanup's directory-rejection gate and
        # the manifest dir-exclusion contract depend on it); the file
        # entries give ``content-integrity`` its per-file coverage so skill
        # drift is caught under ``apm audit --ci --no-drift``.
        deployed.extend(_skill_bundle_file_entries(tp, project_root, targets))

    # A3: warm-cache visibility. If nothing was integrated for any kind AND
    # no skill was created, emit one annotation so the user knows the dep
    # was evaluated (the [+] header above already carries the SHA).
    _total_integrated = sum(_info["files"] for _info in _per_kind.values())
    _total_integrated += int(skill_result.skill_created)
    _total_integrated += int(skill_result.sub_skills_promoted)
    _total_integrated += int(skill_result.bin_deployed)
    if _total_integrated == 0:
        _log_integration("  |-- (files unchanged)")

    return result


def integrate_local_content(
    project_root: Path,
    *,
    targets: Any,
    prompt_integrator: Any,
    agent_integrator: Any,
    skill_integrator: Any,
    instruction_integrator: Any,
    command_integrator: Any,
    hook_integrator: Any,
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

    return integrate_package_primitives(
        local_info,
        project_root,
        targets=targets,
        integrators=IntegratorBundle(
            prompt=prompt_integrator,
            agent=agent_integrator,
            skill=skill_integrator,
            instruction=instruction_integrator,
            command=command_integrator,
            hook=hook_integrator,
            canvas=CanvasIntegrator(),
        ),
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

    Local bundles are produced by ``apm pack`` and shipped (via shared file,
    USB, etc.) to environments that cannot reach the source registry.  This
    orchestrator deploys the bundle's plugin-format files into each active
    target's deploy root and returns a result dict mirroring
    ``integrate_local_content()``'s shape so the caller can persist
    ``local_deployed_files`` / ``local_deployed_file_hashes`` into the
    project lockfile.

    The bundle is treated as a *synthetic* package -- its slug derives from
    *alias* (``--as``) when provided, else from ``bundle_info.package_id``.

    Important contract: this function does **NOT** mutate ``apm.yml``.  Local
    bundles are imperative deploys, not declarative dependencies.

    Args:
        bundle_info: ``LocalBundleInfo`` describing the verified bundle.
        project_root: Workspace root (or ``Path.home()`` for ``--global``).
        targets: Resolved ``TargetProfile`` instances from
            ``resolve_targets()``.
        force: When ``True``, overwrite locally-modified files on collision.
        dry_run: When ``True``, report what would be deployed without
            writing to disk.
        diagnostics: Diagnostic collector for structured warnings.
        logger: Install-flow logger.
        scope: ``InstallScope`` (project vs user) for downstream consumers.
        alias: Slug override from ``--as``.
        allow_executables: The ``allowExecutables`` block from the consuming
            project's ``apm.yml``.  When ``None`` (no enforcement), all
            executable primitives including canvas are allowed.  When
            provided, canvas extensions from the bundle are only deployed if
            the bundle slug is approved for the ``canvas`` exec type.

    Returns:
        Dict with keys ``deployed_files`` (list[str]),
        ``deployed_file_hashes`` (dict[str, str]), ``skipped`` (int), and
        per-primitive counters (``skills``, ``agents``, ``commands``, ...).
    """
    import hashlib
    import shutil

    from apm_cli.utils.atomic_io import normalize_crlf_to_lf, write_text_lf
    from apm_cli.utils.content_hash import compute_file_hash

    from ..core.scope import InstallScope
    from ..utils.path_security import (
        PathTraversalError,
        ensure_path_within,
        validate_path_segments,
    )

    bundle_dir: Path = bundle_info.source_dir
    pack_files: dict[str, str] = {}
    if bundle_info.lockfile:
        pack = bundle_info.lockfile.get("pack") or {}
        bf = pack.get("bundle_files") or {}
        if isinstance(bf, dict):
            pack_files = {str(k): str(v) for k, v in bf.items()}

    if not pack_files:
        # Fallback: walk bundle and hash everything except apm.lock.yaml
        # and plugin.json.  Prevents zero-deploy when an older bundle
        # without bundle_files lands.
        for fp in bundle_dir.rglob("*"):
            if not fp.is_file() or fp.is_symlink():
                continue
            rel = fp.relative_to(bundle_dir).as_posix()
            # Issue #1207 D2.a: case-insensitive ``plugin.json`` and
            # ``.mcp.json`` skip -- bundle metadata must never deploy to
            # consumer projects.  Match the deploy-loop semantics so
            # case-folding filesystems do not let a renamed file slip
            # into pack_files unnecessarily.
            if rel == "apm.lock.yaml" or rel.lower() == "plugin.json" or rel.lower() == ".mcp.json":
                continue
            pack_files[rel] = hashlib.sha256(fp.read_bytes()).hexdigest()

    text_bundle_suffixes = {".json", ".md", ".toml", ".txt", ".yaml", ".yml"}

    def _normalized_bundle_text(path: Path) -> str | None:
        if path.suffix.lower() not in text_bundle_suffixes:
            return None
        try:
            return normalize_crlf_to_lf(path.read_bytes().decode("utf-8"))
        except UnicodeDecodeError:
            return None

    deployed_files: list[str] = []
    deployed_hashes: dict[str, str] = {}
    skipped = 0

    # py-arch-2: Filter bundle-metadata files (plugin.json, .mcp.json) out of
    # pack_files BEFORE the per-target loop.  These are never deployable in
    # any target, so iterating per-target inflated the skip counter
    # (e.g. one plugin.json on a 2-target install bumped skipped by 2).
    # The case-insensitive match here mirrors the fallback walk above and
    # the previously-inline guards in the deploy loop.
    _filtered_pack_files: dict[str, str] = {}
    for _rel, _hash in pack_files.items():
        if _rel.lower() in {"plugin.json", ".mcp.json"}:
            continue
        _filtered_pack_files[_rel] = _hash
    pack_files = _filtered_pack_files

    slug = alias or bundle_info.package_id

    # Security + feature gate: canvas extensions are executable Node bundles
    # (``extension.mjs``).  A local / offline bundle copies its files
    # verbatim WITHOUT routing through ``CanvasIntegrator``, so the
    # experimental feature flag and the allowExecutables gate must be
    # checked explicitly here.  When the canvas feature flag is OFF, drop
    # paths silently (no-op; canvas type does not exist yet).  When ON,
    # check allowExecutables: if no enforcement block is present (None)
    # canvas deploys freely; otherwise the bundle slug must be approved for
    # the ``canvas`` exec type.
    from ..core.experimental import is_enabled
    from ..integration.canvas_integrator import is_canvas_bundle_path

    _canvas_enabled = is_enabled("canvas")
    if _canvas_enabled:
        from ..security.executables import EXEC_TYPE_CANVAS, is_package_approved

        _canvas_approved_bundle = allow_executables is None or is_package_approved(
            allow_executables, slug, EXEC_TYPE_CANVAS
        )
    else:
        _canvas_approved_bundle = False

    if not (_canvas_enabled and _canvas_approved_bundle):
        _blocked = sorted(r for r in pack_files if is_canvas_bundle_path(r))
        if _blocked:
            for _r in _blocked:
                pack_files.pop(_r, None)
            if _canvas_enabled:
                # Canvas feature on but not approved: block and surface message.
                skipped += len(_blocked)
                _msg = (
                    f"Blocked {len(_blocked)} canvas extension file(s) from bundle "
                    f"'{slug}': canvas extensions are executable extension.mjs code "
                    f"and are not approved in allowExecutables. "
                    f"Run 'apm approve {slug}' to approve them."
                )
                if diagnostics is not None:
                    diagnostics.warn(message=_msg, package=str(slug))
                elif logger is not None:
                    logger.warning(_msg)

    if logger:
        logger.verbose_detail(
            f"Integrating local bundle '{slug}' "
            f"({len(pack_files)} file(s), targets={[t.name for t in targets]})"
        )

    # NOTE(M-arch-1): Local bundles intentionally do NOT route through
    # ``integrate_package_primitives`` -- they are an imperative deploy of
    # opaque files keyed by ``pack.bundle_files`` rather than a primitive
    # tree.  Revisit when local-bundle install needs to share collision /
    # link-resolution logic with the dependency-resolver pipeline.
    # TODO(#1098-v0.13): unify with integrate_package_primitives if/when
    # the bundle format grows primitive-typed transforms.
    for target in targets:
        # Resolve deploy root for this target.  Cowork targets can return
        # a dynamically-resolved path; fall back to root_dir under
        # project_root otherwise.
        resolved_root = getattr(target, "resolved_deploy_root", None)
        if resolved_root is not None:
            default_deploy_root = Path(resolved_root)
        else:
            default_deploy_root = project_root / target.root_dir

        # Build a primitive→deploy_root lookup so bundle entries that fall
        # under a primitive with an explicit ``deploy_root`` (e.g.
        # skills→.agents) are routed to the converged directory rather
        # than the per-client ``target.root_dir``.
        _primitive_roots: dict[str, Path] = {}
        for prim_name, prim_mapping in (target.primitives or {}).items():
            if getattr(prim_mapping, "deploy_root", None) and resolved_root is None:
                _primitive_roots[prim_name] = project_root / prim_mapping.deploy_root

        for rel, expected_hash in sorted(pack_files.items()):
            # CR1: bundle_files keys come from untrusted lockfile YAML
            # inside the bundle.  Reject traversal sequences before
            # constructing any filesystem path, then assert the resolved
            # destination stays inside ``deploy_root``.
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

            # Issue #1207 D2.b: for compile-only targets (opencode, codex,
            # gemini -- no ``instructions`` primitive in their profile),
            # bundle ``instructions/*.md`` files must be staged under
            # ``apm_modules/<slug>/.apm/instructions/`` so ``apm compile``
            # can merge them into the target's AGENTS.md / GEMINI.md /
            # equivalent.  Deploying them verbatim to ``<root>/instructions/``
            # is a no-op for these clients.
            _rel_norm = rel.replace("\\", "/")
            _first_seg = _rel_norm.split("/", 1)[0] if "/" in _rel_norm else ""
            if _first_seg == "instructions" and "instructions" not in (target.primitives or {}):
                # Slug must be safe for filesystem path construction --
                # ``package_id`` originates from untrusted ``plugin.json``.
                # Enforce a strict character whitelist documented in
                # docs/src/content/docs/enterprise/security.md so
                # forward slashes, null bytes, spaces, and other
                # filesystem-significant characters are rejected before
                # any path construction or resolution.
                _slug_str = str(slug)
                # CR1.5 (#1217 review): use ASCII-only validation, not
                # ``str.isalnum`` (which accepts Unicode letters/digits
                # like accented or non-Latin chars and would slip past
                # the documented [A-Za-z0-9._-] whitelist).
                _ALLOWED = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
                _slug_ok = (
                    bool(_slug_str)
                    and all(c in _ALLOWED for c in _slug_str)
                    and not _slug_str.startswith(".")
                    and not _slug_str.endswith(".")
                    and ".." not in _slug_str
                )
                if not _slug_ok:
                    if logger is not None:
                        logger.warning(
                            f"Skipped instruction staging for unsafe slug {_slug_str!r}: "
                            "slug must match [A-Za-z0-9._-]+ with no leading/trailing dot, no '..'"
                        )
                    skipped += 1
                    continue
                try:
                    validate_path_segments(_slug_str, context="bundle slug")
                except PathTraversalError as exc:
                    if logger is not None:
                        logger.warning(
                            f"Skipped instruction staging for unsafe slug {_slug_str!r}: {exc}"
                        )
                    skipped += 1
                    continue
                stage_root = project_root / "apm_modules" / slug / ".apm" / "instructions"
                try:
                    ensure_path_within(stage_root, project_root / "apm_modules")
                except PathTraversalError as exc:
                    if logger is not None:
                        logger.warning(f"Skipped unsafe stage root for {slug!r}: {exc}")
                    skipped += 1
                    continue
                # PR #1217 review: preserve nested subdirs under
                # ``instructions/`` so two files with the same basename
                # (e.g. ``instructions/a/x.md`` and
                # ``instructions/b/x.md``) do not collide at the staged
                # location.  ``rel`` already starts with
                # ``instructions/`` so we strip that prefix before
                # joining under the stage root (which itself ends in
                # ``.apm/instructions``).
                _rel_under_instructions = rel.split("/", 1)[1] if "/" in rel else Path(rel).name
                dest = stage_root / _rel_under_instructions
                deploy_root = stage_root
            else:
                # Canvas extensions are Copilot-only.  A plugin bundle is
                # target-agnostic, so guard against depositing an
                # ``extensions/`` tree into a non-Copilot client root
                # (e.g. ``.claude/extensions/``).  Skip silently for other
                # targets; the trust filter above already removed these
                # entries entirely when canvas was not trusted.
                if _first_seg.lower() == "extensions" and target.name != "copilot":
                    continue
                # Route the file to the correct deploy root.  If the first
                # path segment matches a primitive with an explicit
                # ``deploy_root`` (e.g. ``skills/`` -> ``.agents/``), use
                # the converged directory.  Otherwise fall back to the
                # target's default root.
                deploy_root = _primitive_roots.get(_first_seg, default_deploy_root)
                dest = deploy_root / rel
            try:
                ensure_path_within(dest, deploy_root)
            except PathTraversalError as exc:
                if logger is not None:
                    logger.warning(f"Skipped unsafe bundle entry {rel!r}: {exc}")
                skipped += 1
                continue
            try:
                if scope == InstallScope.USER:
                    # User scope: record absolute paths.
                    record = dest.as_posix()
                else:
                    # Project scope: record paths relative to project_root.
                    record = (
                        dest.relative_to(project_root).as_posix()
                        if dest.is_relative_to(project_root)
                        else dest.as_posix()
                    )
            except ValueError:
                record = dest.as_posix()

            normalized_text = _normalized_bundle_text(src)
            if normalized_text is None:
                desired_hash = expected_hash
            else:
                desired_hash = hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()

            if dry_run:
                deployed_files.append(record)
                # Normalize to "sha256:<hex>" so the dry-run lockfile preview
                # matches the format written by ``compute_file_hash`` on the
                # real deploy path.  ``desired_hash`` here is bare hex for
                # the bytes this deploy path writes; without the prefix, downstream
                # exact-match comparisons (e.g. ``cleanup.py`` provenance
                # check) treat the file as user-edited and skip cleanup.
                deployed_hashes[record] = f"sha256:{desired_hash}"
                if logger:
                    logger.verbose_detail(f"[dry-run] would deploy {record}")
                continue

            # Collision handling: skip if file exists and content differs
            # and not force.  Idempotent (same content) writes are silent.
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
            # IM4: hash the deployed file after the deploy transform rather
            # than trusting the source bundle's expected_hash.  Local bundle
            # text files may be LF-normalized during deploy, so the lockfile
            # must bind the actual on-disk bytes.
            deployed_files.append(record)
            # Use ``compute_file_hash`` so the recorded value carries the
            # canonical ``sha256:<hex>`` prefix.  Matches the format written
            # by the regular install pipeline (``compute_deployed_hashes``)
            # so subsequent stale-cleanup provenance checks compare equal
            # instead of mis-classifying these files as user-edited.
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
