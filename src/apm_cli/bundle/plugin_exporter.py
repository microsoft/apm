"""Plugin exporter -- transforms APM packages into plugin-native directories.

Produces a standalone plugin directory that Copilot CLI, Claude Code, or other
plugin hosts can consume directly.  The output contains plugin-spec artefacts
(``agents/``, ``skills/``, ``commands/``, ``plugin.json``) plus an embedded
``apm.lock.yaml`` carrying provenance metadata + a per-file SHA-256 manifest
under ``pack.bundle_files`` (issue #1098).
"""

import hashlib
import json
import re
import shutil
from copy import deepcopy
from pathlib import Path, PurePosixPath, PureWindowsPath

import yaml

from apm_cli.core.scope import manifest_file

from ..core.remediation import (
    PACK_DEPENDENCY_UNATTESTED,
    PACK_INSTALLED_FILES_MISSING,
    PACK_SKILL_SUBSET_MISSING,
)
from ..deps.lockfile import (
    LockedDependency,
    LockFile,
    installed_apm_version,
    resolve_lockfile_path_for_read,
)
from ..models.apm_package import APMPackage, DependencyReference
from ..models.dependency.subsets import skill_subset_filter_tokens
from ..utils.archive import (
    projected_archive_path,
    validate_archive_format,
    write_tar_archive,
    write_zip_archive,
)
from ..utils.atomic_io import write_text_lf
from ..utils.console import _rich_info, _rich_warning
from ..utils.path_security import PathTraversalError, ensure_path_within, safe_rmtree
from ..utils.paths import portable_relpath
from .attest import verify_attested_file
from .credential_files import refuse_credential_files
from .packer import PackResult
from .plugin_layout import (
    PLUGIN_ROOT_DIRS,
    deployed_rendering_extension,
    deployed_rendering_rank,
    deployed_rendering_suffix,
    find_plugin_root_sources,
    superseded_plugin_renderings,
)

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _validate_output_rel(rel: str) -> bool:
    """Return True when *rel* is safe to write inside the output directory."""
    from pathlib import PurePosixPath, PureWindowsPath

    if PurePosixPath(rel).is_absolute() or PureWindowsPath(rel).is_absolute():
        return False
    return ".." not in Path(rel).parts


def _scan_bundle_sources(file_map: dict[str, tuple[Path, str]], logger=None) -> None:
    """Warn when bundle source files contain hidden Unicode characters."""
    from ..security.gate import WARN_POLICY, SecurityGate

    scan_findings_total = 0
    for src, _owner in file_map.values():
        if src.is_symlink():
            continue
        if src.is_dir():
            verdict = SecurityGate.scan_files(src, policy=WARN_POLICY)
            scan_findings_total += len(verdict.all_findings)
        elif src.is_file():
            try:
                text = src.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            verdict = SecurityGate.scan_text(text, str(src), policy=WARN_POLICY)
            scan_findings_total += len(verdict.all_findings)
    if scan_findings_total:
        message = (
            f"Bundle contains {scan_findings_total} hidden character(s) across "
            "source files -- run 'apm audit' to inspect before publishing"
        )
        if logger:
            logger.warning(message)
        else:
            _rich_warning(message)


def _write_bundle_sources(
    file_map: dict[str, tuple[Path, str]],
    bundle_dir: Path,
    output_dir: Path,
) -> None:
    """Write validated bundle sources into a clean output directory."""
    if bundle_dir.exists():
        safe_rmtree(bundle_dir, output_dir)
    bundle_dir.mkdir(parents=True, exist_ok=True)
    for output_rel, (source_abs, _owner) in file_map.items():
        if not _validate_output_rel(output_rel) or source_abs.is_symlink():
            continue
        dest = bundle_dir / output_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            ensure_path_within(dest, bundle_dir)
        except PathTraversalError:
            continue
        shutil.copy2(source_abs, dest, follow_symlinks=False)


_SAFE_BUNDLE_NAME_RE = re.compile(r"[^a-zA-Z0-9._-]")


def _sanitize_bundle_name(name: str) -> str:
    """Sanitize a package name/version for use as a directory component.

    Replaces path separators and traversal characters with hyphens, then
    validates the result is a single safe path component.
    """
    sanitized = _SAFE_BUNDLE_NAME_RE.sub("-", name).strip("-") or "unnamed"
    if ".." in sanitized or "/" in sanitized or "\\" in sanitized:
        sanitized = "unnamed"
    return sanitized


def _rename_prompt(name: str) -> str:
    """Strip the ``.prompt`` infix so ``foo.prompt.md`` becomes ``foo.md``."""
    if name.endswith(".prompt.md"):
        return name[: -len(".prompt.md")] + ".md"
    return name


def _normalize_bare_skill_slug(slug: str) -> str:
    """Normalize bare-skill slugs derived from dependency virtual paths."""
    normalized = slug.replace("\\", "/").strip("/")
    while normalized.startswith("skills/"):
        normalized = normalized[len("skills/") :].lstrip("/")
    if normalized == "skills":
        return ""
    return PurePosixPath(normalized).as_posix() if normalized else ""


# ---------------------------------------------------------------------------
# Component collectors
# ---------------------------------------------------------------------------


def _collect_apm_components(
    apm_dir: Path, *, skipped_links: list[Path] | None = None
) -> list[tuple[Path, str]]:
    """Collect all components from a package's ``.apm/`` directory.

    Returns a list of ``(source_abs, output_rel_posix)`` tuples using the
    APM → plugin mapping table. Symlinked source files are never packed;
    pass *skipped_links* to receive them for a visible warning.
    """
    components: list[tuple[Path, str]] = []
    if not apm_dir.is_dir():
        return components

    # agents/ -> agents/
    _collect_flat(apm_dir / "agents", "agents", components, skipped=skipped_links)

    # skills/ -> skills/ (preserve sub-directory structure). When .apm/ authors
    # no skill, the root skills/ install deploys is packed instead (#4709).
    if _packs_root_skills(apm_dir.parent):
        _collect_recursive(apm_dir.parent / "skills", "skills", components, skipped=skipped_links)
    else:
        _collect_recursive(apm_dir / "skills", "skills", components, skipped=skipped_links)

    # prompts/ -> commands/ (rename .prompt.md -> .md)
    _collect_recursive(
        apm_dir / "prompts", "commands", components, rename=_rename_prompt, skipped=skipped_links
    )

    # instructions/ -> instructions/
    _collect_recursive(apm_dir / "instructions", "instructions", components, skipped=skipped_links)

    # commands/ -> commands/
    _collect_recursive(apm_dir / "commands", "commands", components, skipped=skipped_links)

    # extensions/ -> extensions/ (canvas extensions, experimental Copilot-only).
    # Preserved verbatim so an offline bundle can carry a canvas; the files are
    # inert until the consumer enables the ``canvas`` experimental flag AND
    # approves the package via allowExecutables / ``apm approve`` at install time.
    _collect_recursive(apm_dir / "extensions", "extensions", components, skipped=skipped_links)

    return components


def _packs_root_skills(package_root: Path) -> bool:
    """Whether pack takes skills from root ``skills/`` beside an ``.apm/`` tree.

    Only when ``.apm/skills/`` authors no skill and the root ``skills/`` holds
    one: the skills a path install deploys then come from the root (see
    ``SkillIntegrator.skill_source_paths``). With skills in both, ``.apm/``
    stays the publishing source and the root directory is reported skipped.
    """
    from ..integration.skill_integrator import SkillIntegrator

    root = package_root / "skills"
    authored = package_root / ".apm" / "skills"
    # A skill is a directory holding a SKILL.md file, as install counts them.
    if root.is_symlink() or (
        authored.is_dir()
        and any(child.is_dir() and (child / "SKILL.md").is_file() for child in authored.iterdir())
    ):
        return False
    return any(
        source.parent == root
        for source in SkillIntegrator.skill_source_paths(package_root).values()
    )


