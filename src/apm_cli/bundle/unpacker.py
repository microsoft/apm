"""Bundle unpacker  -- extracts and verifies APM bundles."""

import functools
import os
import shutil
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..core.remediation import UNPACK_CRITICAL_FINDINGS
from ..deps.lockfile import LEGACY_LOCKFILE_NAME, LOCKFILE_NAME, LockFile
from ..utils.archive import (
    MAX_ZIP_ENTRIES,
    MAX_ZIP_UNCOMPRESSED,
    ArchiveError,
    _extract_tar_gz_file,
    safe_extract_zip,
)

_MAX_ZIP_ENTRIES = MAX_ZIP_ENTRIES
_MAX_ZIP_UNCOMPRESSED = MAX_ZIP_UNCOMPRESSED


@dataclass
class UnpackResult:
    """Result of an unpack operation."""

    extracted_dir: Path
    files: list[str] = field(default_factory=list)
    verified: bool = False
    dependency_files: dict[str, list[str]] = field(default_factory=dict)
    skipped_count: int = 0
    security_warnings: int = 0
    security_critical: int = 0
    canvas_blocked: int = 0
    pack_meta: dict = field(default_factory=dict)
    written_files: list[str] = field(default_factory=list)
    """Regular files this unpack wrote, relative to the output directory."""


@dataclass(frozen=True)
class UnpackCandidate:
    """A parsed bundle awaiting admission, before anything is written."""

    source_dir: Path
    lockfile: LockFile
    files: list[str]
    pack_meta: dict


