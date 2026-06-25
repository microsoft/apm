"""``apm approve`` and ``apm deny`` -- manage executable primitive approvals.

These commands mirror npm v12's ``npm approve-scripts`` / ``npm deny-scripts``.
They read and write the user-local ``~/.apm/approvals.yml`` file so that
approval decisions are never committed to source control.  The project
``apm.yml`` only signals whether the gate is enabled (``allowExecutables: {}``
key present) -- individual package approvals live in the user file.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from ..utils.console import _rich_echo, _rich_error, _rich_info, _rich_success, _rich_warning


def _find_manifest() -> Path:
    """Return the project's ``apm.yml`` path or exit."""
    manifest = Path.cwd() / "apm.yml"
    if not manifest.is_file():
        _rich_error("No apm.yml found in the current directory.")
        sys.exit(1)
    return manifest


def _load_allow_executables() -> dict[str, dict[str, bool]] | None:
    """Load the user-local approvals from ``~/.apm/approvals.yml``.

    Returns ``None`` when no approvals have been recorded yet (gate behaviour
    is determined by the project manifest, not this file).
    """
    from ..security.executables import get_user_approvals_path, load_user_approvals

    if not get_user_approvals_path().is_file():
        return None
    return load_user_approvals()


@click.command("approve")
@click.argument("packages", nargs=-1)
@click.option(
    "--pending",
    is_flag=True,
    help="List all packages with unapproved executables.",
)
@click.option(
    "--all",
    "approve_all",
    is_flag=True,
    help="Approve all packages with executables.",
)
def approve_cmd(packages: tuple[str, ...], pending: bool, approve_all: bool) -> None:
    """Approve executable primitives for installed packages.

    Adds entries to ``~/.apm/approvals.yml`` (user-local, never committed to
    source control) so that hooks, MCP servers, canvas extensions, and bin/
    executables from the specified packages are deployed during ``apm install``.

    Examples:

        apm approve owner/repo

        apm approve --pending

        apm approve --all
    """
    manifest = _find_manifest()
    allow_exec = _load_allow_executables()

    if pending:
        _show_pending(manifest, allow_exec or {})
        return

    if allow_exec is None:
        allow_exec = {}

    if approve_all:
        _approve_all_pending(manifest, allow_exec)
        return

    if not packages:
        _rich_error("Specify at least one package, or use --pending / --all.")
        sys.exit(1)

    _approve_packages(manifest, allow_exec, packages)


@click.command("deny")
@click.argument("packages", nargs=-1, required=True)
def deny_cmd(packages: tuple[str, ...]) -> None:
    """Revoke executable approval for packages.

    Removes entries from ``~/.apm/approvals.yml`` (user-local approvals
    store).

    Example:

        apm deny owner/repo
    """
    allow_exec = _load_allow_executables() or {}

    from ..security.executables import save_user_approvals

    removed = 0
    for pkg in packages:
        matched_key = _find_matching_key(allow_exec, pkg)
        if matched_key:
            del allow_exec[matched_key]
            _rich_success(f"Revoked approval for {matched_key}")
            removed += 1
        else:
            _rich_warning(f"{pkg}: not found in ~/.apm/approvals.yml")

    if removed > 0:
        save_user_approvals(allow_exec)
        _rich_info(f"Updated ~/.apm/approvals.yml ({removed} removed).", symbol="info")


def _find_matching_key(allow_exec: dict[str, dict[str, bool]], pkg: str) -> str | None:
    """Find a key in allow_exec that matches *pkg* (exact or prefix)."""
    # Exact match
    if pkg in allow_exec:
        return pkg
    # Prefix match: "owner/repo" matches "owner/repo#v1.0"
    for key in allow_exec:
        if key.startswith(pkg + "#"):
            return key
    return None