def _collect_root_plugin_components(
    project_root: Path, *, skipped_links: list[Path] | None = None
) -> list[tuple[Path, str]]:
    """Collect plugin-native components authored at root level.

    Packages that already follow the plugin directory convention (``agents/``,
    ``skills/``, etc. at the repo root) have their files picked up here.
    """
    components: list[tuple[Path, str]] = []
    for dir_name in PLUGIN_ROOT_DIRS:
        if dir_name == "hooks":
            continue
        _collect_recursive(project_root / dir_name, dir_name, components, skipped=skipped_links)
    return components


def _emit_pack_warning(message: str, logger=None) -> None:
    """Send a pack warning through the command logger when available."""
    if logger:
        logger.warning(message)
    else:
        _rich_warning(message, symbol="warning")


def _warn_skipped_source_links(project_root: Path, links: list[Path], logger=None) -> None:
    """Name each symlinked source file pack dropped, matching own-project install.

    Package sources are not followed through symlinks (own-source link
    expansion was declined); the warning keeps the drop visible.
    """
    for link in links:
        # Resolve only the parent: the link itself must be named, not its target.
        relative = f"{portable_relpath(link.parent, project_root)}/{link.name}"
        _emit_pack_warning(
            f"Skipped symlinked source file {relative}: "
            "apm pack does not follow symlinks in package sources. Replace it with "
            "a regular file to ship it.",
            logger,
        )


def _warn_skipped_root_components(project_root: Path, logger=None) -> None:
    """Explain why plugin-native root sources are not packed."""
    for source in find_plugin_root_sources(project_root):
        if source == "skills" and _packs_root_skills(project_root):
            continue
        if source == "hooks.json":
            message = (
                "Skipping root-level hooks.json because .apm/ is present. "
                "Move publishable hook configuration to .apm/hooks/ or remove "
                "hooks.json to silence this warning."
            )
        else:
            message = (
                f"Skipping root-level {source}/ because .apm/ is present. "
                f"Move publishable files to .apm/{source}/ or remove {source}/ "
                "to silence this warning."
            )
        _emit_pack_warning(message, logger)


def _warn_no_local_primitives(logger=None) -> None:
    """Explain how to recover from an empty APM-native source layout."""
    _emit_pack_warning(
        "No local primitives found. Expected content under .apm/. "
        "Move plugin-native content into .apm/, or remove .apm/ to restore "
        "root convention discovery.",
        logger,
    )


def _collect_bare_skill(
    install_path: Path,
    dep: "LockedDependency",
    out: list[tuple[Path, str]],
) -> None:
    """Detect a bare Claude skill (SKILL.md at dep root, no skills/ subdir).

    Bare skills are packages consisting of just ``SKILL.md`` + supporting files
    at the package root.  They have no ``.apm/`` directory or ``skills/``
    subdirectory, so the normal collectors miss them.  Map the entire package
    into ``skills/{name}/`` so the plugin host can discover it.
    """
    skill_md = install_path / "SKILL.md"
    if not skill_md.is_file():
        return
    # Already collected via .apm/skills/ or root skills/ — skip
    if any(rel.startswith("skills/") for _, rel in out):
        return
    # Derive a slug: prefer virtual_path (e.g. "frontend-design"), else last
    # segment of repo_url (e.g. "my-skill" from "owner/my-skill")
    slug = _normalize_bare_skill_slug(getattr(dep, "virtual_path", "") or "")
    if not slug:
        slug = dep.repo_url.rsplit("/", 1)[-1] if dep.repo_url else "skill"
    for f in sorted(install_path.iterdir()):
        if (
            f.is_file()
            and not f.is_symlink()
            and f.name
            not in (
                "apm.yml",
                "apm.lock.yaml",
                "plugin.json",
            )
        ):
            out.append((f, f"skills/{slug}/{f.name}"))


# -- low-level walkers -------------------------------------------------------


def _record_skipped_link(path: Path, skipped: list[Path] | None) -> None:
    """Record a symlinked source file the walkers will not pack."""
    if skipped is not None and path.is_symlink() and not path.is_dir():
        skipped.append(path)


def _collect_flat(
    src_dir: Path,
    output_prefix: str,
    out: list[tuple[Path, str]],
    *,
    rename=None,
    skipped: list[Path] | None = None,
) -> None:
    """Add every regular non-symlink file directly inside *src_dir*."""
    if src_dir.is_symlink() or not src_dir.is_dir():
        return
    for f in sorted(src_dir.iterdir()):
        _record_skipped_link(f, skipped)
        if f.is_file() and not f.is_symlink():
            name = rename(f.name) if rename else f.name
            out.append((f, f"{output_prefix}/{name}"))


def _collect_recursive(
    src_dir: Path,
    output_prefix: str,
    out: list[tuple[Path, str]],
    *,
    rename=None,
    skipped: list[Path] | None = None,
) -> None:
    """Add every regular non-symlink file under *src_dir*, preserving hierarchy."""
    if src_dir.is_symlink() or not src_dir.is_dir():
        return
    for f in sorted(src_dir.rglob("*")):
        _record_skipped_link(f, skipped)
        if not f.is_file() or f.is_symlink():
            continue
        rel = f.relative_to(src_dir)
        name = rename(rel.name) if rename else rel.name
        out_rel = (rel.parent / name).as_posix()
        out.append((f, f"{output_prefix}/{out_rel}"))


# ---------------------------------------------------------------------------
# Hooks / MCP merging
# ---------------------------------------------------------------------------


_MAX_MERGE_DEPTH = 20


def _deep_merge(base: dict, overlay: dict, *, overwrite: bool = False, _depth: int = 0) -> None:
    """Recursively merge *overlay* into *base*.

    When *overwrite* is False (default), existing base keys win.
    When *overwrite* is True, overlay keys overwrite base keys.

    Raises ``ValueError`` if nesting exceeds ``_MAX_MERGE_DEPTH``.
    """
    if _depth > _MAX_MERGE_DEPTH:
        raise ValueError(f"Hooks/MCP config exceeds maximum nesting depth ({_MAX_MERGE_DEPTH})")
    for key, value in overlay.items():
        if key not in base:
            base[key] = value
        elif overwrite:
            if isinstance(base[key], dict) and isinstance(value, dict):
                _deep_merge(base[key], value, overwrite=True, _depth=_depth + 1)
            else:
                base[key] = value
        elif isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value, overwrite=False, _depth=_depth + 1)


def _collect_hooks_from_apm(
    apm_dir: Path, *, documents: list[tuple[Path, dict]] | None = None
) -> dict:
    """Return merged hooks from ``.apm/hooks/*.json``."""
    hooks: dict = {}
    hooks_dir = apm_dir / "hooks"
    if not hooks_dir.is_dir():
        return hooks
    for f in sorted(hooks_dir.iterdir()):
        if f.is_file() and f.suffix == ".json" and f.name != "package.json" and not f.is_symlink():
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    if documents is not None:
                        documents.append((f, deepcopy(data)))
                    else:
                        _deep_merge(hooks, data, overwrite=False)
            except (OSError, ValueError, RecursionError):
                # Untrusted .apm/hooks/*.json: oversized-int -> bare ValueError,
                # deep nest -> RecursionError. Fail closed (skip this file).
                pass
    return hooks