def unpack_bundle(
    bundle_path: Path,
    output_dir: Path = Path("."),
    skip_verify: bool = False,
    dry_run: bool = False,
    force: bool = False,
    *,
    admit: Callable[[UnpackCandidate], None],
) -> UnpackResult:
    """Extract and apply an APM bundle to a project directory.

    Additive-only semantics (v1): only writes files listed in the bundle's
    lockfile ``deployed_files``.  Never deletes existing files.  If a local
    file has the same name as a bundle file, the bundle file wins (overwrite).

    Args:
        bundle_path: Path to a ``.zip`` (or legacy ``.tar.gz``) archive, or an unpacked bundle directory.
        output_dir: Target project directory to copy files into.
        skip_verify: If *True*, skip completeness verification against the lockfile.
        dry_run: If *True*, resolve the file list but write nothing to disk.
        force: If *True*, deploy even when critical hidden characters are found.
        admit: Bundle admission (org policy and integrity) run before a
            preview or a write; it raises to refuse the bundle.

    Returns:
        :class:`UnpackResult` describing what was (or would be) extracted.

    Raises:
        FileNotFoundError: If the bundle's ``apm.lock.yaml`` is missing.
        ValueError: If verification finds files listed in the lockfile but
            absent from the bundle.
    """
    # 1. If archive, extract to temp dir
    cleanup_temp = False
    if bundle_path.is_file() and bundle_path.name.endswith(".zip"):
        from ..config import get_apm_temp_dir

        temp_dir = Path(tempfile.mkdtemp(prefix="apm-unpack-", dir=get_apm_temp_dir()))
        cleanup_temp = True
        try:
            with zipfile.ZipFile(bundle_path, "r") as zf:
                safe_extract_zip(
                    zf,
                    temp_dir,
                    max_entries=_MAX_ZIP_ENTRIES,
                    max_uncompressed=_MAX_ZIP_UNCOMPRESSED,
                    error_type=ValueError,
                )
        except Exception:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise

        # Locate inner directory (the archive wraps a single top-level dir)
        children = list(temp_dir.iterdir())
        if len(children) == 1 and children[0].is_dir():  # noqa: SIM108
            source_dir = children[0]
        else:
            source_dir = temp_dir
    elif bundle_path.is_file() and bundle_path.name.endswith(".tar.gz"):
        # Legacy .tar.gz support (backward compat)
        from ..config import get_apm_temp_dir

        temp_dir = Path(tempfile.mkdtemp(prefix="apm-unpack-", dir=get_apm_temp_dir()))
        cleanup_temp = True
        try:
            _extract_tar_gz_file(bundle_path, str(temp_dir))
        except ArchiveError as exc:
            shutil.rmtree(temp_dir, ignore_errors=True)
            msg = str(exc)
            if "path" in msg or "Symlinks" in msg or "links" in msg:
                raise ValueError(f"Refusing to extract path-traversal entry: {msg}") from exc
            raise ValueError(msg) from exc
        except Exception:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise

        # Locate inner directory (the archive wraps a single top-level dir)
        children = list(temp_dir.iterdir())
        if len(children) == 1 and children[0].is_dir():  # noqa: SIM108
            source_dir = children[0]
        else:
            source_dir = temp_dir
    elif bundle_path.is_dir():
        source_dir = bundle_path
        temp_dir = None
    else:
        raise FileNotFoundError(f"Bundle not found or unsupported format: {bundle_path}")

    try:
        # 2. Read apm.lock.yaml (or legacy apm.lock) from bundle
        lockfile_path = source_dir / LOCKFILE_NAME
        if not lockfile_path.exists():
            # Backward compat: older bundles used "apm.lock"
            legacy_lockfile_path = source_dir / LEGACY_LOCKFILE_NAME
            if legacy_lockfile_path.exists():
                lockfile_path = legacy_lockfile_path

        # Extract pack: metadata (written by apm pack) before structured parse
        pack_meta: dict = {}
        try:
            from ..utils.yaml_io import load_yaml

            # Bounded loader: an untrusted bundle lockfile cannot hang the
            # unpacker with a merge-key bomb (the except below cannot catch a
            # non-terminating safe_load loop).
            raw = load_yaml(lockfile_path)
            if isinstance(raw, dict):
                val = raw.get("pack", {})
                pack_meta = val if isinstance(val, dict) else {}
        except Exception:
            pass  # non-critical -- proceed without metadata

        lockfile = LockFile.read(lockfile_path)
        if lockfile is None:
            if not lockfile_path.exists():
                raise FileNotFoundError(
                    f"{lockfile_path.name} not found in the bundle  -- the bundle may be incomplete."
                )
            raise FileNotFoundError(
                f"{lockfile_path.name} in the bundle could not be parsed  -- the bundle may be corrupt."
            )

        # Collect deployed_files per dependency and deduplicated global list
        dep_file_map: dict[str, list[str]] = {}
        seen: set[str] = set()
        unique_files: list[str] = []
        for dep in lockfile.get_all_dependencies():
            dep_key = dep.get_unique_key()
            dep_files: list[str] = []
            for f in dep.deployed_files:
                dep_files.append(f)
                if f not in seen:
                    seen.add(f)
                    unique_files.append(f)
            if dep_files:
                dep_file_map[dep_key] = dep_files

        # Feature gate: canvas extensions are executable Node bundles
        # (``extension.mjs``). With the canvas flag OFF their paths are
        # dropped and counted; with it ON they are refused below with the
        # rest of the bundle's executable content.
        canvas_blocked = 0
        from ..core.experimental import is_enabled
        from ..integration.canvas_integrator import is_canvas_bundle_path

        if not is_enabled("canvas"):
            _blocked = {f for f in unique_files if is_canvas_bundle_path(f)}
            if _blocked:
                canvas_blocked = len(_blocked)
                unique_files = [f for f in unique_files if f not in _blocked]
                for _k in list(dep_file_map):
                    dep_file_map[_k] = [f for f in dep_file_map[_k] if f not in _blocked]
                    if not dep_file_map[_k]:
                        del dep_file_map[_k]

        # 3. Verify completeness. A plugin bundle (root plugin.json, the
        # default 'apm pack' output) is not a deployment layout (#3994).
        if (source_dir / "plugin.json").is_file():
            raise ValueError(
                "This is a plugin-format bundle (the default 'apm pack' output); "
                "install it with 'apm install <bundle-path>'. 'apm unpack' reads only "
                "bundles packed with '--format apm'."
            )
        # Executable content (hooks, hook scripts, MCP/LSP config, bin,
        # canvas extensions) deploys only through ``apm install <bundle>``,
        # which applies the executables gate, org policy and the lockfile.
        _refuse_executable_content(unique_files, source_dir, bundle_path)
        verified = True
        if not skip_verify:
            missing = [f for f in unique_files if not (source_dir / f).exists()]
            if missing:
                raise ValueError(
                    "Bundle verification failed  -- the following deployed files "
                    "are missing from the bundle:\n" + "\n".join(f"  - {m}" for m in missing)
                )

        if skip_verify:
            verified = False

        # 3b. Security scan: check bundle contents for hidden Unicode characters
        from ..security.gate import BLOCK_POLICY, SecurityGate

        # Scan all files under source_dir (SecurityGate handles symlink
        # skipping, directory recursion, and OSError resilience)
        verdict = SecurityGate.scan_files(source_dir, policy=BLOCK_POLICY, force=force)
        security_warnings = verdict.warning_count
        security_critical = verdict.critical_count

        if verdict.should_block:
            affected = []
            for path, findings in verdict.findings_by_file.items():
                c = sum(1 for f in findings if f.severity == "critical")
                if c > 0:
                    affected.append(f"  {path}  ({c} critical)")
            raise ValueError(
                f"Blocked: bundle contains {len(affected)} file(s) "
                f"with critical hidden characters\n\n"
                f"Affected files:\n" + "\n".join(affected) + "\n\n"
                # Review, never bypass: no --force hint (RA-008).
                f"Next step: {UNPACK_CRITICAL_FINDINGS.sentence()}\n\n"
                "Learn more: https://apm.github.io/apm/enterprise/security/"
            )

        # 3c. Admission: org policy and integrity, before a preview or a write.
        admit(
            UnpackCandidate(
                source_dir=source_dir,
                lockfile=lockfile,
                files=_bundle_file_paths(unique_files, source_dir),
                pack_meta=pack_meta,
            )
        )

        # Dry-run: return file list without writing
        if dry_run:
            return UnpackResult(
                extracted_dir=bundle_path,
                files=unique_files,
                verified=verified,
                dependency_files=dep_file_map,
                security_warnings=security_warnings,
                security_critical=security_critical,
                canvas_blocked=canvas_blocked,
                pack_meta=pack_meta,
            )

        # 4. Copy target files to output_dir (additive, no deletes).
        # Every destination is validated before the first write.
        from ..utils.path_security import has_symlink_component

        output_dir = Path(output_dir)
        destinations = _validated_unpack_destinations(unique_files, source_dir, output_dir)
        skipped = 0
        written_paths: list[Path] = []

        def _copy_file(src_file: str, dst_file: str) -> str:
            result = shutil.copy2(src_file, dst_file, follow_symlinks=False)
            written_paths.append(Path(dst_file))
            return result

        for rel_path, dest in destinations:
            src = source_dir / rel_path
            if has_symlink_component(source_dir, src):
                # Never read through a linked bundle path.
                skipped += 1
                continue
            if not src.exists():
                skipped += 1
                continue  # skip_verify may allow missing files
            if src.is_dir():
                from ..security.gate import ignore_non_content

                shutil.copytree(
                    src,
                    dest,
                    dirs_exist_ok=True,
                    ignore=ignore_non_content,
                    copy_function=_copy_file,
                )
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                _copy_file(str(src), str(dest))
        # Only files this unpack copied; skipped and pre-existing files stay unclaimed.
        written = list(
            dict.fromkeys(
                path.relative_to(output_dir).as_posix()
                for path in written_paths
                if path.is_file() and not path.is_symlink()
            )
        )

        return UnpackResult(
            extracted_dir=bundle_path,
            files=unique_files,
            verified=verified,
            dependency_files=dep_file_map,
            skipped_count=skipped,
            security_warnings=security_warnings,
            security_critical=security_critical,
            canvas_blocked=canvas_blocked,
            pack_meta=pack_meta,
            written_files=written,
        )
    finally:
        # Clean up temp dir if we created one
        if cleanup_temp and temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)


