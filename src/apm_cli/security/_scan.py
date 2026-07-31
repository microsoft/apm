"""Filesystem scanner for executable primitives declared by APM packages.

Extracted from ``executables.py`` to keep that facade under the 800-line
guardrail. ``scan_package_executables`` is re-exported from ``executables.py``
so callers see no change.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .executables import ExecutableDeclaration


def scan_package_executables(
    install_path: Path,
    package_name: str,
    package_version: str,
    *,
    is_transitive: bool = False,
    parent_name: str | None = None,
) -> ExecutableDeclaration:
    """Scan a materialised package directory for executable primitives.

    Checks for:
    - ``.apm/hooks/*.json`` and ``hooks/*.json`` -- hook definitions
      (mirrors :meth:`HookIntegrator.find_hook_files`)
    - ``bin/`` directory -- bin executables
    - MCP is declared in the package's ``apm.yml`` under
      ``dependencies.mcp``, not as files -- so we parse that instead.
    - ``.apm/extensions/<name>/extension.mjs`` -- canvas extension bundles
      (mirrors :meth:`CanvasIntegrator.find_canvas_bundles`)

    Returns an :class:`ExecutableDeclaration` (may have zero counts if
    the package declares no executables).
    """
    from .executables import ExecutableDeclaration, build_approval_key

    key = build_approval_key(package_name, package_version)

    # 1. Hooks: .apm/hooks/*.json and hooks/*.json (aligned with
    #    HookIntegrator.find_hook_files -- only JSON files are actionable).
    hook_files: list[Path] = []
    for hook_dir in [install_path / ".apm" / "hooks", install_path / "hooks"]:
        if hook_dir.is_dir():
            hook_files.extend(
                sorted(f for f in hook_dir.glob("*.json") if f.is_file() and not f.is_symlink())
            )
    hook_details = [f.name for f in hook_files]

    # 2. Bin executables: top-level bin/ AND .apm/skills/*/bin/
    bin_files: list[Path] = []
    for bin_dir in [install_path / "bin"]:
        if bin_dir.is_dir():
            bin_files.extend(
                f for f in bin_dir.iterdir() if f.is_file() and not f.name.startswith(".")
            )
    # Also scan skill-level bin/ directories
    apm_skills = install_path / ".apm" / "skills"
    if apm_skills.is_dir():
        for skill_dir in apm_skills.iterdir():
            skill_bin = skill_dir / "bin"
            if skill_bin.is_dir():
                bin_files.extend(
                    f for f in skill_bin.iterdir() if f.is_file() and not f.name.startswith(".")
                )
    bin_files = sorted(set(bin_files))
    bin_details = [f.name for f in bin_files]

    # 3. MCP servers: parse from apm.yml dependencies.mcp
    mcp_count = 0
    mcp_details: list[str] = []
    apm_yml = install_path / "apm.yml"
    if apm_yml.is_file():
        try:
            from ..utils.yaml_io import load_yaml

            data = load_yaml(apm_yml)
            if isinstance(data, dict):
                deps = data.get("dependencies", {})
                if isinstance(deps, dict):
                    mcp_list = deps.get("mcp", [])
                    if isinstance(mcp_list, list):
                        mcp_count = len(mcp_list)
                        for entry in mcp_list:
                            if isinstance(entry, str):
                                mcp_details.append(entry)
                            elif isinstance(entry, dict):
                                mcp_details.append(entry.get("name", str(entry)))
        except Exception:
            pass  # Non-fatal: if we cannot parse, treat as zero MCP

    # 4. Canvas extensions: .apm/extensions/<name>/extension.mjs
    #    Mirrors CanvasIntegrator.find_canvas_bundles marker detection.
    canvas_marker = "extension.mjs"
    canvas_dirs: list[Path] = []
    extensions_root = install_path / ".apm" / "extensions"
    if extensions_root.is_dir():
        for ext_dir in extensions_root.iterdir():
            if ext_dir.is_dir() and (ext_dir / canvas_marker).is_file():
                canvas_dirs.append(ext_dir)
    canvas_dirs = sorted(canvas_dirs)
    canvas_details = [d.name for d in canvas_dirs]

    return ExecutableDeclaration(
        package_key=key,
        package_name=package_name,
        is_transitive=is_transitive,
        parent_name=parent_name,
        hook_count=len(hook_files),
        mcp_count=mcp_count,
        bin_count=len(bin_files),
        canvas_count=len(canvas_dirs),
        hook_details=hook_details,
        mcp_details=mcp_details,
        bin_details=bin_details,
        canvas_details=canvas_details,
    )