def _collect_hooks_from_root(
    package_root: Path, *, documents: list[tuple[Path, dict]] | None = None
) -> dict:
    """Return hooks from a root-level ``hooks.json`` or ``hooks/`` directory."""
    hooks: dict = {}
    # Single file
    hooks_file = package_root / "hooks.json"
    if hooks_file.is_file() and not hooks_file.is_symlink():
        try:
            data = json.loads(hooks_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                if documents is not None:
                    documents.append((hooks_file, deepcopy(data)))
                else:
                    _deep_merge(hooks, data, overwrite=False)
        except (OSError, ValueError, RecursionError):
            # Untrusted root hooks.json: fail closed (oversized-int ValueError /
            # deep-nest RecursionError are not JSONDecodeError).
            pass
    # Directory
    hooks_dir = package_root / "hooks"
    if hooks_dir.is_dir():
        for f in sorted(hooks_dir.iterdir()):
            if (
                f.is_file()
                and f.suffix == ".json"
                and f.name != "package.json"
                and not f.is_symlink()
            ):
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        if documents is not None:
                            documents.append((f, deepcopy(data)))
                        else:
                            _deep_merge(hooks, data, overwrite=False)
                except (OSError, ValueError, RecursionError):
                    # Untrusted .../hooks/*.json: fail closed (skip this file).
                    pass
    return hooks


def _collect_mcp(package_root: Path, *, exclude_apm_managed: bool = True) -> dict:
    """Return ``mcpServers`` dict from ``.mcp.json``."""
    from ..core.plugin_manifest import collect_mcp_servers

    return collect_mcp_servers(package_root, exclude_apm_managed=exclude_apm_managed)


# ---------------------------------------------------------------------------
# devDependencies filtering
# ---------------------------------------------------------------------------


def _get_dev_dependency_urls(apm_yml_path: Path) -> set[tuple[str, str]]:
    """Read ``devDependencies.apm`` from raw YAML and return a set of
    ``(repo_url, virtual_path)`` tuples for matching against lockfile entries.

    Using the composite key avoids false positives when multiple virtual
    packages share the same base repo (e.g. different sub-paths under
    ``github/awesome-copilot``).
    """
    try:
        from ..utils.yaml_io import load_yaml

        data = load_yaml(apm_yml_path)
    except (yaml.YAMLError, OSError, ValueError):
        return set()
    if not isinstance(data, dict):
        return set()
    dev_deps = data.get("devDependencies", {})
    if not isinstance(dev_deps, dict):
        return set()
    apm_dev = dev_deps.get("apm", [])
    if not isinstance(apm_dev, list):
        return set()
    keys: set[tuple[str, str]] = set()
    for dep in apm_dev:
        if isinstance(dep, str):
            try:
                ref = DependencyReference.parse(dep)
                keys.add((ref.repo_url, ref.virtual_path or ""))
            except ValueError:
                keys.add((dep, ""))
        elif isinstance(dep, dict):
            try:
                ref = DependencyReference.parse_from_dict(dep)
                keys.add((ref.repo_url, ref.virtual_path or ""))
            except ValueError:
                pass
    return keys


# ---------------------------------------------------------------------------
# Plugin.json helpers
# ---------------------------------------------------------------------------


def _find_or_synthesize_plugin_json(
    project_root: Path,
    apm_yml_path: Path,
    *,
    suppress_missing_warning: bool = False,
    logger=None,
) -> dict:
    """Locate an existing ``plugin.json`` or synthesise one from ``apm.yml``."""
    from ..core.plugin_manifest import find_or_synthesize_plugin_json

    return find_or_synthesize_plugin_json(
        project_root,
        apm_yml_path,
        suppress_missing_warning=suppress_missing_warning,
        logger=logger,
    )


def _has_marketplace_block(apm_yml_path: Path) -> bool:
    """Return True if apm.yml declares a non-empty ``marketplace:`` block."""
    try:
        import yaml

        # Bounded loader so a hostile apm.yml cannot wedge the export with a
        # merge/alias expansion bomb (fails closed as yaml.YAMLError).
        from apm_cli.utils.yaml_io import load_yaml

        data = load_yaml(apm_yml_path) or {}
    except (OSError, yaml.YAMLError):
        return False
    return bool(data.get("marketplace"))


def _reconcile_plugin_json_version(
    plugin_json: dict,
    pkg_version: str,
    *,
    source_label: str = "plugin.json",
    logger=None,
) -> dict:
    """Force the bundled ``plugin_json["version"]`` to the ``apm.yml`` version.

    The bundle directory is named ``<name>-<version>`` from ``apm.yml``, so a
    disk-first ``plugin.json`` that still carries an older release's version
    would ship a self-contradicting bundle: ``build/pkg-1.0.3/plugin.json``
    declaring ``1.0.2`` (#3351). Only the bundle copy changes; the authored
    source file is never rewritten. A conflicting authored version warns
    (naming ``source_label``) so the author can fix it; an absent one is
    filled silently because nothing contradicts ``apm.yml``.

    Returns a new dict; the input is not mutated.
    """
    result = dict(plugin_json)
    authored_version = result.get("version")
    if authored_version == pkg_version:
        return result
    result["version"] = pkg_version
    if authored_version is None:
        return result
    msg = (
        f"{source_label} declares version {authored_version} but apm.yml declares "
        f"{pkg_version}; the bundled plugin.json uses {pkg_version}. "
        f"Update the version in {source_label} to match apm.yml."
    )
    if logger:
        logger.warning(msg)
    else:
        _rich_warning(msg)
    return result


def _update_plugin_json_paths(plugin_json: dict, output_files: list[str], logger=None) -> dict:
    r"""Strip component-path keys from ``plugin.json``.

    Per the official Claude Code plugin manifest schema, the
    ``agents``/``skills``/``commands`` keys point to *additional* files
    OUTSIDE the convention directories (``agents/``, ``skills/``,
    ``commands/``) and each entry must match ``^\./.*`` (relative path)
    and the per-key file-extension pattern. The ``instructions`` key is
    not defined by the schema at all. The convention directories
    themselves are auto-discovered by Claude Code -- listing them here
    is invalid (or unrecognized).

    APM emits everything into the convention directories, so we drop
    these keys entirely to keep the manifest schema-conformant.

    The ``output_files`` argument is retained for signature stability
    (and as a hook for future "additional files" extensions); it is
    currently unused.
    """
    result = dict(plugin_json)
    stripped = [k for k in ("agents", "skills", "commands", "instructions") if k in result]
    for key in stripped:
        result.pop(key, None)
    if stripped:
        msg = (
            "Stripped schema-invalid keys from authored plugin.json: "
            f"{', '.join(stripped)} -- convention directories are auto-discovered by Claude Code"
        )
        if logger:
            logger.warning(msg)
        else:
            _rich_warning(msg)
    return result


# ---------------------------------------------------------------------------
# Dep → filesystem helpers
# ---------------------------------------------------------------------------


def _dep_install_path(dep: LockedDependency, apm_modules_dir: Path) -> Path:
    """Compute the filesystem install path for a locked dependency."""
    dep_ref = dep.to_dependency_ref()
    return dep_ref.get_install_path(apm_modules_dir)


def _deployed_path_parts(rel_path: str) -> tuple[str, ...]:
    """Return safe POSIX path parts for a lockfile deployed_files entry."""
    rel = rel_path.replace("\\", "/")
    pure = PurePosixPath(rel)
    if pure.is_absolute() or PureWindowsPath(rel).is_absolute():
        raise ValueError(f"Refusing to pack absolute deployed file path: {rel_path!r}")
    parts = pure.parts
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"Refusing to pack unsafe deployed file path: {rel_path!r}")
    return parts