def _validated_unpack_destinations(
    rel_paths: list[str], source_dir: Path, output_dir: Path
) -> list[tuple[str, Path]]:
    """Return ``(rel_path, destination)`` pairs, refusing unmanaged destinations.

    Bundle files land only under known integration directories of
    *output_dir*, never through a symlinked path component. Directory
    entries are checked file by file, since their copy merges into any
    existing destination tree; nested directories (including empty ones)
    are checked as well.
    """
    from ..integration.base_integrator import BaseIntegrator
    from ..integration.targets import get_integration_prefixes
    from ..utils.path_security import has_symlink_component

    output_dir_resolved = output_dir.resolve()
    allowed_prefixes = tuple(p for p in get_integration_prefixes() if "://" not in p)
    destinations: list[tuple[str, Path]] = []
    for rel_path in rel_paths:
        p = Path(rel_path)
        if p.is_absolute() or rel_path.startswith("/") or ".." in p.parts:
            raise ValueError(f"Refusing to unpack unsafe path from bundle lockfile: {rel_path!r}")
        dest = output_dir / rel_path
        if dest.is_symlink() or not BaseIntegrator.validate_deploy_path(
            rel_path,
            output_dir,
            allowed_prefixes=allowed_prefixes,
            resolved_project_root=output_dir_resolved,
        ):
            raise ValueError(
                "Refusing to unpack path outside managed integration directories "
                f"of the output directory: {rel_path!r}"
            )
        src = source_dir / rel_path
        if src.is_dir() and not has_symlink_component(source_dir, src):
            for walk_root, dirs, files in os.walk(src):
                for name in (*dirs, *files):
                    dest_file = dest / Path(walk_root).relative_to(src) / name
                    if has_symlink_component(output_dir, dest_file):
                        raise ValueError(
                            "Refusing to unpack through a symlinked path in the output "
                            f"directory: {rel_path!r}"
                        )
        destinations.append((rel_path, dest))
    return destinations


