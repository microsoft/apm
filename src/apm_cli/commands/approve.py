"""``apm approve`` / ``apm deny`` -- manage executable-primitive trust.

Issue #1873 unifies the vocabulary onto one noun, ``executables``, and gives
the commands two clearly-scoped destinations:

* DEFAULT (admin UX): the project ``apm.yml`` ``executables: {allow, deny}``
  block -- committed to source control so the whole team inherits the trust
  decision. This is where a maintainer pins the dependencies their project
  trusts.
* ``--user`` (personal UX): ``~/.apm/config.json`` ``executables: {allow,
  deny}`` -- never committed, lowest authority, a personal override on the
  current machine only.

The deny-wins precedence is resolved by
:func:`apm_cli.security.executables.resolve_exec_decision`; ``--list`` and
``apm policy explain`` surface the effective decision and the deciding layer.
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


def _load_store(
    manifest: Path, user_scope: bool
) -> tuple[dict[str, dict[str, bool]], dict[str, dict[str, bool]]]:
    """Load the (allow, deny) grant maps from the selected store."""
    from ..security.executables import load_project_executables, load_user_executables

    if user_scope:
        return load_user_executables()
    allow, deny, _alias = load_project_executables(manifest)
    return allow, deny


def _save_store(
    manifest: Path,
    user_scope: bool,
    allow: dict[str, dict[str, bool]],
    deny: dict[str, dict[str, bool]],
) -> None:
    """Persist the (allow, deny) grant maps to the selected store."""
    from ..security.executables import save_user_executables, write_project_executables

    if user_scope:
        save_user_executables(allow, deny)
    else:
        write_project_executables(manifest, allow, deny)


def _store_label(user_scope: bool) -> str:
    return "~/.apm/config.json" if user_scope else "apm.yml (executables)"


def _load_org_policy(project_root: Path):
    """Best-effort load of the merged org policy. Returns a default on failure."""
    from ..policy.schema import ApmPolicy

    try:
        from ..policy.discovery import discover_policy

        result = discover_policy(project_root)
        if getattr(result, "policy", None) is not None:
            return result.policy
    except Exception:
        pass
    return ApmPolicy()


@click.command("approve")
@click.argument("packages", nargs=-1)
@click.option("--pending", is_flag=True, help="List packages with unapproved executables.")
@click.option("--all", "approve_all", is_flag=True, help="Approve every package with executables.")
@click.option(
    "--recommended",
    is_flag=True,
    help="Approve the org-recommended executable set (executables.recommend).",
)
@click.option(
    "--list",
    "list_decisions",
    is_flag=True,
    help="List the effective trust decision and deciding layer per installed package.",
)
@click.option(
    "--user",
    "user_scope",
    is_flag=True,
    help="Persist to your personal ~/.apm/config.json (lowest authority) "
    "instead of the shared project apm.yml.",
)
def approve_cmd(
    packages: tuple[str, ...],
    pending: bool,
    approve_all: bool,
    recommended: bool,
    list_decisions: bool,
    user_scope: bool,
) -> None:
    """Approve executable primitives (hooks, MCP, bin, canvas) for packages.

    By default writes to the project ``apm.yml`` ``executables.allow`` block
    (committed). Use ``--user`` to record a personal grant in
    ``~/.apm/config.json`` instead.

    Examples:

        apm approve owner/repo

        apm approve --recommended

        apm approve --list

        apm approve --user owner/repo
    """
    manifest = _find_manifest()

    if list_decisions:
        _list_decisions(manifest)
        return

    allow, deny = _load_store(manifest, user_scope)

    if pending:
        _show_pending(manifest, allow)
        return

    if recommended:
        _approve_recommended(manifest, user_scope, allow, deny)
        return

    if approve_all:
        _approve_all_pending(manifest, user_scope, allow, deny)
        return

    if not packages:
        _rich_error("Specify at least one package, or use --pending / --all / --recommended.")
        sys.exit(1)

    _approve_packages(manifest, user_scope, allow, deny, packages)


@click.command("deny")
@click.argument("packages", nargs=-1, required=True)
@click.option(
    "--user",
    "user_scope",
    is_flag=True,
    help="Record the deny in your personal ~/.apm/config.json instead of apm.yml.",
)
def deny_cmd(packages: tuple[str, ...], user_scope: bool) -> None:
    """Deny executable primitives for packages (a narrowing override).

    Writes a deny entry to the project ``executables.deny`` (default) or your
    personal ``~/.apm/config.json`` (``--user``). Deny always wins.

    Example:

        apm deny owner/repo
    """
    manifest = _find_manifest()
    allow, deny = _load_store(manifest, user_scope)

    declarations = _scan_installed_packages(manifest)
    decl_key_map = {d.package_key: d for d in declarations}
    decl_name_map = {d.package_name: d for d in declarations}

    changed = 0
    for pkg in packages:
        decl = decl_key_map.get(pkg) or decl_name_map.get(pkg)
        if decl is None:
            for d in declarations:
                if d.package_key.startswith(pkg + "#") or d.package_name.startswith(pkg):
                    decl = d
                    break
        if decl is None:
            # Allow denying a package that is not (or no longer) installed.
            deny[pkg] = {t: True for t in ("hooks", "mcp", "bin", "canvas")}
            allow.pop(_find_matching_key(allow, pkg) or pkg, None)
            _rich_success(f"Denied {pkg} (all executable types)")
            changed += 1
            continue
        deny[decl.package_key] = {t: True for t in decl.exec_types}
        allow.pop(_find_matching_key(allow, decl.package_key) or decl.package_key, None)
        _rich_success(f"Denied {decl.package_key}: {decl.summary_line()}")
        changed += 1

    if changed > 0:
        _save_store(manifest, user_scope, allow, deny)
        _rich_info(f"Updated {_store_label(user_scope)} ({changed} denied).", symbol="info")


def explain_decision(package: str) -> None:
    """Explain the effective executable-trust decision for a package.

    Shows, per executable type the package declares, whether it is allowed,
    which precedence layer decided, and which lower-authority layers were
    shadowed by that decision.

    Backs the ``apm policy explain <pkg>`` subcommand.

    Example:

        apm policy explain owner/repo
    """
    manifest = _find_manifest()
    from ..security.executables import build_exec_trust_context, resolve_exec_decision
    from ..utils.yaml_io import load_yaml

    data = load_yaml(manifest)
    project_data = data if isinstance(data, dict) else {}
    policy = _load_org_policy(manifest.parent)
    ctx = build_exec_trust_context(policy=policy, project_data=project_data)

    declarations = _scan_installed_packages(manifest)
    decl = None
    for d in declarations:
        if package in (d.package_key, d.package_name):
            decl = d
            break
        if d.package_key.startswith(package + "#") or d.package_name.startswith(package):
            decl = d
            break

    if decl is None:
        _rich_warning(f"{package}: not found among installed packages with executables.")
        if not ctx.gate_enabled:
            _rich_info("The executable-trust gate is disabled for this project.", symbol="info")
        return

    if not ctx.gate_enabled:
        _rich_info(f"{decl.package_key}: gate disabled -- all executables deploy.", symbol="info")
        return

    _rich_echo(f"{decl.package_key}: {decl.summary_line()}")
    has_block = False
    for exec_type in decl.exec_types:
        decision = resolve_exec_decision(ctx, decl.package_key, exec_type)
        state = "[+] allowed" if decision.allowed else "[x] blocked"
        _rich_echo(f"  {exec_type:<7} {state}  (layer: {decision.deciding_layer})")
        if decision.shadowed_layers:
            _rich_echo(f"          shadowed: {', '.join(decision.shadowed_layers)}")
        if not decision.allowed:
            has_block = True
    if has_block:
        _rich_info(
            f"To trust it: `apm approve {decl.package_name}` "
            f"(or `apm approve --user {decl.package_name}` for this machine only).",
            symbol="info",
        )


def _list_decisions(manifest: Path) -> None:
    """Print the effective trust decision + deciding layer per installed package."""
    from ..security.executables import build_exec_trust_context, resolve_exec_decision
    from ..utils.yaml_io import load_yaml

    data = load_yaml(manifest)
    project_data = data if isinstance(data, dict) else {}
    policy = _load_org_policy(manifest.parent)
    ctx = build_exec_trust_context(policy=policy, project_data=project_data)

    declarations = [d for d in _scan_installed_packages(manifest) if d.has_executables]
    if not declarations:
        _rich_success("No installed packages declare executable primitives.")
        return

    if not ctx.gate_enabled:
        _rich_info(
            "Executable-trust gate disabled -- all executables deploy. "
            "Add an `executables:` block to apm.yml to enable it.",
            symbol="info",
        )

    for decl in declarations:
        states = []
        for exec_type in decl.exec_types:
            decision = resolve_exec_decision(ctx, decl.package_key, exec_type)
            mark = "+" if decision.allowed else "x"
            states.append(f"{exec_type}[{mark}:{decision.deciding_layer}]")
        _rich_echo(f"  {decl.package_key}: {' '.join(states)}")


def _find_matching_key(grant_map: dict[str, dict[str, bool]], pkg: str) -> str | None:
    """Find a key in *grant_map* that matches *pkg* (exact or prefix)."""
    if pkg in grant_map:
        return pkg
    for key in grant_map:
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


def _approve_recommended(
    manifest: Path,
    user_scope: bool,
    allow: dict[str, dict[str, bool]],
    deny: dict[str, dict[str, bool]],
) -> None:
    """Bulk-approve the org ``executables.recommend`` set."""
    policy = _load_org_policy(manifest.parent)
    recommend = set(getattr(getattr(policy, "executables", None), "recommend", ()) or ())
    if not recommend:
        _rich_info("No org-recommended executables to approve.", symbol="info")
        return

    declarations = {d.package_name: d for d in _scan_installed_packages(manifest)}
    count = 0
    for name in sorted(recommend):
        decl = declarations.get(name)
        if decl is None or not decl.has_executables:
            continue
        allow[decl.package_key] = {t: True for t in decl.exec_types}
        _rich_success(f"Approved {decl.package_key}: {decl.summary_line()}")
        count += 1

    if count == 0:
        _rich_info("No installed packages match the org-recommended set.", symbol="info")
        return
    _save_store(manifest, user_scope, allow, deny)
    _rich_info(f"Updated {_store_label(user_scope)} ({count} approved).", symbol="info")


def _approve_all_pending(
    manifest: Path,
    user_scope: bool,
    allow: dict[str, dict[str, bool]],
    deny: dict[str, dict[str, bool]],
) -> None:
    """Approve all installed packages with unapproved executables."""
    declarations = _scan_installed_packages(manifest)
    count = 0
    for decl in declarations:
        if decl.has_executables and not _is_approved(allow, decl):
            allow[decl.package_key] = {t: True for t in decl.exec_types}
            _rich_success(f"Approved {decl.package_key}: {decl.summary_line()}")
            count += 1

    if count == 0:
        _rich_success("All packages with executables are already approved.")
        return

    _save_store(manifest, user_scope, allow, deny)
    _rich_info(f"Updated {_store_label(user_scope)} ({count} approved).", symbol="info")


def _approve_packages(
    manifest: Path,
    user_scope: bool,
    allow: dict[str, dict[str, bool]],
    deny: dict[str, dict[str, bool]],
    packages: tuple[str, ...],
) -> None:
    """Approve specific packages by name."""
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

        allow[decl.package_key] = {t: True for t in decl.exec_types}
        _rich_success(f"Approved {decl.package_key}: {decl.summary_line()}")
        count += 1

    if count > 0:
        _save_store(manifest, user_scope, allow, deny)
        _rich_info(f"Updated {_store_label(user_scope)} ({count} approved).", symbol="info")


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