def _skill_name_from_deployed_parts(parts: tuple[str, ...]) -> str | None:
    """Return the deployed skill name when *parts* point inside a skills dir."""
    if len(parts) >= 3 and parts[0].startswith(".") and parts[1] == "skills":
        return parts[2]
    if len(parts) >= 2 and parts[0] == "skills":
        return parts[1]
    return None


def _plugin_rel_for_deployed_path(
    rel_path: str,
    skill_subset: set[str] | None,
) -> str | None:
    """Map an installed deployed_files path back to plugin-native output layout."""
    parts = _deployed_path_parts(rel_path)
    if not parts:
        return None

    if parts[0] == "skills":
        skill_name = _skill_name_from_deployed_parts(parts)
        if skill_subset and (skill_name is None or skill_name not in skill_subset):
            return None
        plugin_parts = list(parts)
    elif parts[0] in {"agents", "commands", "instructions", "extensions", "hooks"}:
        plugin_parts = list(parts)
    elif len(parts) >= 3 and parts[0].startswith("."):
        skill_name = _skill_name_from_deployed_parts(parts)
        if skill_name is not None:
            if skill_subset and skill_name not in skill_subset:
                return None
            plugin_parts = ["skills", *parts[2:]]
        elif parts[1] == "agents":
            plugin_parts = ["agents", *parts[2:]]
        elif parts[1] in {"commands", "prompts"}:
            plugin_parts = ["commands", *parts[2:]]
        elif parts[1] in {"instructions", "rules", "steering"}:
            plugin_parts = ["instructions", *parts[2:]]
        elif parts[1] == "extensions":
            plugin_parts = ["extensions", *parts[2:]]
        elif parts[1] == "hooks":
            plugin_parts = ["hooks", *parts[2:]]
        else:
            return None
    elif len(parts) >= 2 and parts[0].startswith(".") and parts[1] == "hooks.json":
        plugin_parts = ["hooks.json"]
    else:
        return None

    # Only a .prompt.md rendering renames; a target's native Markdown command
    # or prompt (Claude commands/, Kiro prompts/) keeps its stem.
    native_suffix = deployed_rendering_suffix(PurePosixPath(*parts).as_posix())
    if (
        native_suffix in (None, ".prompt.md")
        and plugin_parts[0] == "commands"
        and plugin_parts[-1].endswith(".prompt.md")
    ):
        plugin_parts[-1] = _rename_prompt(plugin_parts[-1])
    return PurePosixPath(*plugin_parts).as_posix()


def _collect_explicit_local_components(
    project_root: Path,
    includes: list[str],
    *,
    documents: list[tuple[Path, dict]] | None = None,
) -> tuple[list[tuple[Path, str]], dict, bool]:
    """Collect only explicitly listed local paths and validate each path."""
    components: list[tuple[Path, str]] = []
    hooks: dict = {}
    hooks_present = False
    for declared_path in includes:
        parts = _deployed_path_parts(declared_path)
        candidate = project_root.joinpath(*parts)
        if candidate.is_symlink():
            raise ValueError(
                f"includes path {declared_path!r} is a symlink. "
                "Replace it with a regular file or directory."
            )
        try:
            source = ensure_path_within(candidate, project_root)
        except PathTraversalError as exc:
            raise ValueError(
                f"includes path {declared_path!r} escapes the project root. "
                "Fix the path in apm.yml."
            ) from exc
        if not source.exists():
            raise ValueError(
                f"includes path {declared_path!r} does not exist. "
                "Fix the path in apm.yml or create it."
            )
        entries = [source] if source.is_file() else sorted(source.rglob("*"))
        for entry in entries:
            if entry.is_symlink():
                raise ValueError(
                    f"Symlink found inside includes path {declared_path!r}: "
                    f"{entry.name}. Remove the symlink or list a regular path."
                )
        for file_path in (entry for entry in entries if entry.is_file()):
            try:
                file_path = ensure_path_within(file_path, project_root)
            except PathTraversalError as exc:
                raise ValueError(
                    f"A file inside includes path {declared_path!r} escapes the "
                    "project root. Remove the symlink or fix the path in apm.yml."
                ) from exc
            repo_relative = portable_relpath(file_path, project_root)
            is_hook = repo_relative == "hooks.json" or (
                file_path.suffix == ".json"
                and file_path.name != "package.json"
                and file_path.parent in (project_root / "hooks", project_root / ".apm/hooks")
            )
            if is_hook:
                hooks_present = True
                try:
                    hook_data = json.loads(file_path.read_text(encoding="utf-8"))
                except (OSError, ValueError, RecursionError) as exc:
                    raise ValueError(
                        f"Explicit hook include is not valid JSON: {repo_relative}"
                    ) from exc
                if not isinstance(hook_data, dict):
                    raise ValueError(
                        f"Explicit hook include must contain a JSON object: {repo_relative}"
                    )
                if documents is not None:
                    documents.append((file_path, deepcopy(hook_data)))
                else:
                    _deep_merge(hooks, hook_data, overwrite=False)
                continue
            plugin_relative = _plugin_rel_for_deployed_path(repo_relative, None)
            if plugin_relative is None:
                raise ValueError(
                    f"Explicit include path is not a packable primitive: {repo_relative}"
                )
            components.append((file_path, plugin_relative))
    return components, hooks, hooks_present


def _verify_attested_hash(
    source: Path,
    project_root: Path,
    dep: LockedDependency,
) -> None:
    """Fail loudly when a packed deployed file diverges from its attested hash.

    ``deployed_file_hashes`` records the SHA-256 (``"sha256:<hex>"``) of each
    file at install time. Verifying the on-disk copy before packing closes the
    integrity half of the provenance hole: a deployed file that was tampered
    or corrupted after ``apm install`` must never enter the bundle silently.

    Files with no recorded hash (older lockfiles predating
    ``deployed_file_hashes``) are packed without verification -- absence of an
    attestation is forward-compat tolerated, presence of a *mismatched* one is
    a hard error. Delegates to the shared :func:`verify_attested_file` helper so
    the plugin and archive pack paths share one implementation.
    """
    rel = source.relative_to(project_root).as_posix()
    expected = dep.deployed_file_hashes.get(rel) if dep.deployed_file_hashes else None
    verify_attested_file(source, expected, dep.repo_url, rel)


def _append_deployed_component(
    components: list[tuple[Path, str]],
    source: Path,
    output_rel: str,
    seen_outputs: dict[str, set[Path]],
    project_root: Path,
    dep: LockedDependency,
) -> None:
    """Append one deployed component unless it is unsafe or already mapped.

    A repeated output path is dropped, except a further target rendering of a
    primitive (``.claude/commands/x.md`` and ``.github/prompts/x.prompt.md``):
    every rendering stays a candidate for ``_drop_same_path_renderings``
    (#3686).
    """
    if not _validate_output_rel(output_rel):
        return
    seen = seen_outputs.get(output_rel)
    if seen is not None and (
        source in seen
        or deployed_rendering_rank(portable_relpath(source, project_root)) is None
        or any(deployed_rendering_rank(portable_relpath(p, project_root)) is None for p in seen)
    ):
        return
    if source.is_file() and not source.is_symlink():
        _verify_attested_hash(source, project_root, dep)
        components.append((source, output_rel))
        seen_outputs.setdefault(output_rel, set()).add(source)


