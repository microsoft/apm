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
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..core.command_logger import InstallLogger
    from ..core.scope import InstallScope
    from ..install.context import InstallContext
    from ..utils.diagnostics import DiagnosticCollector


# CRITICAL: Shadow Python builtins that share names with Click commands so
# ``set()`` / ``list()`` / ``dict()`` resolve to the builtins, not Click
# subcommand objects.  ``commands/install`` and ``install/pipeline`` do the
# same dance for the same reason.
set = builtins.set
list = builtins.list
dict = builtins.dict


def _deployed_path_entry(
    target_path: Path,
    project_root: Path,
    targets: Any,
) -> str:
    """Return the lockfile-safe path string for a deployed file.

    For standard targets the entry is ``project_root``-relative.  For
    cowork (dynamic-root) targets the entry uses the synthetic
    ``cowork://`` URI scheme so the lockfile pipeline does not attempt
    a ``Path.relative_to(project_root)`` that would crash.

    Raises
    ------
    RuntimeError
        If the path is outside the project tree and cannot be
        translated to a ``cowork://`` URI via any available target.
    """
    try:
        return target_path.relative_to(project_root).as_posix()
    except ValueError:
        # Path is outside the project tree -- must be a dynamic-root
        # target.  Find the matching target and translate.
        if targets:
            for _t in targets:
                if _t.resolved_deploy_root is not None:
                    from apm_cli.integration.copilot_cowork_paths import to_lockfile_path

                    return to_lockfile_path(target_path, _t.resolved_deploy_root)
        raise RuntimeError(  # noqa: B904
            f"Cannot translate {target_path!r} to a lockfile path: "
            f"path is outside the project tree and no dynamic-root "
            f"target matched. This is a bug — please report it."
        )


def integrate_package_primitives(
    package_info: Any,
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
    package_name: str = "",
    logger: InstallLogger | None = None,
    scope: InstallScope | None = None,
    skill_subset: tuple | None = None,
    ctx: InstallContext | None = None,
    scratch_root: Path | None = None,
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

    Returns a dict with integration counters and the list of deployed file paths.
    """
    from apm_cli.integration.dispatch import get_dispatch_table

    _dispatch = get_dispatch_table()
    result = {
        "prompts": 0,
        "agents": 0,
        "skills": 0,
        "sub_skills": 0,
        "instructions": 0,
        "commands": 0,
        "hooks": 0,
        "links_resolved": 0,
        "deployed_files": [],
    }

    deployed = result["deployed_files"]

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
        "prompts": prompt_integrator,
        "agents": agent_integrator,
        "commands": command_integrator,
        "instructions": instruction_integrator,
        "hooks": hook_integrator,
        "skills": skill_integrator,
    }

    # Aggregate per-primitive across targets so we emit ONE line per kind
    # (per the 1/2/3+ collapse rule), not one per target.
    # Structure: { prim_name: {"files": int, "adopted": int, "label": str, "paths": [str]} }
    _per_kind: dict[str, dict[str, Any]] = {}

    for _prim_name, _entry in _dispatch.items():
        if _entry.multi_target:
            continue  # skills handled separately
        _integrator = _INTEGRATOR_KWARGS[_prim_name]
        _agg_files = 0
        _agg_adopted = 0
        _agg_paths: list[str] = []
        _label = _prim_name
        for _target in targets:
            _mapping = _target.primitives.get(_prim_name)
            if _mapping is None:
                continue
            _int_result = getattr(_integrator, _entry.integrate_method)(
                _target,
                package_info,
                project_root,
                force=force,
                managed_files=managed_files,
                diagnostics=diagnostics,
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
            if _prim_name == "instructions" and _mapping.format_id in (
                "cursor_rules",
                "claude_rules",
            ):
                _label = "rule(s)"
            elif _prim_name == "instructions":
                _label = "instruction(s)"
            elif _prim_name == "hooks":
                if _target.hooks_config_display:
                    _deploy_dir = _target.hooks_config_display
                _label = "hook(s)"
            else:
                _label = _prim_name
            _agg_paths.append(_deploy_dir)

        if _agg_files > 0 or _agg_adopted > 0:
            _per_kind[_prim_name] = {
                "files": _agg_files,
                "adopted": _agg_adopted,
                "label": _label,
                "paths": _agg_paths,
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

    skill_result = skill_integrator.integrate_package_skill(
        package_info,
        project_root,
        diagnostics=diagnostics,
        managed_files=managed_files,
        force=force,
        targets=targets,
        skill_subset=skill_subset,
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
    for tp in skill_result.target_paths:
        deployed.append(_deployed_path_entry(tp, project_root, targets))

    # A3: warm-cache visibility. If nothing was integrated for any kind AND
    # no skill was created, emit one annotation so the user knows the dep
    # was evaluated (the [+] header above already carries the SHA).
    _total_integrated = sum(_info["files"] for _info in _per_kind.values())
    _total_integrated += int(skill_result.skill_created)
    _total_integrated += int(skill_result.sub_skills_promoted)
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
    ctx: InstallContext | None = None,
) -> dict:
    """Integrate primitives from the project's own .apm/ directory.

    This treats the project root as a synthetic package so that local
    skills, instructions, agents, prompts, hooks, and commands in .apm/
    are deployed to target directories exactly like dependency primitives.

    Only .apm/ sub-directories are processed.  A root-level SKILL.md is
    intentionally ignored (it describes the project itself, not a
    deployable skill).

    Returns a dict with integration counters and deployed file paths,
    same shape as ``integrate_package_primitives()``.
    """
    from ..models.apm_package import APMPackage, PackageInfo, PackageType

    local_pkg = APMPackage(
        name="_local",
        version="0.0.0",
        package_path=project_root,
        source="local",
    )
    local_info = PackageInfo(
        package=local_pkg,
        install_path=project_root,
        package_type=PackageType.APM_PACKAGE,
    )

    return integrate_package_primitives(
        local_info,
        project_root,
        targets=targets,
        prompt_integrator=prompt_integrator,
        agent_integrator=agent_integrator,
        skill_integrator=skill_integrator,
        instruction_integrator=instruction_integrator,
        command_integrator=command_integrator,
        hook_integrator=hook_integrator,
        force=force,
        managed_files=managed_files,
        diagnostics=diagnostics,
        package_name="_local",
        logger=logger,
        scope=scope,
        ctx=ctx,
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

    Returns:
        Dict with keys ``deployed_files`` (list[str]),
        ``deployed_file_hashes`` (dict[str, str]), ``skipped`` (int), and
        per-primitive counters (``skills``, ``agents``, ``commands``, ...).
    """
    import hashlib
    import shutil

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
            _first_seg = rel.split("/", 1)[0] if "/" in rel else ""
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

            if dry_run:
                deployed_files.append(record)
                # Normalize to "sha256:<hex>" so the dry-run lockfile preview
                # matches the format written by ``compute_file_hash`` on the
                # real deploy path.  ``expected_hash`` here is bare hex from
                # ``pack.bundle_files``; without the prefix, downstream
                # exact-match comparisons (e.g. ``cleanup.py`` provenance
                # check) treat the file as user-edited and skip cleanup.
                deployed_hashes[record] = f"sha256:{expected_hash}"
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
                if existing_hash and existing_hash != expected_hash:
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
            shutil.copy2(src, dest, follow_symlinks=False)
            # IM4: hash the deployed file (post-copy) rather than trusting
            # the source bundle's expected_hash.  Today the integrator is a
            # raw copy so the values match, but documenting deployed-file
            # provenance now keeps the lockfile honest if future transforms
            # (frontmatter injection, etc.) mutate content during deploy.
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