def _show_pending(manifest: Path, allow_exec: dict[str, dict[str, bool]]) -> None:
    """List all installed packages with unapproved executables."""
    declarations = _scan_installed_packages(manifest)
    pending = [d for d in declarations if d.has_executables and not _is_approved(allow_exec, d)]

    if not pending:
        _rich_success("All packages with executables are approved.")
        return

    _rich_warning(f"{len(pending)} package(s) with unapproved executables:")
    _rich_echo("")
    for decl in pending:
        _rich_echo(f"  {decl.package_key}: {decl.summary_line()}")
    _rich_echo("")
    _rich_info(
        "Run 'apm approve <package>' to approve individual packages, "
        "or 'apm approve --all' to approve everything.",
        symbol="info",
    )


def _approve_all_pending(manifest: Path, allow_exec: dict[str, dict[str, bool]]) -> None:
    """Approve all installed packages with unapproved executables."""
    from ..security.executables import save_user_approvals

    declarations = _scan_installed_packages(manifest)
    count = 0
    for decl in declarations:
        if decl.has_executables and not _is_approved(allow_exec, decl):
            allow_exec[decl.package_key] = {t: True for t in decl.exec_types}
            _rich_success(f"Approved {decl.package_key}: {decl.summary_line()}")
            count += 1

    if count == 0:
        _rich_success("All packages with executables are already approved.")
        return

    save_user_approvals(allow_exec)
    _rich_info(f"Updated ~/.apm/approvals.yml ({count} approved).", symbol="info")


def _approve_packages(
    manifest: Path,
    allow_exec: dict[str, dict[str, bool]],
    packages: tuple[str, ...],
) -> None:
    """Approve specific packages by name."""
    from ..security.executables import save_user_approvals

    declarations = _scan_installed_packages(manifest)
    decl_map = {d.package_name: d for d in declarations}
    decl_key_map = {d.package_key: d for d in declarations}

    count = 0
    for pkg in packages:
        decl = decl_key_map.get(pkg) or decl_map.get(pkg)
        if decl is None:
            for d in declarations:
                if d.package_key.startswith(pkg + "#") or d.package_name.startswith(pkg):
                    decl = d
                    break

        if decl is None:
            _rich_warning(f"{pkg}: not found in installed packages")
            continue

        if not decl.has_executables:
            _rich_info(f"{pkg}: no executable primitives to approve.", symbol="info")
            continue

        allow_exec[decl.package_key] = {t: True for t in decl.exec_types}
        _rich_success(f"Approved {decl.package_key}: {decl.summary_line()}")
        count += 1

    if count > 0:
        save_user_approvals(allow_exec)
        _rich_info(f"Updated ~/.apm/approvals.yml ({count} approved).", symbol="info")


def _scan_installed_packages(manifest: Path) -> list:
    """Scan all installed packages under apm_modules/ for executables."""
    from ..security.executables import ExecutableDeclaration, scan_package_executables

    apm_modules = manifest.parent / "apm_modules"
    results: list[ExecutableDeclaration] = []

    if not apm_modules.is_dir():
        return results

    def _scan_dir(base: Path) -> None:
        for pkg_dir in sorted(base.iterdir()):
            if not pkg_dir.is_dir() or pkg_dir.name.startswith("."):
                continue
            # Recurse into _local/ (local path dependencies)
            if pkg_dir.name == "_local":
                _scan_dir(pkg_dir)
                continue
            pkg_yml = pkg_dir / "apm.yml"
            name = pkg_dir.name
            version = ""
            if pkg_yml.is_file():
                try:
                    from ..utils.yaml_io import load_yaml

                    data = load_yaml(pkg_yml)
                    if isinstance(data, dict):
                        name = data.get("name", name)
                        version = str(data.get("version", ""))
                except Exception:
                    pass

            decl = scan_package_executables(pkg_dir, name, version)
            if decl.has_executables:
                results.append(decl)

    _scan_dir(apm_modules)
    return results


def _is_approved(
    allow_exec: dict[str, dict[str, bool]],
    decl,
) -> bool:
    """Check if a declaration is fully approved."""
    from ..security.executables import _is_fully_approved

    return _is_fully_approved(allow_exec, decl)