def _collect_deployed_components(
    project_root: Path,
    dep: LockedDependency,
) -> list[tuple[Path, str]]:
    """Collect dependency components from lockfile deployed_files entries."""
    components: list[tuple[Path, str]] = []
    missing: list[str] = []
    seen_outputs: dict[str, set[Path]] = {}
    skill_subset = skill_subset_filter_tokens(dep.skill_subset)

    for rel_path in dep.deployed_files:
        try:
            plugin_rel = _plugin_rel_for_deployed_path(rel_path, skill_subset)
        except ValueError as exc:
            raise ValueError(f"Cannot pack dependency {dep.repo_url}: {exc}") from exc
        if plugin_rel is None:
            continue
        source = project_root / rel_path
        try:
            source = ensure_path_within(source, project_root)
        except PathTraversalError as exc:
            raise ValueError(
                f"Cannot pack dependency {dep.repo_url}: deployed file path "
                f"escapes the project root: {rel_path!r}"
            ) from exc
        if not source.exists():
            missing.append(rel_path)
            continue
        if source.is_symlink():
            continue
        if source.is_dir():
            for child in sorted(source.rglob("*")):
                if not child.is_file() or child.is_symlink():
                    continue
                # Defense-in-depth: rglob's symlink-following behaviour is
                # Python-version dependent, so re-assert containment on every
                # expanded child rather than trusting the walk. A planted
                # directory symlink whose target escapes ``project_root`` is
                # rejected here even if an intermediate component was a symlink.
                try:
                    ensure_path_within(child, project_root)
                except PathTraversalError:
                    continue
                child_rel = child.relative_to(source).as_posix()
                child_output = (PurePosixPath(plugin_rel) / child_rel).as_posix()
                _append_deployed_component(
                    components, child, child_output, seen_outputs, project_root, dep
                )
        else:
            _append_deployed_component(
                components, source, plugin_rel, seen_outputs, project_root, dep
            )

    if missing:
        shown_missing = missing[:10]
        remaining = len(missing) - len(shown_missing)
        suffix = f"\n  ... and {remaining} more" if remaining else ""
        raise ValueError(
            f"Cannot pack dependency {dep.repo_url}: installed files recorded "
            f"in apm.lock.yaml are missing on disk. {PACK_INSTALLED_FILES_MISSING.sentence()}\n"
            + "\n".join(f"  - {path}" for path in shown_missing)
            + suffix
        )
    return components


def _cache_would_contribute_primitives(install_path: Path, dep: LockedDependency) -> bool:
    """Return True if the unattested apm_modules cache holds packable primitives.

    Used ONLY to decide whether to fail loudly when a locked dependency records
    no ``deployed_files``. This reads the cache to DETECT a provenance gap; it
    never copies cache bytes into the bundle. A dependency that installed
    real components but recorded none of them in the lockfile is a stale or
    partial install -- packing its cache would leak unattested content, so we
    refuse rather than silently pack it.
    """
    if not install_path.is_dir() or _whole_native_plugin_root(dep):
        # A plugin deployed whole is attested by its native_plugin record (#5253).
        return False
    probe = _collect_apm_components(install_path / ".apm")
    probe.extend(_collect_root_plugin_components(install_path))
    _collect_bare_skill(install_path, dep, probe)
    # A canvas the executable gate kept from deploying is not a provenance
    # gap: install recorded it as withheld, and pack omits it (#4714).
    if _gate_parked_types(install_path, dep):
        probe = [item for item in probe if not item[1].startswith("extensions/")]
    return bool(probe)


def _whole_native_plugin_root(dep: LockedDependency) -> str | None:
    """The recorded root of a dependency deployed only as a whole native plugin (#5253)."""
    record = getattr(dep, "native_plugin", None)
    return record.root if record is not None and not dep.deployed_files else None


def _gate_parked_types(install_path: Path, dep: LockedDependency) -> tuple[str, ...]:
    """Executable types the cache ships that the lock records the gate withheld (#4714).

    Of the executable kinds only canvas extensions pack from ``.apm/``; the
    lock row's ``exec_status`` and ``exec_allowed_types`` say whether install
    deployed them.
    """
    from ..security.executables import EXEC_TYPE_CANVAS, TRUST_DENIED, TRUST_GATED

    if dep.exec_status not in (TRUST_GATED, TRUST_DENIED):
        return ()
    if EXEC_TYPE_CANVAS in (dep.exec_allowed_types or ()):
        return ()
    canvas: list[tuple[Path, str]] = []
    _collect_recursive(install_path / ".apm" / "extensions", "extensions", canvas)
    return (EXEC_TYPE_CANVAS,) if canvas else ()


def _warn_parked_executables(
    dep: LockedDependency, install_path: Path, logger=None, *, format_carries: bool = True
) -> None:
    """Name a dependency's gate-parked canvas that pack leaves out, and its remedy (#4714).

    *format_carries* is False for a bundle format that cannot carry canvas
    extensions at all, so approving alone would not ship them.
    """
    from ..install.exec_skip_reasons import withheld_executable_reason

    for exec_type in _gate_parked_types(install_path, dep):
        label = dep.get_unique_key()
        reason = withheld_executable_reason(
            label,
            exec_type,
            "canvas extension(s)",
            denied=_canvas_denied(dep, install_path),
            outcome="not packed",
        )
        message = f"dependency {label}: {reason}"
        if not format_carries:
            message += (
                " The agent-plugin format does not carry canvas extensions;"
                " pack with '--format claude-plugin' to ship them."
            )
        if logger:
            logger.warning(message)
        else:
            _rich_warning(message, symbol="warning")


def _canvas_denied(dep: LockedDependency, install_path: Path) -> bool | None:
    """Whether the lock row's worst-case status is the canvas's own verdict.

    ``exec_status`` is the worst case across executable types: a denied row
    proves the canvas denied only when canvas is the package's one executable
    type; otherwise it is unknown (``None``).
    """
    from ..security.executables import EXEC_TYPE_CANVAS, TRUST_DENIED, scan_package_executables

    if dep.exec_status != TRUST_DENIED:
        return False
    try:
        declared = set(scan_package_executables(install_path, dep.repo_url, "").exec_types)
    except Exception:
        return None
    return True if declared <= {EXEC_TYPE_CANVAS} else None


def _cache_config_kinds(install_path: Path) -> tuple[str, ...]:
    """Return the config kinds (``hooks``, ``MCP``) the unattested cache holds.

    Hooks/MCP *configuration* (``.apm/hooks/*.json``, root ``hooks.json`` /
    ``hooks/``, ``.mcp.json``) is never copied from the cache: plugin pack
    emits only lockfile-attested content. This probe drives a transition
    warning that names the dependency whose cached config will NOT be packed;
    it never copies cache bytes.
    """
    if not install_path.is_dir():
        return ()
    kinds: list[str] = []
    if _collect_hooks_from_apm(install_path / ".apm") or _collect_hooks_from_root(install_path):
        kinds.append("hooks")
    # A dependency's own .mcp.json is authored by that package, not rendered by APM.
    if _collect_mcp(install_path, exclude_apm_managed=False):
        kinds.append("MCP")
    return tuple(kinds)