# Basenames that only name hook, MCP or LSP server configuration, at any depth
# (``.kiro/settings/mcp.json``, ``.agents/mcp_config.json``).
_EXECUTABLE_CONFIG_NAMES = frozenset(
    {
        "hooks.json",
        "apm-hooks.json",
        "mcp.json",
        ".mcp.json",
        "mcp_config.json",
        "lsp.json",
        ".lsp.json",
        "lsp-config.json",
        "opencode.json",
        "opencode.jsonc",
    }
)
# Client settings files that can register hooks or servers; they sit at the
# project root or directly under a client root (``.claude/settings.json``).
_CLIENT_SETTINGS_NAMES = frozenset(
    {
        "settings.json",
        "settings.local.json",
        "config.toml",
        "config.yaml",
        ".claude.json",
    }
)
# Directory segments whose files are hook resources or bin executables.
_EXECUTABLE_DIR_NAMES = frozenset({"hooks", "bin"})


@functools.lru_cache(maxsize=1)
def _lsp_config_paths() -> frozenset[str]:
    """Project-scope LSP config paths, as the LSP integrator writes them."""
    from ..integration.lsp_integrator import _LSP_TARGET_SPECS

    return frozenset(
        "/".join(spec.project_relative_path).lower() for spec in _LSP_TARGET_SPECS.values()
    )


@functools.lru_cache(maxsize=1)
def _plugin_manifest_paths() -> tuple[str, ...]:
    """Plugin manifest paths; a manifest can declare hooks, MCP and LSP servers."""
    from ..core.plugin_manifest import PLUGIN_ECOSYSTEM_PATHS

    return tuple(path.lower() for path in PLUGIN_ECOSYSTEM_PATHS.values())


def executable_content_kind(rel_path: str) -> str | None:
    """Return why a bundle path is executable content, or ``None``."""
    from ..integration.canvas_integrator import is_canvas_bundle_path

    parts = [seg for seg in rel_path.replace("\\", "/").split("/") if seg]
    if not parts:
        return None
    if is_canvas_bundle_path(rel_path):
        return "canvas extension"
    name = parts[-1].lower()
    joined = "/".join(parts).lower()
    if (
        name in _EXECUTABLE_CONFIG_NAMES
        or (len(parts) <= 2 and name in _CLIENT_SETTINGS_NAMES)
        or joined in _lsp_config_paths()
        or any(joined == m or joined.endswith("/" + m) for m in _plugin_manifest_paths())
    ):
        return "hook, MCP or LSP configuration"
    for seg in parts[:-1]:
        if seg.lower() in _EXECUTABLE_DIR_NAMES:
            return "hook resource" if seg.lower() == "hooks" else "bin executable"
    return None


def _bundle_file_paths(rel_paths: list[str], source_dir: Path) -> list[str]:
    """Expand directory entries into the files a copy would write."""
    expanded: dict[str, None] = {}
    for rel in rel_paths:
        expanded[rel] = None
        src = source_dir / rel
        if src.is_dir() and not src.is_symlink():
            for walk_root, _dirs, files in os.walk(src):
                base = Path(rel) / Path(walk_root).relative_to(src)
                expanded.update(dict.fromkeys((base / name).as_posix() for name in files))
    return list(expanded)


def _refuse_executable_content(rel_paths: list[str], source_dir: Path, bundle_path: Path) -> None:
    """Refuse a bundle that carries executable content.

    ``apm unpack`` copies files verbatim, so it cannot apply the executables
    gate, org policy or a lockfile record; ``apm install <bundle>`` does.
    """
    refused = [
        (rel, kind)
        for rel in _bundle_file_paths(rel_paths, source_dir)
        if (kind := executable_content_kind(rel))
    ]
    if not refused:
        return
    listed = "\n".join(f"  - {rel}  ({kind})" for rel, kind in refused[:10])
    more = f"\n  ... and {len(refused) - 10} more" if len(refused) > 10 else ""
    raise ValueError(
        f"Refusing to unpack {len(refused)} executable file(s); 'apm unpack' cannot "
        f"apply the executables gate or org policy:\n{listed}{more}\n\n"
        f"Install the bundle instead: apm install {bundle_path}\n"
        "(repack a legacy '--format apm' bundle with 'apm pack --archive' first)"
    )
