"""Executable primitive approval gate (npm v12-inspired opt-in model).

APM packages can declare four kinds of executable primitives -- hooks,
MCP servers, bin/ executables, and canvas extensions -- that run arbitrary
code on the developer's machine.  When the consuming project declares an
``allowExecutables`` block in its ``apm.yml``, this module enforces a
deny-by-default policy: none of these primitives are deployed unless
explicitly approved.  Projects that omit the block entirely get
backward-compatible behaviour (all executables deployed).

The design mirrors npm v12's ``allowScripts`` (shipping July 2026):
version-pinned per-package approval, interactive prompts at install
time, and hard errors in non-interactive (CI) environments.

See also: ``apm approve`` / ``apm deny`` CLI commands.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Executable type constants used as keys in the allowExecutables block.
EXEC_TYPE_HOOKS = "hooks"
EXEC_TYPE_MCP = "mcp"
EXEC_TYPE_BIN = "bin"
EXEC_TYPE_CANVAS = "canvas"

# Types with active enforcement in the install gate.
ENFORCED_EXEC_TYPES = (EXEC_TYPE_HOOKS, EXEC_TYPE_BIN, EXEC_TYPE_MCP, EXEC_TYPE_CANVAS)

# All recognised exec-type keys (for manifest validation).
ALL_EXEC_TYPES = (EXEC_TYPE_HOOKS, EXEC_TYPE_MCP, EXEC_TYPE_BIN, EXEC_TYPE_CANVAS)


@dataclass(frozen=True)
class ExecutableDeclaration:
    """Describes the executable primitives declared by a single package.

    Attributes:
        package_key: Approval key for this package (e.g. ``owner/repo#v1.0``
            or ``name@marketplace#1.2.0``).
        package_name: Human-readable package name.
        is_transitive: Whether this package is a transitive dependency.
        parent_name: Name of the direct dependency that pulled this in
            (only set when *is_transitive* is True).
        hook_count: Number of hook files discovered.
        mcp_count: Number of MCP server entries discovered.
        bin_count: Number of bin/ executables discovered.
        canvas_count: Number of canvas extensions discovered.
        hook_details: Per-hook summaries for ``inspect`` display.
        mcp_details: Per-MCP-server summaries.
        bin_details: Per-binary summaries.
        canvas_details: Per-canvas summaries.
    """

    package_key: str
    package_name: str
    is_transitive: bool = False
    parent_name: str | None = None
    hook_count: int = 0
    mcp_count: int = 0
    bin_count: int = 0
    canvas_count: int = 0
    hook_details: list[str] = field(default_factory=list)
    mcp_details: list[str] = field(default_factory=list)
    bin_details: list[str] = field(default_factory=list)
    canvas_details: list[str] = field(default_factory=list)

    @property
    def has_executables(self) -> bool:
        """Return True if this package declares enforced executable primitives."""
        return (
            self.hook_count > 0 or self.bin_count > 0 or self.mcp_count > 0 or self.canvas_count > 0
        )

    @property
    def exec_types(self) -> list[str]:
        """Return the list of enforced executable types this package declares."""
        types: list[str] = []
        if self.hook_count > 0:
            types.append(EXEC_TYPE_HOOKS)
        if self.mcp_count > 0:
            types.append(EXEC_TYPE_MCP)
        if self.bin_count > 0:
            types.append(EXEC_TYPE_BIN)
        if self.canvas_count > 0:
            types.append(EXEC_TYPE_CANVAS)
        return types

    def summary_line(self) -> str:
        """One-line summary for the interactive prompt (enforced types only)."""
        parts: list[str] = []
        if self.hook_count:
            parts.append(f"{self.hook_count} hook(s)")
        if self.mcp_count:
            parts.append(f"{self.mcp_count} MCP server(s)")
        if self.bin_count:
            parts.append(f"{self.bin_count} bin executable(s)")
        if self.canvas_count:
            parts.append(f"{self.canvas_count} canvas extension(s)")
        return ", ".join(parts)


# -------------------------------------------------------------------
# Approval checking
# -------------------------------------------------------------------


def is_package_approved(
    allow_executables: dict[str, dict[str, bool]] | None,
    package_key: str,
    exec_type: str,
) -> bool:
    """Check whether *package_key* is approved for *exec_type*.

    Args:
        allow_executables: The parsed ``allowExecutables`` block from the
            consuming project's ``apm.yml``.  ``None`` means no block
            exists (nothing approved).
        package_key: The approval key (e.g. ``owner/repo#v1.0``).
        exec_type: One of ``hooks``, ``mcp``, ``bin``.

    Returns:
        ``True`` only when the block contains a matching entry with
        ``{exec_type}: true``.
    """
    if not allow_executables:
        return False
    entry = allow_executables.get(package_key)
    if not entry or not isinstance(entry, dict):
        return False
    return bool(entry.get(exec_type, False))


def is_any_type_approved(
    allow_executables: dict[str, dict[str, bool]] | None,
    package_key: str,
) -> bool:
    """Return True if *package_key* is approved for at least one exec type."""
    if not allow_executables:
        return False
    entry = allow_executables.get(package_key)
    if not entry or not isinstance(entry, dict):
        return False
    return any(entry.get(t, False) for t in ALL_EXEC_TYPES)


# -------------------------------------------------------------------
# Approval key construction
# -------------------------------------------------------------------


def build_approval_key(package_name: str, version: str) -> str:
    """Build the ``allowExecutables`` key for a resolved package.

    Uses the format ``<name>#<version>`` which works for all package
    sources (marketplace, git, registry).  The caller is responsible for
    providing the canonical *package_name* (e.g. ``owner/repo`` for git,
    ``name@marketplace`` for marketplace packages).
    """
    if not version:
        return package_name
    return f"{package_name}#{version}"


# -------------------------------------------------------------------
# Package scanning
# -------------------------------------------------------------------


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


# -------------------------------------------------------------------
# Interactive approval prompt
# -------------------------------------------------------------------


def _is_interactive() -> bool:
    """Return True when stdin is a TTY and not suppressed by env vars."""
    if os.environ.get("APM_NON_INTERACTIVE") or os.environ.get("CI"):
        return False
    return hasattr(sys.stdin, "isatty") and sys.stdin.isatty()


def prompt_executable_approval(
    declarations: list[ExecutableDeclaration],
    *,
    allow_executables: dict[str, dict[str, bool]] | None = None,
    trust_all: bool = False,
    no_executables: bool = False,
) -> dict[str, dict[str, bool]]:
    """Run the interactive approval flow for packages with executables.

    Args:
        declarations: Executable declarations for packages that need
            approval (already filtered to only those with executables).
        allow_executables: Existing ``allowExecutables`` block from
            ``apm.yml`` (merged into result for packages already approved).
        trust_all: When True, auto-approve everything without prompting.
        no_executables: When True, deny everything without prompting.

    Returns:
        Updated ``allowExecutables`` dict ready to write back to
        ``apm.yml``.

    Raises:
        SystemExit: In non-interactive mode when unapproved executables
            exist and neither *trust_all* nor *no_executables* is set.
    """
    import click

    from ..utils.console import _rich_echo, _rich_info, _rich_warning

    result = dict(allow_executables or {})

    # Filter to only declarations that actually have executables and are
    # not already fully approved.
    pending = [d for d in declarations if d.has_executables and not _is_fully_approved(result, d)]

    if not pending:
        return result

    # --no-executables: deny everything
    if no_executables:
        return result

    # --trust-all: approve everything
    if trust_all:
        for decl in pending:
            result[decl.package_key] = {t: True for t in decl.exec_types}
        return result

    # Non-interactive (CI): hard error
    if not _is_interactive():
        _rich_warning(
            f"{len(pending)} package(s) declare executable primitives "
            "but are not approved in allowExecutables:"
        )
        for decl in pending:
            provenance = "(transitive)" if decl.is_transitive else "(direct)"
            _rich_echo(f"  {decl.package_key} {provenance}: {decl.summary_line()}")
        _rich_echo("")
        _rich_info(
            "Run 'apm approve <package>' to approve, "
            "or add entries to allowExecutables in apm.yml.",
            symbol="info",
        )
        sys.exit(1)

    # Interactive: prompt per-package
    _rich_warning(f"{len(pending)} package(s) declare executable primitives:")
    _rich_echo("")

    for decl in pending:
        provenance = "transitive" if decl.is_transitive else "direct dependency"
        if decl.is_transitive and decl.parent_name:
            provenance = f"transitive via {decl.parent_name}"
        _rich_echo(f"  {decl.package_key} ({provenance})")
        _rich_echo(f"    {decl.summary_line()}")
        _rich_echo("")

    _rich_echo("  These will execute code on your machine when triggered by")
    _rich_echo("  your IDE or by 'apm run'.")
    _rich_echo("")

    for decl in pending:
        approved = click.confirm(
            f"  Trust {decl.package_name}?",
            default=False,
        )
        if approved:
            result[decl.package_key] = {t: True for t in decl.exec_types}
        _rich_echo("")

    return result


def _is_fully_approved(
    allow_executables: dict[str, dict[str, bool]],
    decl: ExecutableDeclaration,
) -> bool:
    """Return True if all exec types in *decl* are approved."""
    entry = allow_executables.get(decl.package_key)
    if not entry or not isinstance(entry, dict):
        return False
    return all(entry.get(t, False) for t in decl.exec_types)


# -------------------------------------------------------------------
# Manifest read/write helpers
# -------------------------------------------------------------------


def parse_allow_executables(data: dict[str, Any]) -> dict[str, dict[str, bool]] | None:
    """Parse the ``allowExecutables`` block from raw apm.yml data.

    Returns ``None`` when the block is absent.  Raises ``ValueError``
    on schema violations (non-dict values, unknown exec types with
    non-bool values).
    """
    raw = data.get("allowExecutables")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(
            "allowExecutables must be a mapping of "
            "package keys to {hooks: bool, mcp: bool, bin: bool, canvas: bool}"
        )

    result: dict[str, dict[str, bool]] = {}
    for pkg_key, entry in raw.items():
        if not isinstance(pkg_key, str):
            raise ValueError(f"allowExecutables key must be a string, got {type(pkg_key).__name__}")
        if not isinstance(entry, dict):
            raise ValueError(
                f"allowExecutables[{pkg_key!r}] must be a mapping "
                f"of exec types to booleans, got {type(entry).__name__}"
            )
        parsed_entry: dict[str, bool] = {}
        for exec_type, value in entry.items():
            exec_type_str = str(exec_type)
            if exec_type_str not in ALL_EXEC_TYPES:
                raise ValueError(
                    f"allowExecutables[{pkg_key!r}]: unknown exec type "
                    f"{exec_type_str!r} (valid: {', '.join(ALL_EXEC_TYPES)})"
                )
            if not isinstance(value, bool):
                raise ValueError(
                    f"allowExecutables[{pkg_key!r}][{exec_type_str!r}] "
                    f"must be a boolean, got {type(value).__name__}"
                )
            parsed_entry[exec_type_str] = value
        result[str(pkg_key)] = parsed_entry

    return result


def write_allow_executables(
    manifest_path: Path,
    allow_executables: dict[str, dict[str, bool]],
) -> None:
    """Persist *allow_executables* back to the project's ``apm.yml``.

    Reads the existing YAML, updates the ``allowExecutables`` key, and
    writes it back using the standard ``dump_yaml`` helper.

    Note: individual package approvals should live in the user-local
    ``~/.apm/approvals.yml`` (see :func:`save_user_approvals`), not in the
    project manifest which is committed to source control.  This function is
    retained for writing the gate opt-in signal (``allowExecutables: {}``) and
    for CI/automated contexts that intentionally commit approvals.
    """
    from ..utils.yaml_io import dump_yaml, load_yaml

    data = load_yaml(manifest_path)
    if not isinstance(data, dict):
        return

    if allow_executables:
        data["allowExecutables"] = allow_executables
    elif "allowExecutables" in data:
        del data["allowExecutables"]

    dump_yaml(data, manifest_path)


# -------------------------------------------------------------------
# User-local approvals store (~/.apm/approvals.yml)
# -------------------------------------------------------------------


def get_user_approvals_path() -> Path:
    """Return the path to the user-local executable approvals file.

    The file lives at ``~/.apm/approvals.yml`` and is never committed to
    source control.  It stores the same ``allowExecutables`` mapping as the
    project ``apm.yml`` but is scoped to the current user's machine, so
    cloning a project does not implicitly grant trust to its dependencies.
    """
    return Path.home() / ".apm" / "approvals.yml"


def load_user_approvals() -> dict[str, dict[str, bool]]:
    """Load the user-local approvals from ``~/.apm/approvals.yml``.

    Returns an empty dict when the file does not exist.  The file stores the
    approvals mapping directly (package key -> exec-type booleans), without
    the ``allowExecutables:`` YAML wrapper used in project ``apm.yml``.
    """
    path = get_user_approvals_path()
    if not path.is_file():
        return {}
    from ..utils.yaml_io import load_yaml

    data = load_yaml(path)
    if not isinstance(data, dict):
        return {}
    return data


def save_user_approvals(approvals: dict[str, dict[str, bool]]) -> None:
    """Persist *approvals* to ``~/.apm/approvals.yml``.

    Creates ``~/.apm/`` if it does not exist.  The file is written with
    mode ``0o600`` (owner-only) to prevent other users on a shared system
    from reading the approval list.
    """
    import contextlib

    from ..utils.yaml_io import dump_yaml

    path = get_user_approvals_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    dump_yaml(approvals, path)
    with contextlib.suppress(NotImplementedError, OSError):
        import os

        os.chmod(path, 0o600)


def effective_allow_executables(
    project_allow_executables: dict[str, dict[str, bool]] | None,
) -> dict[str, dict[str, bool]] | None:
    """Return the effective allowExecutables map for an install run.

    Merges the project-level gate signal with the user-local approvals:

    - Returns ``None`` when the project has no ``allowExecutables`` block
      (gate disabled -- backward-compatible behaviour, all executables
      deployed).
    - Returns a merged dict when the gate is enabled: project-level entries
      (retained for CI / automated pipelines) are overlaid with user-local
      approvals from ``~/.apm/approvals.yml``.  User approvals take
      precedence so an ``apm approve`` decision is always honoured even if the
      project entry is absent or stale.
    """
    if project_allow_executables is None:
        return None
    user = load_user_approvals()
    return {**project_allow_executables, **user}


def filter_mcp_by_allow_executables(
    mcp_deps: list,
    project_allow_execs: dict | None,
    logger: Any,
) -> list:
    """Filter MCP deps not approved in allowExecutables. Returns filtered list."""
    if project_allow_execs is None or not mcp_deps:
        return mcp_deps
    _allow_execs = effective_allow_executables(project_allow_execs)
    if _allow_execs is None:
        return mcp_deps
    _filtered = []
    for _dep in mcp_deps:
        _slug = _dep.name
        if _slug and not is_package_approved(_allow_execs, _slug, EXEC_TYPE_MCP):
            logger.verbose_detail(
                f"Skipping MCP server from '{_slug}': not approved in allowExecutables. "
                f"Run 'apm approve {_slug}' to approve."
            )
        else:
            _filtered.append(_dep)
    if len(_filtered) < len(mcp_deps):
        logger.warning(
            f"Filtered {len(mcp_deps) - len(_filtered)} MCP server(s) not approved "
            "in allowExecutables."
        )
    return _filtered


def read_bundle_allow_executables(apm_yml_path: Path, logger: Any) -> dict | None:
    """Read allowExecutables from apm.yml for bundle install. Fail-closed on error."""
    try:
        from ..utils.yaml_io import load_yaml  # local import avoids circular at module init

        if not apm_yml_path.is_file():
            return None
        data = load_yaml(apm_yml_path)
        if isinstance(data, dict):
            return parse_allow_executables(data)
        return None
    except Exception as exc:
        logger.warning(
            f"Could not read allowExecutables from apm.yml: {exc}. "
            "Treating as fully enforced with no approvals.",
            symbol="warning",
        )
        return {}