def _packs_hook_config(components: list[tuple[Path, str]]) -> bool:
    """Whether *components* carry a hooks config file (``hooks.json`` or ``hooks/*.json``)."""
    for _source, rel in components:
        parts = PurePosixPath(rel).parts
        if parts == ("hooks.json",) or (
            len(parts) == 2 and parts[0] == "hooks" and parts[1].endswith(".json")
        ):
            return True
    return False


def _warn_unpacked_dependency_config(
    dep: LockedDependency,
    install_path: Path,
    dep_components: list[tuple[Path, str]],
    logger=None,
) -> None:
    """Name the dependency hooks/MCP config the bundle will not carry (#2013).

    A hooks file the lockfile attests (``deployed_files`` plus its hash) packs
    with the dependency's components, so only hook config that reaches no
    packed hooks file is named, and MCP config, which install merges into
    shared settings and never attests. A plugin deployed whole is named once
    instead: pack leaves its recorded root out (#5253).
    """
    root = _whole_native_plugin_root(dep)
    if root:
        # Its hooks and MCP config load from the plugin root, never from shared settings.
        _emit_pack_warning(
            f"dependency {dep.get_unique_key()}: the native plugin deployed whole under "
            f"{root} is not packed; a bundle carries only files the lockfile records in "
            "deployed_files. A consumer that needs it declares it as its own dependency.",
            logger,
        )
        return
    kinds = [
        kind
        for kind in _cache_config_kinds(install_path)
        if kind != "hooks" or not _packs_hook_config(dep_components)
    ]
    if kinds:
        _emit_pack_warning(
            f"dependency {dep.repo_url} contributed {'/'.join(kinds)} config that "
            "is not attested in apm.lock.yaml; it will NOT be packed. "
            "Attested primitives (skills/agents/etc.) are unaffected.",
            logger,
        )


# ---------------------------------------------------------------------------
# Main exporter
# ---------------------------------------------------------------------------


def _collect_lsp_servers(
    project_root: Path, package: APMPackage, lockfile: LockFile | None
) -> tuple[dict[str, dict], list[str]]:
    """Return the ``.lsp.json`` servers a Claude plugin bundle carries (RA-034).

    Sources, later wins on a name collision (the root package's authored file
    last, as for ``.mcp.json``): servers the lockfile records, declared
    ``dependencies.lsp``, then a root ``.lsp.json``. Every entry is validated
    by ``LSPDependency`` (``command`` and ``extensionToLanguage``), the schema
    plugin admission applies when the bundle is installed. The second value
    names each server with no complete config, so none is dropped silently.
    """
    from ..deps.plugin_parser import lsp_servers_to_apm_deps
    from ..models.dependency.lsp import LSPDependency

    servers: dict[str, dict] = {}
    names: set[str] = set()

    def _admit(config: dict) -> None:
        try:
            dep = LSPDependency.from_dict(config)
        except (ValueError, TypeError):
            return
        servers[dep.name] = dep.to_lsp_json_entry()

    if lockfile is not None:
        names.update(lockfile.lsp_servers or ())
        for name, config in sorted((lockfile.lsp_configs or {}).items()):
            names.add(name)
            if isinstance(config, dict):
                _admit({**config, "name": name})
    for dep in package.get_lsp_dependencies():
        names.add(dep.name)
        if dep.command is not None and dep.extension_to_language is not None:
            _admit(dep.to_dict())
    lsp_json = project_root / ".lsp.json"
    if lsp_json.is_file() and not lsp_json.is_symlink():
        try:
            data = json.loads(lsp_json.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        authored = data.get("lspServers", data) if isinstance(data, dict) else None
        if isinstance(authored, dict):
            names.update(str(name) for name in authored)
            for config in lsp_servers_to_apm_deps(authored, project_root, warn_on_invalid=False):
                _admit(config)
    return dict(sorted(servers.items())), sorted(names - servers.keys())


def _deployed_path(source: Path, project_root: Path, rendered: dict[Path, str]) -> str | None:
    """Return the deployed (project-relative) path a packed source was read from.

    Rules rendered into a pack temp root (*rendered*) keep their deployed path.
    """
    if source in rendered:
        return rendered[source]
    try:
        return portable_relpath(source, project_root)
    except ValueError:
        return None


def _routed_rule_components(
    lockfile, project_root: Path, dependency_keys: set[str], rendered: dict[Path, str]
) -> dict[str, list[tuple[Path, str]]]:
    """Render Claude rules #3939 routed to AGENTS.md as plugin instructions.

    A pre-#3939 install deployed them as ``.claude/rules/`` files, which the
    plugin pack mapped to ``instructions/``; render them through the same
    projection so the bundle keeps those bytes. Only *dependency_keys* (the
    dependencies the bundle ships) render. Keyed by dependency key.
    """
    import atexit
    import tempfile

    from .claude_routed_rules import routed_claude_rules, write_routed_claude_rules

    components: dict[str, list[tuple[Path, str]]] = {}
    routed = routed_claude_rules(lockfile, project_root, dependency_keys=dependency_keys)
    if not routed:
        return components
    rendered_root = Path(tempfile.mkdtemp(prefix="apm-pack-rules-"))
    atexit.register(shutil.rmtree, rendered_root, ignore_errors=True)
    for dep_key, rules in routed.items():
        dep_root = rendered_root / hashlib.sha256(dep_key.encode()).hexdigest()[:16]
        write_routed_claude_rules(dep_root, rules)
        rendered.update({dep_root / rel: rel for rel in rules})
        components[dep_key] = [
            (dep_root / rel, plugin_rel)
            for rel in sorted(rules)
            if (plugin_rel := _plugin_rel_for_deployed_path(rel, None)) is not None
        ]
    return components


def export_plugin_bundle(
    project_root: Path,
    output_dir: Path,
    target: str | None = None,
    archive: bool = False,
    archive_format: str = "zip",
    dry_run: bool = False,
    force: bool = False,
    logger=None,
) -> PackResult:
    """Export the project as a plugin-native directory.

    The output contains only plugin-spec artefacts (``agents/``, ``skills/``,
    ``commands/``, ``plugin.json``, …) with no APM-specific files.

    Args:
        project_root: Root of the project containing ``apm.yml``.
        output_dir: Parent directory for the generated bundle.
        target: Unused for plugin format (reserved for future use).
        archive: If True, produce a ``.zip`` (or ``.tar.gz`` when *archive_format* is ``"tar.gz"``) and remove the directory.
        archive_format: Archive format when *archive* is True -- ``"zip"`` (default) or ``"tar.gz"``.
        dry_run: If True, resolve the file list without writing to disk.
        force: On collision, last writer wins instead of first.

    Returns:
        :class:`PackResult` describing what was produced.
    """
    project_root = project_root.resolve()
    # 1. Read lockfile
    lockfile_path = resolve_lockfile_path_for_read(project_root, read_only=dry_run)
    lockfile = LockFile.read(lockfile_path)

    # 2. Read apm.yml
    apm_yml_path = manifest_file(project_root)
    package = APMPackage.from_apm_yml(apm_yml_path)
    pkg_name = package.name
    pkg_version = package.version or "0.0.0"

    # Guard: reject local-path dependencies (non-portable)
    for dep_ref in package.get_apm_dependencies():
        if dep_ref.is_local:
            raise ValueError(
                f"Cannot pack -- apm.yml contains local path dependency: "
                f"{dep_ref.local_path}\n"
                f"Local dependencies are for development only. Replace them with "
                f"remote references (e.g., 'owner/repo') before packing."
            )

    # 3. Find or synthesize plugin.json
    plugin_json = _find_or_synthesize_plugin_json(
        project_root,
        apm_yml_path,
        suppress_missing_warning=_has_marketplace_block(apm_yml_path),
        logger=logger,
    )
    from ..utils.helpers import find_plugin_json

    authored_manifest = find_plugin_json(project_root)
    plugin_json = _reconcile_plugin_json_version(
        plugin_json,
        pkg_version,
        source_label=(
            portable_relpath(authored_manifest, project_root)
            if authored_manifest is not None
            else "plugin.json"
        ),
        logger=logger,
    )

    # 4. devDependencies filtering
    dev_dep_urls = _get_dev_dependency_urls(apm_yml_path)

    # 5. Collect components -- deps first (lockfile order), then root package
    #    file_map: output_rel_posix -> (source_abs, owner_name)
    file_map: dict[str, tuple[Path, str]] = {}
    collisions: list[str] = []
    dropped_renderings: list[str] = []
    merged_hooks: dict = {}
    merged_mcp: dict = {}

    apm_modules_dir = project_root / "apm_modules"

    # Prefer lockfile is_dev flag (covers transitive deps);
    # fall back to apm.yml URL matching for older lockfiles
    packed_deps = [
        dep
        for dep in (lockfile.get_all_dependencies() if lockfile else [])
        if not (
            getattr(dep, "is_dev", False)
            or (dep.repo_url, getattr(dep, "virtual_path", "") or "") in dev_dep_urls
        )
    ]
    rendered_rules: dict[Path, str] = {}
    routed_rules = _routed_rule_components(
        lockfile, project_root, {dep.get_unique_key() for dep in packed_deps}, rendered_rules
    )
    if lockfile:
        for dep in packed_deps:
            dep_name = dep.repo_url

            # Provenance rule (issue #1999 follow-up): a dependency's content
            # is packed EXCLUSIVELY from what the lockfile attests via
            # ``deployed_files`` (+ per-file ``deployed_file_hashes``).
            # ``apm_modules`` is an unattested cache -- it carries no integrity
            # or provenance guarantee, so no bytes from it may reach the bundle.
            install_path = _dep_install_path(dep, apm_modules_dir)
            _warn_parked_executables(dep, install_path, logger)
            dep_routed = routed_rules.get(dep.get_unique_key(), [])
            if dep.deployed_files:
                dep_components = _collect_deployed_components(project_root, dep)
                if dep.skill_subset and not dep_components:
                    declared_skills = ", ".join(dep.skill_subset)
                    raise ValueError(
                        f"Cannot pack dependency {dep.repo_url}: the skills "
                        f"recorded in apm.lock.yaml (skill_subset: {declared_skills}) "
                        "were not found among its installed files. "
                        f"{PACK_SKILL_SUBSET_MISSING.sentence()}"
                    )
            elif not dep_routed and _cache_would_contribute_primitives(install_path, dep):
                # Unattested primitives sit in the cache but the lockfile
                # recorded none of them: a stale or partial install. Refuse
                # to pack the cache -- fail loudly with a fix.
                raise ValueError(
                    f"Cannot pack dependency {dep.repo_url}: the lockfile records "
                    "no deployed files for it, but installed content that cannot "
                    "be verified exists in the apm_modules cache (a stale or "
                    f"partial install). {PACK_DEPENDENCY_UNATTESTED.sentence()}"
                )
            else:
                # No attested content and nothing packable in the cache: the
                # dependency contributes no plugin primitives (e.g. an
                # MCP-only or hooks-config-only package). Skip it cleanly.
                dep_components = []

            # Transition warning (#2013): name dependency hooks/MCP config the
            # bundle will not carry, so an author who relied on it merging into
            # shared settings is not surprised by the exclusion.
            _warn_unpacked_dependency_config(dep, install_path, dep_components, logger)

            dep_components = _drop_duplicate_renderings(
                dep_components + dep_routed, project_root, rendered_rules, dropped_renderings
            )
            _merge_file_map(file_map, dep_components, dep_name, force, collisions)

    # 6. Collect own components according to the local source authority.
    from ..core.plugin_manifest import PLUGIN_ECOSYSTEM_PATHS
    from .plugin_hooks import PluginHookResources

    own_apm_dir = project_root / ".apm"
    hook_documents: list[tuple[Path, dict]] = []
    if isinstance(package.includes, list):
        own_components, root_hooks, _hooks_present = _collect_explicit_local_components(
            project_root,
            package.includes,
            documents=hook_documents,
        )
    else:
        skipped_links: list[Path] = []
        own_components = _collect_apm_components(own_apm_dir, skipped_links=skipped_links)
        root_hooks = _collect_hooks_from_apm(own_apm_dir, documents=hook_documents)
        if own_apm_dir.is_dir():
            _warn_skipped_root_components(project_root, logger)
        else:
            own_components.extend(
                _collect_root_plugin_components(project_root, skipped_links=skipped_links)
            )
            root_hooks_top = _collect_hooks_from_root(project_root, documents=hook_documents)
            _deep_merge(root_hooks, root_hooks_top, overwrite=False)
        _warn_skipped_source_links(project_root, skipped_links, logger)

    hook_resources = PluginHookResources(
        project_root,
        own_components,
        includes=package.includes if isinstance(package.includes, list) else None,
        descriptors={path for path, _data in hook_documents},
        output_path=lambda rel: _plugin_rel_for_deployed_path(rel, None),
    )
    root_hooks = {}
    for descriptor, data in hook_documents:
        _deep_merge(root_hooks, hook_resources.rewrite(data, descriptor), overwrite=False)

    if (
        not isinstance(package.includes, list)
        and own_apm_dir.is_dir()
        and not own_components
        and not root_hooks
    ):
        _warn_no_local_primitives(logger)

    own_components = _drop_duplicate_renderings(
        own_components, project_root, rendered_rules, dropped_renderings
    )
    _merge_file_map(file_map, own_components, pkg_name, force, collisions)
    hook_resources.validate_outputs(
        file_map,
        reserved={
            "plugin.json",
            "hooks.json",
            ".mcp.json",
            ".lsp.json",
            "apm.lock.yaml",
            PLUGIN_ECOSYSTEM_PATHS["claude"],
        },
    )

    # Hooks -- root package wins on key collision
    _deep_merge(merged_hooks, root_hooks, overwrite=True)

    # MCP -- root package wins on server-name collision
    root_mcp = _collect_mcp(project_root)
    _deep_merge(merged_mcp, root_mcp, overwrite=True)

    # 6b. LSP -- Claude plugins carry LSP servers in a root .lsp.json; name
    #     each server without a complete config instead of dropping it (I8).
    merged_lsp, unpacked_lsp = _collect_lsp_servers(project_root, package, lockfile)
    if unpacked_lsp:
        message = (
            "LSP servers without a complete config (command and extensionToLanguage) "
            f"are not packed: {', '.join(unpacked_lsp)}. Add both fields to each one in "
            "apm.yml dependencies.lsp or .lsp.json, then pack again."
        )
        if logger:
            logger.warning(message)
        else:
            _rich_warning(message, symbol="warning")

    # 7. Name the dropped renderings, then emit collision warnings
    _report_dropped_renderings(dropped_renderings, logger)
    for msg in collisions:
        if logger:
            logger.warning(msg)
        else:
            _rich_warning(msg)

    # 8. Build output file list (sorted for determinism)
    refuse_credential_files(file_map)
    output_files = sorted(file_map.keys())

    # Add generated files to the list
    if merged_hooks:
        output_files.append("hooks.json")
        output_files.append(PLUGIN_ECOSYSTEM_PATHS["claude"])
        plugin_json["hooks"] = "./hooks.json"
    output_files.extend(hook_resources.generated)
    if merged_mcp:
        output_files.append(".mcp.json")
    if merged_lsp:
        output_files.append(".lsp.json")
    output_files.append("plugin.json")

    # 9. Dry run -- return file list without writing
    safe_name = _sanitize_bundle_name(pkg_name)
    safe_version = _sanitize_bundle_name(pkg_version)
    bundle_dir = output_dir / f"{safe_name}-{safe_version}"
    ensure_path_within(bundle_dir, output_dir)
    if dry_run:
        bundle_path = (
            projected_archive_path(output_dir, bundle_dir.name, archive_format)
            if archive
            else bundle_dir
        )
        return PackResult(bundle_path=bundle_path, files=output_files)

    # 10. Security scan (warn-only, never blocks)
    _scan_bundle_sources(file_map, logger)

    # 11. Write files to output directory (clean slate to prevent symlink attacks)
    _write_bundle_sources(file_map, bundle_dir, output_dir)

    # 12. Write merged hooks.json
    if merged_hooks:
        write_text_lf(bundle_dir / "hooks.json", json.dumps(merged_hooks, indent=2, sort_keys=True))
    for relative, content in hook_resources.generated.items():
        destination = bundle_dir / relative
        ensure_path_within(destination, bundle_dir)
        write_text_lf(destination, content)

    # 13. Write merged .mcp.json
    if merged_mcp:
        write_text_lf(
            bundle_dir / ".mcp.json",
            json.dumps({"mcpServers": merged_mcp}, indent=2, sort_keys=True),
        )

    # 13b. Write merged .lsp.json (flat server map, Claude plugin layout)
    if merged_lsp:
        write_text_lf(bundle_dir / ".lsp.json", json.dumps(merged_lsp, indent=2, sort_keys=True))

    # 14. Write plugin.json with updated component paths
    plugin_json = _update_plugin_json_paths(plugin_json, output_files, logger=logger)
    write_text_lf(bundle_dir / "plugin.json", json.dumps(plugin_json, indent=2, sort_keys=False))
    if merged_hooks:
        native_manifest = bundle_dir / PLUGIN_ECOSYSTEM_PATHS["claude"]
        ensure_path_within(native_manifest, bundle_dir)
        native_manifest.parent.mkdir(parents=True, exist_ok=True)
        write_text_lf(native_manifest, json.dumps(plugin_json, indent=2, sort_keys=False))

    # 14b. Write enriched lockfile with bundle_files manifest (issue #1098).
    # Walk the bundle and hash every file (excluding the lockfile itself,
    # which we are about to write) so install-time integrity verification can
    # detect tampering without needing the original deployed_files map.
    # A project without apm.lock.yaml (e.g. ``dependencies: {}`` with only
    # local primitives) still gets an integrity manifest: synthesize an empty
    # lockfile so ``pack.bundle_files`` is always recorded (issue #3846).
    if lockfile is None:
        lockfile = LockFile(apm_version=installed_apm_version())
    from .lockfile_enrichment import enrich_lockfile_for_pack

    bundle_files: dict[str, str] = {}
    for fp in bundle_dir.rglob("*"):
        if not fp.is_file() or fp.is_symlink():
            continue
        rel = fp.relative_to(bundle_dir).as_posix()
        if rel == "apm.lock.yaml":
            continue
        bundle_files[rel] = hashlib.sha256(fp.read_bytes()).hexdigest()
    # Issue #1207 D1: do NOT silently substitute ``"copilot"`` when
    # ``target`` is missing.  Bundles are target-agnostic at install
    # time; ``pack.target`` is recorded as informational metadata only.
    # Falling back to ``"all"`` preserves the lockfile-filter shape
    # (which uses target prefixes to narrow each dep's deployed_files
    # list to the union of supported targets) without locking the
    # bundle to a single client.
    enriched_yaml = enrich_lockfile_for_pack(
        lockfile,
        "plugin",
        target or "all",
        bundle_files=bundle_files,
        renderings=_packed_renderings(file_map, project_root, rendered_rules),
    )
    write_text_lf(bundle_dir / "apm.lock.yaml", enriched_yaml)

    result = PackResult(bundle_path=bundle_dir, files=output_files)

    # 15. Archive if requested
    if archive:
        validate_archive_format(archive_format)
        archive_path = projected_archive_path(output_dir, bundle_dir.name, archive_format)
        if archive_format == "tar.gz":
            write_tar_archive(bundle_dir, archive_path)
        else:
            write_zip_archive(bundle_dir, archive_path)
        shutil.rmtree(bundle_dir)
        result.bundle_path = archive_path

    return result


# ---------------------------------------------------------------------------
# Rendering selection and collision handling
# ---------------------------------------------------------------------------


def _drop_duplicate_renderings(
    components: list[tuple[Path, str]],
    project_root: Path,
    rendered: dict[Path, str],
    dropped: list[str],
) -> list[tuple[Path, str]]:
    """Keep one rendering per primitive of one package (#3686).

    A multi-target install deploys a primitive once per target (for example
    ``.claude/agents/rv.md`` and ``.github/agents/rv.agent.md``); the bundle
    carries only the rendering :mod:`.plugin_layout` prefers, and each dropped
    one is named by its deployed path. Distinct primitives that share a plugin
    path stay a collision for :func:`_merge_file_map` to report.
    """
    deployed = [_deployed_path(source, project_root, rendered) for source, _rel in components]
    superseded = superseded_plugin_renderings(deployed)
    dropped.extend(deployed[index] or "" for index in sorted(superseded))
    return [component for index, component in enumerate(components) if index not in superseded]


def _packed_renderings(
    file_map: dict[str, tuple[Path, str]], project_root: Path, rendered: dict[Path, str]
) -> dict[str, str]:
    """Record the deploying target's extension of every packed file (#3686).

    Taken from the final *file_map* winner, after rendering deduplication and
    collision resolution, so a consumer never lowers a file by the provenance
    of a dropped or displaced one. Own sources carry no entry.
    """
    renderings: dict[str, str] = {}
    for rel, (source, _owner) in file_map.items():
        deployed = _deployed_path(source, project_root, rendered)
        if deployed and (extension := deployed_rendering_extension(deployed)):
            renderings[rel] = extension
    return renderings


def _report_dropped_renderings(dropped: list[str], logger=None) -> None:
    """Name every per-target duplicate rendering pack left out, on one line."""
    if not dropped:
        return
    message = "Packed one rendering per primitive; dropped per-target duplicates: " + ", ".join(
        dict.fromkeys(dropped)
    )
    if logger:
        logger.info(message)
    else:
        _rich_info(message, symbol="info")


def _merge_file_map(
    file_map: dict[str, tuple[Path, str]],
    components: list[tuple[Path, str]],
    owner: str,
    force: bool,
    collisions: list[str],
) -> None:
    """Merge *components* into *file_map* with collision handling.

    Without ``--force``: first writer wins (skip with warning).
    With ``--force``: last writer wins (overwrite with warning).
    """
    for source, output_rel in components:
        if not _validate_output_rel(output_rel):
            continue
        if output_rel in file_map:
            existing_owner = file_map[output_rel][1]
            collisions.append(
                f"{output_rel} -- collision between '{existing_owner}' and "
                f"'{owner}' ({'last writer wins' if force else 'first writer wins'})"
            )
            if force:
                file_map[output_rel] = (source, owner)
            # else: first writer wins, skip
        else:
            file_map[output_rel] = (source, owner)
