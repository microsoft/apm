"""``apm approve`` / ``apm deny`` -- manage executable-primitive trust.

Issue #1873 unifies the vocabulary onto one noun, ``executables``, and gives
the commands two clearly-scoped destinations:

* DEFAULT (admin UX): the project ``apm.yml`` ``executables: {allow, deny}``
  block -- committed to source control so the whole team inherits the trust
  decision. This is where a maintainer pins the dependencies their project
  trusts.
* ``--user`` (personal UX): ``$APM_HOME/config.json`` ``executables: {allow,
  deny}`` -- never committed, lowest authority, a personal override on the
  current machine only.

The deny-wins precedence is resolved by
:func:`apm_cli.security.executables.resolve_exec_decision`; ``--list`` and
``apm policy explain`` surface the effective decision and the deciding layer.
"""

from __future__ import annotations

import sys
import weakref
from pathlib import Path
from typing import TYPE_CHECKING

import click

from apm_cli.core.remediation import (
    BUNDLE_EXECUTABLES_ALLOW,
    EXECUTABLES_APPROVED,
    EXECUTABLES_DENIED,
    EXECUTABLES_UNAPPROVED,
    INSTALLED_FILES_DIFFER,
    LOCKFILE_RECORDS_INCONSISTENT,
)
from apm_cli.core.scope import manifest_file
from apm_cli.install.locking import serialized_lifecycle

from ..core.command_logger import CommandLogger
from ..install.errors import PolicyViolationError
from ..policy.outcome_routing import resolve_trust_policy
from ..policy.schema import ApmPolicy
from ..utils.console import _rich_echo, _rich_error, _rich_info, _rich_success, _rich_warning

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ..security.executables import ExecutableDeclaration


def _find_manifest(user_scope: bool = False, global_scope: bool = False) -> Path:
    """Return the project's ``apm.yml`` path or exit.

    ``-g`` reads the user-scope manifest that ``apm install -g`` wrote
    (``$APM_HOME/apm.yml``) and the packages installed next to it (#4154).
    ``--user`` writes only the user store, so outside a project it reads the
    user-scope manifest (``APM_HOME/apm.yml``, which may not exist) and the
    packages ``apm install -g`` installed next to it (#3957).
    """
    from ..core.scope import InstallScope, get_manifest_path

    if global_scope:
        manifest = get_manifest_path(InstallScope.USER)
        if manifest.is_file():
            return manifest
        _rich_error("No user-scope apm.yml found. Install packages with 'apm install -g' first.")
        sys.exit(1)
    manifest = manifest_file(Path.cwd())
    if manifest.is_file():
        return manifest
    if user_scope:
        return get_manifest_path(InstallScope.USER)
    _rich_error("No apm.yml found in the current directory.")
    sys.exit(1)


def _install_command(manifest: Path) -> str:
    """Return the install command that deploys *manifest*'s approvals."""
    from ..core.scope import is_user_scope_root

    return "apm install -g" if is_user_scope_root(manifest.parent) else "apm install"


def _canonical_specs(manifest: Path, specs: Sequence[str]) -> tuple[str, ...]:
    """Resolve every CLI spec to its store identity, or exit 1 before any write.

    The dependency selector ``apm uninstall`` and ``apm audit PACKAGE`` use
    picks the lock row; ``canonical_approval_spec`` names its store identity.
    """
    from ..deps.lockfile import LockFile, get_lockfile_path
    from ..models.dependency.manifest_host import manifest_host_scope_for
    from ..models.manifest_io import load_manifest
    from ..security.executables import ApprovalSpecError, canonical_approval_spec
    from .uninstall.engine import select_installed_lock_key

    lockfile = LockFile.read(get_lockfile_path(manifest.parent))
    resolved: list[str] = []
    errors: list[str] = []
    try:
        data = load_manifest(manifest) if manifest.is_file() else None
        with manifest_host_scope_for(data):
            for spec in specs:
                try:
                    selection = select_installed_lock_key(spec, data, lockfile)
                    key = selection.key if selection is not None else None
                    resolved.append(canonical_approval_spec(spec, lockfile, selected_key=key))
                except (ApprovalSpecError, ValueError) as exc:
                    errors.append(str(exc) if str(exc).startswith(spec) else f"{spec}: {exc}")
    except ValueError as exc:
        errors.append(str(exc))
    for error in errors:
        _rich_error(error)
    if errors:
        sys.exit(1)
    return tuple(resolved)


def _installed_identities(manifest: Path) -> frozenset[str]:
    """Store identities of every package ``apm.lock.yaml`` records for *manifest*."""
    from ..security.executables import _readable_lockfile, canonical_approval_spec

    lockfile = _readable_lockfile(manifest.parent)
    if lockfile is None:
        return frozenset()
    return frozenset(
        canonical_approval_spec(row.get_unique_key(), lockfile, selected_key=row.get_unique_key())
        for row in lockfile.get_package_dependencies()
    )


def _load_store(
    manifest: Path, user_scope: bool
) -> tuple[dict[str, dict[str, bool]], dict[str, dict[str, bool]]]:
    """Load the (allow, deny) grant maps from the selected store.

    A key written as an install spec is stored under the package identity
    when this store is saved (#4189).
    """
    from ..security.executables import (
        _readable_lockfile,
        canonicalize_store_keys,
        load_project_executables,
        load_user_executables,
    )

    if user_scope:
        allow, deny = load_user_executables()
    else:
        allow, deny, used_alias = load_project_executables(manifest)
        if used_alias:
            from ..security.executables import warn_allow_executables_alias_once

            warn_allow_executables_alias_once()
    lockfile = _readable_lockfile(manifest.parent)
    return (
        canonicalize_store_keys(allow, lockfile, store="executables.allow", drop_raw=True),
        canonicalize_store_keys(deny, lockfile, store="executables.deny", drop_raw=True),
    )


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
    if user_scope:
        from .. import config

        return config.CONFIG_FILE
    return "apm.yml"


_ORG_POLICY_PER_COMMAND: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _load_org_policy(project_root: Path, logger: CommandLogger | None = None) -> ApmPolicy:
    """Return the merged org policy, routed as every other policy reader routes it.

    One command resolves its policy once (#4178): the result is memoized per
    command logger, so a miss is reported once however many trust readers ask.

    A blocking outcome raises :class:`PolicyViolationError`; an outcome that
    leaves no usable policy is reported and yields an empty policy.
    """
    root = Path(project_root).resolve()
    if logger is not None:
        cached = _ORG_POLICY_PER_COMMAND.get(logger)
        if cached is not None and cached[0] == root:
            return cached[1]
    policy = resolve_trust_policy(project_root, logger=logger)
    policy = policy if policy is not None else ApmPolicy()
    if logger is not None:
        _ORG_POLICY_PER_COMMAND[logger] = (root, policy)
    return policy


def _report_policy_block(exc: PolicyViolationError) -> None:
    """Announce a blocking policy outcome; nothing was written."""
    _rich_error(str(exc), symbol="error")
    _rich_info(
        "Nothing was written. Run 'apm policy status --no-cache' to diagnose "
        "the org policy source, fix it, then re-run the command.",
        symbol="info",
    )


def load_org_policy(project_root: Path, logger: CommandLogger | None = None) -> ApmPolicy:
    """Public wrapper for routed merged org policy loading."""
    return _load_org_policy(project_root, logger=logger)


@click.command("approve", short_help="Approve executable primitives for packages")
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
    help="List effective trust decisions; fleet view for apm policy explain.",
)
@click.option(
    "--user",
    "user_scope",
    is_flag=True,
    help="Persist to your personal APM user config (lowest authority) "
    "instead of the shared project apm.yml.",
)
@click.option(
    "-g",
    "--global",
    "global_scope",
    is_flag=True,
    help="Approve packages installed at user scope (apm install -g); records the "
    "grant in the user-scope apm.yml.",
)
@serialized_lifecycle
def approve_cmd(
    packages: tuple[str, ...],
    pending: bool,
    approve_all: bool,
    recommended: bool,
    list_decisions: bool,
    user_scope: bool,
    global_scope: bool,
) -> None:
    """Approve executable primitives (hooks, MCP, LSP, bin, canvas) for packages.

    By default writes to the project ``apm.yml`` ``executables.allow`` block
    (committed). Use ``--user`` to record a personal grant in your APM user
    config instead.

    Examples:

        apm approve owner/repo

        apm approve --recommended

        apm approve --list

        apm approve --user owner/repo

        apm approve -g owner/repo
    """
    if not (packages or pending or approve_all or recommended or list_decisions):
        # A missing package is a usage error (exit 2), like `apm deny` (#4262).
        raise click.UsageError(
            "Specify at least one package, or use --pending / --all / --recommended."
        )
    logger = CommandLogger("approve")
    try:
        _reject_quoted_specs(packages, "approve", user_store=user_scope, global_scope=global_scope)
        manifest = _find_manifest(user_scope, global_scope)

        if list_decisions:
            _list_decisions(manifest, logger=logger)
            return

        if pending:
            _show_pending(manifest, logger=logger)
            return

        allow, deny = _load_store(manifest, user_scope)

        if recommended:
            _approve_recommended(manifest, user_scope, allow, deny, logger=logger)
            return

        if approve_all:
            _approve_all_pending(manifest, user_scope, allow, deny, logger=logger)
            return

        from ..security.executables import _is_content_bound_key

        bundle_keys = tuple(pkg for pkg in packages if _is_content_bound_key(pkg))
        packages = tuple(pkg for pkg in packages if not _is_content_bound_key(pkg))
        bundles = _resolve_bundle_keys(manifest, bundle_keys)
        packages = _canonical_specs(manifest, packages) if packages else ()
        if bundle_keys:
            _approve_bundles(manifest, user_scope, allow, deny, bundles, logger=logger)
            if not packages:
                return
            allow, deny = _load_store(manifest, user_scope)
        _approve_packages(manifest, user_scope, allow, deny, packages, logger=logger)
    except PolicyViolationError as exc:
        _report_policy_block(exc)
        sys.exit(1)
    finally:
        logger.render_summary()


@click.command("deny", short_help="Deny executable primitives for packages")
@click.argument("packages", nargs=-1, required=True)
@click.option(
    "--user",
    "user_scope",
    is_flag=True,
    help="Record the deny in your personal APM user config instead of apm.yml.",
)
@click.option(
    "-g",
    "--global",
    "global_scope",
    is_flag=True,
    help="Deny packages installed at user scope (apm install -g).",
)
@serialized_lifecycle
def deny_cmd(packages: tuple[str, ...], user_scope: bool, global_scope: bool) -> None:
    """Deny executable primitives for packages (a narrowing override).

    Writes a deny entry to the project ``executables.deny`` (default) or your
    personal APM user config (``--user``). Deny always wins.

    Example:

        apm deny owner/repo
    """
    _reject_quoted_specs(packages, "deny", user_store=user_scope, global_scope=global_scope)
    manifest = _find_manifest(user_scope, global_scope)
    packages = _canonical_specs(manifest, packages)
    allow, deny = _load_store(manifest, user_scope)

    declarations = _scan_installed_packages(manifest)
    installed = _installed_identities(manifest)
    from ..security.executables import _is_content_bound_key

    # A content-bound bundle key is the exact grant key apm install printed.
    unmatched = [
        pkg
        for pkg in packages
        if _find_declaration(declarations, pkg) is None
        and pkg not in installed
        and not _is_content_bound_key(pkg)
    ]
    if unmatched:
        # Never record a deny that matches no installed package (#4189).
        for pkg in unmatched:
            _report_not_found(declarations, pkg, manifest, verb="deny")
        sys.exit(1)

    changed = 0
    for pkg in packages:
        decl = _find_declaration(declarations, pkg)
        if decl is None:
            # Installed, but declares no executables yet: block any it adds.
            from ..security.executables import ALL_EXEC_TYPES

            deny[pkg] = dict.fromkeys(ALL_EXEC_TYPES, True)
            allow.pop(_find_matching_key(allow, pkg) or pkg, None)
            _rich_info(f"Denied {pkg} (all executable types)", symbol="info")
            changed += 1
            continue
        deny[decl.package_key] = {t: True for t in decl.exec_types}
        allow.pop(_find_matching_key(allow, decl.package_key) or decl.package_key, None)
        allow.pop(_identity(decl), None)
        _rich_info(f"Denied {decl.package_key}: {decl.summary_line()}", symbol="info")
        if decl.original_identity:
            # Deny is safety-positive: the upstream the user named stays denied
            # even after the replace: rule is removed.
            deny[decl.original_identity] = {t: True for t in decl.exec_types}
            allow.pop(
                _find_matching_key(allow, decl.original_identity) or decl.original_identity,
                None,
            )
            _rich_info(
                f"Denied {decl.original_identity} (replaced by {decl.package_key})",
                symbol="info",
            )
        changed += 1

    if changed > 0:
        _save_store(manifest, user_scope, allow, deny)
        _rich_info(f"Updated {_store_label(user_scope)} ({changed} denied).", symbol="info")
        # Deployed hooks keep firing until install retires them (#3924).
        _rich_info(
            EXECUTABLES_DENIED.with_command(_install_command(manifest)).sentence(),
            symbol="info",
        )


def _effective_trust_context(manifest: Path, *, logger: CommandLogger):
    """Build the org / project / user trust context the install gate resolves."""
    from ..models.manifest_io import load_manifest
    from ..security.executables import build_exec_trust_context

    data = load_manifest(manifest) if manifest.is_file() else None
    project_data = data if isinstance(data, dict) else {}
    policy = _load_org_policy(manifest.parent, logger=logger)
    return build_exec_trust_context(policy=policy, project_data=project_data)


def explain_decision(package: str, *, logger: CommandLogger, global_scope: bool = False) -> None:
    """Explain the effective executable-trust decision for a package.

    Shows, per executable type the package declares, whether it is allowed,
    which precedence layer decided, and which lower-authority layers were
    shadowed by that decision.

    Backs the ``apm policy explain <pkg>`` subcommand.

    Example:

        apm policy explain owner/repo
    """
    manifest = _find_manifest(global_scope=global_scope)
    (package,) = _canonical_specs(manifest, (package,))
    ctx = _effective_trust_context(manifest, logger=logger)

    decl = _find_declaration(_scan_installed_packages(manifest), package)

    if decl is None:
        if package not in _installed_identities(manifest):
            _report_not_found(_scan_installed_packages(manifest), package, manifest)
            sys.exit(1)
        _rich_warning(f"{package}: not found among installed packages with executables.")
        if not ctx.gate_enabled:
            _rich_info("The executable-trust gate is disabled for this project.", symbol="info")
        return

    if not ctx.gate_enabled:
        # The executables gate is one layer; org policy can still block the package (#3994).
        _rich_info(
            f"{decl.package_key}: executable-trust gate disabled -- its executables "
            "need no approval. Org policy can still block the package; "
            "'apm audit --ci' reports org-policy violations.",
            symbol="info",
        )
        return

    _rich_echo(f"{decl.package_key}: {decl.summary_line()}")
    verdicts = _verdicts(ctx, decl)
    has_block = any(not verdict.allowed for verdict in verdicts.values())
    _rich_echo(f"  verdict: {'blocked' if has_block else 'allowed'}")
    for exec_type, verdict in verdicts.items():
        state = "[+] allowed" if verdict.allowed else "[x] blocked"
        _rich_echo(f"  {exec_type:<7} {state}  (layer: {_verdict_label(verdict)})")
        if verdict.shadowed:
            _rich_echo(f"          shadowed: {', '.join(verdict.shadowed)}")
    if has_block:
        _explain_next_step(manifest, decl, verdicts.values())


def _explain_next_step(manifest: Path, decl: ExecutableDeclaration, verdicts) -> None:
    """Name how a block was recorded and how to change it, by store identity (#4246)."""
    from ..security.executables import (
        LAYER_ORG_DENY,
        LAYER_ORG_DENY_ALL,
        LAYER_PROJECT_DENY,
        LAYER_USER_DENY,
    )
    from ..security.trust import approve_command

    identity = _identity(decl)
    user_scope = _install_command(manifest).endswith("-g")
    layers = {verdict.layer for verdict in verdicts if not verdict.allowed}
    if layers & {LAYER_ORG_DENY, LAYER_ORG_DENY_ALL}:
        _rich_info(
            "Denied by org policy; approving cannot lift it. Ask the policy owner.", symbol="info"
        )
        return
    for layer, store in ((LAYER_PROJECT_DENY, "apm.yml"), (LAYER_USER_DENY, "--user config")):
        if layer in layers:
            _rich_info(
                f"Denied in {store} (the executables.deny entry for {identity}). To lift it, "
                f"remove that entry, or run '{_lift_deny_hint(layer, identity, user_scope=user_scope)}'.",
                symbol="info",
            )
            return
    _rich_info(
        f"To trust it: `{approve_command(identity, user_scope=user_scope)}` "
        f"(or `{approve_command(identity, user_scope=user_scope, user_store=True)}` "
        "for this machine only).",
        symbol="info",
    )


def _verdicts(ctx, decl: ExecutableDeclaration):
    """Per-type verdicts for one installed package, through the trust owner."""
    from ..security.trust import package_type_verdicts

    return package_type_verdicts(
        ctx,
        (decl.package_key, _identity(decl)),
        Path(decl.install_path) if decl.install_path else None,
        decl.exec_types,
        transitive=decl.depth > 1,
        native=decl.native_plugin,
    )


def _verdict_label(verdict) -> str:
    if verdict.pinned and verdict.allowed:
        return f"{verdict.layer}, pinned"
    if verdict.content_blind:
        return f"{verdict.layer}, any content"
    if verdict.pinned:
        return f"{verdict.layer}, pinned, {verdict.state}"
    return verdict.layer


def _list_decisions(manifest: Path, *, logger: CommandLogger) -> None:
    """Print the effective trust decision + deciding layer per installed package."""
    ctx = _effective_trust_context(manifest, logger=logger)

    declarations = [d for d in _scan_installed_packages(manifest) if d.has_executables]
    if not declarations:
        _rich_success("No installed packages declare executable primitives.")
        return

    if not ctx.gate_enabled:
        _rich_info(
            "Executable-trust gate disabled -- executables need no approval "
            "(org policy still applies). "
            "Add an `executables:` block to apm.yml to enable it.",
            symbol="info",
        )

    rows: list[tuple[object, list[tuple[str, object]]]] = []
    allowed_count = blocked_count = 0
    for decl in declarations:
        row: list[tuple[str, object]] = []
        for exec_type, verdict in _verdicts(ctx, decl).items():
            row.append((exec_type, (verdict.allowed, _verdict_label(verdict))))
            if verdict.allowed:
                allowed_count += 1
            else:
                blocked_count += 1
        rows.append((decl, row))

    _rich_info(
        f"{len(declarations)} package(s) with executables "
        f"({allowed_count} allowed type(s), {blocked_count} blocked type(s)).",
        symbol="info",
    )
    for decl, row in rows:
        states = []
        for exec_type, (allowed, label) in row:
            mark = "+" if allowed else "x"
            states.append(f"{exec_type}[{mark}:{label}]")
        _rich_echo(f"  {decl.package_key}: {' '.join(states)}")
        if decl.native_plugin:
            _echo_pins(decl, _pins_for(decl))
    if blocked_count:
        _rich_info(
            "[x] = parked. Trust with 'apm approve <package>' or 'apm approve --recommended'.",
            symbol="info",
        )
    if any("any content" in label for _decl, row in rows for _t, (_a, label) in row):
        _rich_info(
            "'any content' = approved without a pin. Pin it: apm approve <package>",
            symbol="info",
        )


def _find_declaration(
    declarations: Sequence[ExecutableDeclaration], pkg: str
) -> ExecutableDeclaration | None:
    """Return the installed declaration that *pkg* names exactly, or None.

    Lockfile-derived names (the approval key, or the locked identity a
    ``replace:`` rule redirected) are tried before the package's own
    ``apm.yml`` name, so another package cannot capture a grant or deny by
    declaring a lookalike name. ``acme/hooky#1.0.0`` still finds a replaced
    ``acme/hooky``, but a partial name never matches, and a name that matches
    more than one installed package is refused rather than guessed: a wrong
    match would grant or deny execution for another package.
    """

    def _original_versioned(decl: ExecutableDeclaration) -> bool:
        original = decl.original_identity
        return bool(original) and (original.startswith(pkg + "#") or pkg.startswith(original + "#"))

    tiers = (
        lambda d: pkg in (d.package_key, d.original_identity),
        lambda d: pkg == d.package_name,
        lambda d: d.package_key.startswith(pkg + "#") or _original_versioned(d),
    )
    for matches in tiers:
        found = list({id(d): d for d in declarations if matches(d)}.values())
        if len(found) > 1:
            names = ", ".join(sorted(d.package_key for d in found))
            _rich_error(f"{pkg!r} matches more than one installed package ({names}); name one.")
            sys.exit(1)
        if found:
            return found[0]
    return None


def _not_found_message(
    declarations: Sequence[ExecutableDeclaration], pkg: str, *, user_scope: bool = False
) -> str:
    """Name the ported package a port-less name almost matched (#3967).

    Grants never fall back across ports, so the message points at the exact
    key to use instead of guessing.
    """
    from dataclasses import replace

    from ..models.dependency.reference import DependencyReference

    try:
        wanted = DependencyReference.parse(pkg).get_unique_key()
    except ValueError:
        return f"{pkg}: not found in installed packages"
    ported = []
    for decl in declarations:
        try:
            ref = DependencyReference.parse(decl.package_key)
        except ValueError:
            continue
        if ref.port is not None and replace(ref, port=None).get_unique_key() == wanted:
            ported.append(decl.package_key)
    if not ported:
        return f"{pkg}: not found in installed packages"
    from ..security.trust import approve_command

    keys = " or ".join(f"'{approve_command(key, user_scope=user_scope)}'" for key in sorted(ported))
    return f"{pkg}: not found in installed packages; it is installed with a port, run {keys}"


def _find_matching_key(grant_map: dict[str, dict[str, bool]], pkg: str) -> str | None:
    """Find a key in *grant_map* that matches *pkg* (exact or prefix)."""
    if pkg in grant_map:
        return pkg
    for key in grant_map:
        if key.startswith(pkg + "#"):
            return key
    return None


# An item no trust layer admits while no layer enables the gate: it deploys today.
UNGATED_STATE = "deployed: no executables policy"


def _show_pending(manifest: Path, *, logger: CommandLogger) -> None:
    """List installed packages whose executables no trust layer admits.

    Resolves through the same owner as the install gate, evaluated as if the
    gate were on so a project can review before opting in. Pinned items show
    whether their definition changed since approval.
    """
    from dataclasses import replace

    pending = _pending_declarations(manifest, logger=logger)
    effective = _effective_trust_context(manifest, logger=logger)
    ctx = replace(effective, gate_enabled=True)
    bundles = _pending_bundles(manifest, ctx)
    if not pending and not bundles:
        _rich_success("All packages with executables are approved.")
        return

    if pending:
        _rich_warning(f"{len(pending)} package(s) with unapproved or changed executables:")
        for decl in pending:
            pins = _pins_for(decl)
            _rich_echo(f"  {_identity(decl)}: {decl.summary_line()}")
            states = _item_states(ctx, decl, pins)
            if not effective.gate_enabled:
                # No layer opts in, so these deploy today; the review is ahead of the opt-in.
                states = {
                    key: UNGATED_STATE if state == "not approved" else state
                    for key, state in states.items()
                }
            _echo_pins(decl, pins, states)
        _rich_echo("")
        _rich_info(EXECUTABLES_UNAPPROVED.sentence(), symbol="info")
    if bundles:
        _show_pending_bundles(manifest, bundles)


def _pending_bundles(manifest: Path, ctx) -> list:
    """Installed local bundles whose parked executables are still not approved (#4221)."""
    from ..install.local_bundle_pending import pending_bundles
    from ..security.executables import materialize_exec_map, package_type_approved

    allow_map = materialize_exec_map(ctx)
    return pending_bundles(
        manifest.parent,
        lambda key, exec_type: package_type_approved(allow_map, (key,), exec_type),
    )


def _show_pending_bundles(manifest: Path, bundles: list) -> None:
    from ..security.trust import approve_command

    user_scope = _install_command(manifest).endswith("-g")
    _rich_warning(f"{len(bundles)} local bundle(s) with parked executables:")
    for bundle in bundles:
        _rich_echo(
            f"  local bundle '{bundle.slug}': {', '.join(bundle.exec_types)} not approved; "
            "approval key:"
        )
        # The key is copied into apm.yml, so it is never wrapped.
        _rich_echo(f'    "{bundle.approval_key}"', soft_wrap=True)
        _rich_echo(
            f"    {approve_command(bundle.approval_key, user_scope=user_scope)}", soft_wrap=True
        )
    _rich_echo("")
    _rich_info(
        BUNDLE_EXECUTABLES_ALLOW.bind(manifest=manifest.name)
        .with_command(f"{_install_command(manifest)} <bundle>")
        .sentence(),
        symbol="info",
    )


def _approve_recommended(
    manifest: Path,
    user_scope: bool,
    allow: dict[str, dict[str, bool]],
    deny: dict[str, dict[str, bool]],
    *,
    logger: CommandLogger,
) -> None:
    """Bulk-approve the org ``executables.recommend`` set."""
    policy = _load_org_policy(manifest.parent, logger=logger)
    recommend = set(getattr(getattr(policy, "executables", None), "recommend", ()) or ())
    if not recommend:
        _rich_info("No org-recommended executables to approve.", symbol="info")
        return

    declarations = {d.package_name: d for d in _scan_installed_packages(manifest)}
    selected = [
        declarations[name]
        for name in sorted(recommend)
        if name in declarations and declarations[name].has_executables
    ]
    if not selected:
        _rich_info("No installed packages match the org-recommended set.", symbol="info")
        return
    _record_and_save(manifest, user_scope, allow, deny, selected, logger=logger, bulk=True)


def _approve_all_pending(
    manifest: Path,
    user_scope: bool,
    allow: dict[str, dict[str, bool]],
    deny: dict[str, dict[str, bool]],
    *,
    logger: CommandLogger,
) -> None:
    """Approve all installed packages with unapproved or changed executables."""
    pending = _pending_declarations(manifest, logger=logger)
    if not pending:
        _rich_success("All packages with executables are already approved.")
        return
    _record_and_save(manifest, user_scope, allow, deny, pending, logger=logger, bulk=True)


def _approve_packages(
    manifest: Path,
    user_scope: bool,
    allow: dict[str, dict[str, bool]],
    deny: dict[str, dict[str, bool]],
    packages: tuple[str, ...],
    *,
    logger: CommandLogger,
) -> None:
    """Approve specific packages by name; exit 1 when any name matches nothing."""
    declarations = _scan_installed_packages(manifest)

    selected = []
    missing = 0
    for pkg in packages:
        decl = _find_declaration(declarations, pkg)

        if decl is None:
            missing += 1
            _report_not_found(declarations, pkg, manifest)
            continue

        if not decl.has_executables:
            _rich_info(f"{pkg}: no executable primitives to approve.", symbol="info")
            continue
        selected.append(decl)
    if selected:
        _record_and_save(manifest, user_scope, allow, deny, selected, logger=logger)
    if missing:
        sys.exit(1)


def _resolve_bundle_keys(manifest: Path, keys: Sequence[str]) -> list:
    """Match each content-bound key to the bundle its install recorded, or exit 1 (#4437).

    The key the install printed binds the bundle's exact content digest. A key
    with a wrong digest, or one a later install of changed bundle bytes
    replaced, matches no recorded bundle and is refused before any write.
    """
    from ..install.local_bundle_pending import recorded_bundle
    from ..security.trust import approve_command

    resolved = []
    unmatched = []
    for key in dict.fromkeys(keys):
        bundle = recorded_bundle(manifest.parent, key)
        if bundle is None:
            unmatched.append(key)
        else:
            resolved.append(bundle)
    if unmatched:
        review = approve_command("--pending", user_scope=_install_command(manifest).endswith("-g"))
        for key in unmatched:
            _rich_error(
                f"{key}: no installed local bundle has this exact content (wrong digest, "
                f"or the bundle changed since the key was printed). Nothing written. Review: {review}",
                symbol="error",
            )
        sys.exit(1)
    return resolved


def _approve_bundles(
    manifest: Path,
    user_scope: bool,
    allow: dict,
    deny: dict,
    bundles: list,
    *,
    logger: CommandLogger,
) -> None:
    """Grant parked bundle executables under their exact key: the executables.allow entry."""
    from dataclasses import replace

    if not bundles:
        return
    cleared = []
    for bundle in bundles:
        entry = allow.get(bundle.approval_key)
        grant = dict(entry) if isinstance(entry, dict) else {}
        grant.update(dict.fromkeys(bundle.exec_types, True))
        allow[bundle.approval_key] = grant
        if deny.pop(bundle.approval_key, None) is not None:
            cleared.append(bundle.approval_key)
    _save_store(manifest, user_scope, allow, deny)
    for key in cleared:
        _rich_info(f"Cleared deny for {key} in {_store_label(user_scope)}.", symbol="info")
    ctx = replace(_effective_trust_context(manifest, logger=logger), gate_enabled=True)
    blocked = {bundle.approval_key for bundle in _pending_bundles(manifest, ctx)}
    for bundle in bundles:
        if bundle.approval_key in blocked:
            _rich_error(
                f"{bundle.approval_key} is still denied by another trust layer; the approval "
                "has no effect until that deny is lifted.",
                symbol="error",
            )
        else:
            _rich_success(
                f"Approved local bundle '{bundle.slug}': {', '.join(bundle.exec_types)}",
                symbol="check",
            )
    approved = len([b for b in bundles if b.approval_key not in blocked])
    if approved:
        _rich_info(
            f"Updated {_store_label(user_scope)} ({approved} approved). "
            f"Rerun '{_install_command(manifest)} <bundle>' to deploy.",
            symbol="info",
        )
    if blocked & {bundle.approval_key for bundle in bundles}:
        sys.exit(1)


def _trust_command(
    verb: str, argument: str, *, global_scope: bool = False, user_store: bool = False
) -> str:
    """Return the runnable ``apm approve`` / ``apm deny`` command for *argument*."""
    import shlex

    from ..security.trust import approve_command

    if verb == "approve":
        return approve_command(argument, user_scope=global_scope, user_store=user_store)
    flags = ("-g " if global_scope else "") + ("--user " if user_store else "")
    return f"apm {verb} {flags}{shlex.quote(argument)}"


def _reject_quoted_specs(
    specs: Sequence[str], verb: str, *, user_store: bool = False, global_scope: bool = False
) -> None:
    """Refuse a package argument that still carries literal quotes, before any write.

    No package identity or approval key starts or ends with a quote, so such
    an argument would only record a key that matches nothing.
    """
    quoted = [spec for spec in specs if spec[:1] in "\"'" or spec[-1:] in "\"'"]
    for spec in quoted:
        bare = spec.strip("\"'")
        rerun = (
            f" Run: {_trust_command(verb, bare, global_scope=global_scope, user_store=user_store)}"
            if bare
            else ""
        )
        _rich_error(
            f"{spec}: the package argument contains literal quote characters. "
            f"Nothing written.{rerun}",
            symbol="error",
        )
    if quoted:
        sys.exit(1)


def _local_bundle_keys(manifest: Path, pkg: str) -> list[str]:
    """Exact-content keys of the installed local bundles *pkg* (or ``_local/<pkg>``) names."""
    from ..install.local_bundle_pending import pending_bundles

    name = pkg.split("#", 1)[0].removeprefix("_local/")
    # Treating nothing as approved lists every bundle an install recorded.
    recorded = pending_bundles(manifest.parent, lambda _key, _type: False)
    return sorted(
        bundle.approval_key
        for bundle in recorded
        if name in (bundle.slug, bundle.approval_key.split("#", 1)[0])
    )


def _report_not_found(declarations, pkg: str, manifest: Path, *, verb: str = "approve") -> None:
    """Explain why *pkg* matched nothing and name the command that works."""
    from ..security.trust import approve_command

    base = pkg.split("#", 1)[0]
    decl = _find_declaration(declarations, base) if base != pkg else None
    user_scope = _install_command(manifest).endswith("-g")
    if decl is not None:
        command = approve_command(_identity(decl), user_scope=user_scope)
        _rich_error(f"{pkg}: approve the installed package by identity, not a ref: {command}")
        return
    bundle_keys = _local_bundle_keys(manifest, pkg)
    if bundle_keys:
        # A bundle grant binds its exact content, so name the key rather than guess.
        commands = " or ".join(
            _trust_command(verb, key, global_scope=user_scope) for key in bundle_keys
        )
        _rich_error(
            f"{pkg}: not found in installed packages; it is a local bundle whose key "
            f"binds its exact content. Run: {commands}",
            symbol="error",
        )
        return
    review = approve_command("--pending", user_scope=user_scope)
    _rich_error(f"{_not_found_message(declarations, pkg, user_scope=user_scope)}. Review: {review}")


def _identity(decl: ExecutableDeclaration) -> str:
    """Version-blind approval key: pins bind content, so versions add nothing."""
    from ..security.executables import _strip_version

    return _strip_version(decl.package_key)


def _pins_for(decl: ExecutableDeclaration) -> list:
    from ..security.trust import package_pins

    if decl.install_path is None:
        return []
    return package_pins(
        Path(decl.install_path), transitive=decl.depth > 1, native=decl.native_plugin
    )


def _type_counts(decl: ExecutableDeclaration, pins: list) -> str:
    if any(pin.exec_type == "plugin" for pin in pins):
        return "whole plugin"
    parts = []
    hooks = sum(1 for pin in pins if pin.exec_type == "hooks")
    servers = sum(1 for pin in pins if pin.exec_type == "mcp")
    if hooks:
        parts.append(f"{hooks} hook file{'s' if hooks != 1 else ''}")
    if servers:
        parts.append(f"{servers} MCP server{'s' if servers != 1 else ''}")
    for exec_type, count in (("bin", decl.bin_count), ("lsp", decl.lsp_count)):
        if count:
            parts.append(f"{count} {exec_type} item{'s' if count != 1 else ''}")
    if decl.canvas_count:
        parts.append(f"{decl.canvas_count} canvas extension(s)")
    return ", ".join(parts) or decl.summary_line()


def _echo_pins(decl: ExecutableDeclaration, pins: list, states: dict | None = None) -> None:
    """List what a package runs, one line per pinned item (ASCII only)."""
    from ..security.trust_fingerprint import short_fingerprint

    plugin = [pin for pin in pins if pin.exec_type == "plugin"]
    # A native plugin is reviewed as one unit; its split-install item pins
    # are recorded alongside but not listed.
    pins_all = pins
    pins = plugin or pins
    width = max((len(pin.item) for pin in pins), default=0)
    for pin in pins:
        digest = short_fingerprint(pin.fingerprint) if pin.fingerprint else "--------"
        state = f"  [{states[(pin.exec_type, pin.item)]}]" if states else ""
        _rich_echo(f"      {pin.exec_type:<6} {pin.item:<{width}}  {pin.summary}  {digest}{state}")
    if plugin:
        hooks = sum(1 for pin in pins_all if pin.exec_type == "hooks")
        servers = sum(1 for pin in pins_all if pin.exec_type == "mcp")
        if hooks or servers:
            _rich_echo(
                f"      (also pins {hooks} hook file(s) and {servers} MCP server(s) "
                "for a split install of this package)"
            )
        return
    for exec_type, count, noun in (
        ("bin", decl.bin_count, "executable(s)"),
        ("lsp", decl.lsp_count, "LSP server(s)"),
        ("canvas", decl.canvas_count, "canvas extension(s)"),
    ):
        if count:
            _rich_echo(f"      {exec_type:<6} {count} {noun} (approved by type)")


def _record_declaration(allow: dict, decl: ExecutableDeclaration) -> str | None:
    """List one package's definitions and record pinned approval in *allow*.

    Returns the success line to print once the store is written, or ``None``
    when the package was refused.
    """
    from ..security.trust import record_pins

    identity = _identity(decl)
    if not decl.content_verified:
        _rich_error(
            f"{identity}: installed files differ from apm.lock.yaml; not approved. "
            f"{INSTALLED_FILES_DIFFER.sentence()}"
        )
        return None
    pins = _pins_for(decl)
    if decl.native_plugin and decl.install_path:
        from ..security.trust import apm_owned_reference

        reference = apm_owned_reference(Path(decl.install_path))
        if reference is not None:
            _rich_error(
                f"{identity}: {reference}; APM cannot pin files under .apm/ or apm.yml, "
                "so this plugin cannot be approved. Ask the publisher to move them."
            )
            return None
    via = f" (via {decl.parent_name})" if decl.parent_name else ""
    _rich_info(f"{identity}{via} declares:", symbol="info")
    _echo_pins(decl, pins)
    unpinnable = [pin for pin in pins if pin.fingerprint is None]
    if unpinnable:
        for pin in unpinnable:
            _rich_error(f"{identity}: cannot pin {pin.exec_type} '{pin.item}': {pin.error}")
        return None
    narrowed = record_pins(allow, identity, decl.exec_types, pins)
    for exec_type in narrowed:
        count = sum(1 for pin in pins if pin.exec_type == exec_type)
        noun = {"hooks": "hook file", "mcp": "MCP server"}[exec_type]
        pinned_to = (
            "the whole plugin"
            if decl.native_plugin
            else f"{count} {noun}{'s' if count != 1 else ''}"
        )
        _rich_info(
            f"{identity} {exec_type}: was approved for any content; now pinned to {pinned_to}",
            symbol="info",
        )
    return f"Approved {identity} at these exact definitions ({_type_counts(decl, pins)})"


def _first_approval_parks(
    manifest: Path,
    approving: list[ExecutableDeclaration],
    allow: dict,
    deny: dict,
    *,
    logger: CommandLogger,
) -> list[tuple[str, list[str]]]:
    """Packages a first project approval would park by turning the gate on."""
    from ..models.manifest_io import load_manifest
    from ..security.executables import (
        build_exec_trust_context,
        project_executables_gate_enabled,
    )

    data = load_manifest(manifest)
    project_data = data if isinstance(data, dict) else {}
    policy = _load_org_policy(manifest.parent, logger=logger)
    if project_executables_gate_enabled(project_data):
        return []
    before = build_exec_trust_context(policy=policy, project_data=project_data)
    if before.gate_enabled:
        return []
    after = build_exec_trust_context(
        policy=policy, project_data={"executables": {"allow": allow, "deny": deny}}
    )
    approving_keys = {decl.package_key for decl in approving}
    parked = []
    for decl in _scan_installed_packages(manifest):
        if decl.package_key in approving_keys:
            continue
        blocked = [
            exec_type
            for exec_type, verdict in _verdicts(after, decl).items()
            if not verdict.allowed
        ]
        if blocked:
            parked.append((_identity(decl), blocked))
    return parked


def _confirm_first_approval(parked: list[tuple[str, list[str]]]) -> bool:
    """Warn that the gate turns on; ask on a TTY, proceed otherwise."""
    from ..security.executables import _is_interactive

    noun = "package" if len(parked) == 1 else "packages"
    _rich_warning(
        f"This approval turns on the executable gate. The next install will park "
        f"{len(parked)} other {noun}:",
        symbol="warning",
    )
    for identity, types in parked:
        _rich_echo(f"      {identity} ({', '.join(types)})")
    names = " ".join(identity for identity, _ in parked)
    _rich_echo(f"    Review: apm approve --pending  |  Approve them: apm approve {names}")
    if not _is_interactive():
        return True
    return click.confirm("    Continue?", default=False)


def _record_and_save(
    manifest: Path,
    user_scope: bool,
    allow: dict,
    deny: dict,
    declarations: list[ExecutableDeclaration],
    *,
    logger: CommandLogger,
    bulk: bool = False,
) -> None:
    """Record pinned approvals for *declarations* and persist the store once.

    Naming a package clears its deny in the same store, mirroring how
    ``apm deny`` drops the matching allow; ``--all`` and ``--recommended``
    (*bulk*) skip denied packages instead of lifting them. Exits 1 when any
    requested package could not be pinned, the user declined, or another
    layer still denies it, so an approval never reports success without
    effect (#3957).
    """
    from ..security.trust import PINNED_GRANTS_MIN_APM, has_any_pins

    global_install = _install_command(manifest).endswith("-g")
    had_pins = has_any_pins(allow)
    successes: list[str] = []
    recorded: list[ExecutableDeclaration] = []
    cleared: list[str] = []
    refused = 0
    # Bulk approval is eligible only where no layer denies (#3957): an org or
    # other-store deny is skipped with its reason and no allow is written.
    layer_denied = (
        {id(decl): layer for decl, layer in _still_denied(manifest, declarations, logger=logger)}
        if bulk
        else {}
    )
    for decl in declarations:
        denied = _same_store_denies(deny, decl)
        if bulk and not denied and id(decl) in layer_denied:
            layer = layer_denied[id(decl)]
            _rich_info(
                f"Skipped {_identity(decl)}: denied ({layer}). "
                f"Lift the deny: {_lift_deny_hint(layer, _identity(decl), user_scope=global_install)}",
                symbol="info",
            )
            continue
        if denied and bulk:
            _rich_info(
                f"Skipped {_identity(decl)}: denied in {_store_label(user_scope)}. "
                f"Lift the deny: apm approve {_user_flag(user_scope)}{_identity(decl)}",
                symbol="info",
            )
            continue
        line = _record_declaration(allow, decl)
        if line is None:
            refused += 1
        else:
            for key in denied:
                deny.pop(key, None)
            cleared.extend(denied)
            successes.append(line)
            recorded.append(decl)
    if successes and not user_scope:
        parked = _first_approval_parks(manifest, recorded, allow, deny, logger=logger)
        if parked and not _confirm_first_approval(parked):
            _rich_info("Nothing written.", symbol="info")
            sys.exit(1)
    if successes:
        _save_store(manifest, user_scope, allow, deny)
        for key in cleared:
            _rich_info(f"Cleared deny for {key} in {_store_label(user_scope)}.", symbol="info")
        still_denied = _still_denied(manifest, recorded, logger=logger)
        for decl, layer in still_denied:
            _rich_error(
                f"{_identity(decl)} is still denied ({layer}); the approval has no effect "
                f"until that deny is lifted: {_lift_deny_hint(layer, _identity(decl), user_scope=global_install)}"
            )
        still_ids = {id(decl) for decl, _layer in still_denied}
        successes = [
            line
            for decl, line in zip(recorded, successes, strict=True)
            if id(decl) not in still_ids
        ]
        refused += len(still_denied)
        for line in successes:
            _rich_success(line, symbol="check")
    if successes:
        install = _install_command(manifest)
        share = "" if user_scope or install.endswith("-g") else " Commit apm.yml to share."
        _rich_info(
            f"Updated {_store_label(user_scope)} ({len(successes)} approved). "
            f"{EXECUTABLES_APPROVED.with_command(install).sentence()}{share}",
            symbol="info",
        )
        if share and not had_pins and has_any_pins(allow):
            _rich_info(
                f"Teammates need APM {PINNED_GRANTS_MIN_APM} or later to read pinned approvals.",
                symbol="info",
            )
    if refused:
        sys.exit(1)


def _user_flag(user_scope: bool) -> str:
    return "--user " if user_scope else ""


def _same_store_denies(deny: dict, decl: ExecutableDeclaration) -> list[str]:
    """Deny keys in the store being written that name *decl*'s package."""
    from ..security.executables import _is_content_bound_key, canonical_exec_identity

    keys = {decl.package_key, _identity(decl)}
    # Same spellings the deny ladder matches (URL, host, case, version), so a
    # deny that blocks the package is the deny approve clears.
    identities = {canonical_exec_identity(key) for key in keys if not _is_content_bound_key(key)}
    return [
        key
        for key in deny
        if key in keys
        or (not _is_content_bound_key(key) and canonical_exec_identity(key) in identities)
    ]


def _still_denied(
    manifest: Path, declarations: list[ExecutableDeclaration], *, logger: CommandLogger
) -> list[tuple[ExecutableDeclaration, str]]:
    """Recorded packages a deny in another layer still blocks, with its layer."""
    from dataclasses import replace

    from ..security.executables import TRUST_DENIED

    if not declarations:
        return []
    ctx = replace(_effective_trust_context(manifest, logger=logger), gate_enabled=True)
    blocked = []
    for decl in declarations:
        layers = [v.layer for v in _verdicts(ctx, decl).values() if v.state == TRUST_DENIED]
        if layers:
            blocked.append((decl, layers[0]))
    return blocked


def _lift_deny_hint(layer: str, identity: str, *, user_scope: bool = False) -> str:
    from ..security.executables import LAYER_PROJECT_DENY, LAYER_USER_DENY
    from ..security.trust import approve_command

    if layer == LAYER_USER_DENY:
        return approve_command(identity, user_scope=user_scope, user_store=True)
    if layer == LAYER_PROJECT_DENY:
        return approve_command(identity, user_scope=user_scope)
    return "the org policy denies it; ask its owner to change apm-policy.yml"


def _item_states(ctx, decl: ExecutableDeclaration, pins: list) -> dict[tuple[str, str], str]:
    """Per-item approval state for display: approved, changed or not approved."""
    from ..security.trust import TrustSubject, admit, admit_native_plugin, pinned_layer, plugin_pin

    keys = (decl.package_key, _identity(decl))
    states = {}
    if decl.native_plugin and decl.install_path:
        verdict = admit_native_plugin(ctx, keys, Path(decl.install_path))
        if verdict.allowed:
            state = "approved" if plugin_pin(ctx, keys) else "approved, any content"
        else:
            state = {
                "definition-changed": "changed since approval",
                "denied": "denied",
                "apm-owned-reference": f"cannot be approved: {verdict.detail}",
            }.get(str(verdict.reason), "not approved")
        return {(pin.exec_type, pin.item): state for pin in pins}
    for pin in pins:
        subject = TrustSubject(keys, pin.exec_type, pin.item, pin.fingerprint)
        verdict = admit(ctx, subject)
        if verdict.allowed:
            pinned = pinned_layer(ctx, subject) is not None
            states[(pin.exec_type, pin.item)] = "approved" if pinned else "approved, any content"
        elif verdict.reason == "definition-changed":
            states[(pin.exec_type, pin.item)] = "changed since approval"
        elif verdict.reason == "denied":
            states[(pin.exec_type, pin.item)] = "denied"
        else:
            states[(pin.exec_type, pin.item)] = "not approved"
    return states


def _pending_declarations(manifest: Path, *, logger: CommandLogger) -> list:
    """Installed packages with an executable no trust layer currently admits."""
    from dataclasses import replace

    ctx = replace(_effective_trust_context(manifest, logger=logger), gate_enabled=True)
    return [
        decl
        for decl in _scan_installed_packages(manifest)
        if any(not verdict.allowed for verdict in _verdicts(ctx, decl).values())
    ]


def _is_native_plugin(
    package_type: str | None, package_dir: Path, *, user_scope: bool = False
) -> bool:
    """A plugin a harness loads intact: an Agent Plugin, a Codex bundle, or a
    Claude plugin under the user-scope native route (#3290)."""
    if package_type == "agent_plugin":
        return True
    if package_type is None and _is_agent_plugin(package_dir):
        return True
    if (package_dir / ".codex-plugin" / "plugin.json").is_file():
        return True
    return (
        user_scope
        and package_type in (None, "marketplace_plugin")
        and (package_dir / ".claude-plugin" / "plugin.json").is_file()
    )


def _is_agent_plugin(package_dir: Path) -> bool:
    """Whether the canonical loader reads *package_dir* as an Agent Plugin.

    Without a lockfile there is no recorded ``package_type`` to consult.
    """
    from ..agent_plugins.errors import AgentPluginError
    from ..agent_plugins.loader import detect_agent_plugin

    try:
        detection = detect_agent_plugin(package_dir)
    except (AgentPluginError, OSError):
        return False
    return detection is not None and detection.plugin is not None


def scan_installed_executable_packages(manifest: Path) -> list:
    """Scan all installed packages under apm_modules/ for executables."""
    from dataclasses import replace

    from ..security.executables import (
        locked_dependency_approval,
        scan_package_executables,
    )
    from ..security.trust import (
        approval_scan_dir,
        installed_content_matches_lock,
        native_declaration,
    )

    apm_modules = manifest.parent / "apm_modules"
    results: list[ExecutableDeclaration] = []
    from ..core.scope import InstallScope, get_apm_dir

    user_scope = manifest.parent.resolve() == get_apm_dir(InstallScope.USER).resolve()

    if not apm_modules.is_dir():
        return results

    from ..deps.lockfile import LockFile, get_lockfile_path

    lockfile = LockFile.read(get_lockfile_path(manifest.parent))
    if lockfile is not None:
        from ..deps.path_anchoring import LocalResolutionError, resolve_local_dep_dirs
        from ..utils.yaml_io import load_yaml

        try:
            local_dependency_dirs = resolve_local_dep_dirs(lockfile, manifest.parent)
        except LocalResolutionError as exc:
            raise click.ClickException(
                "Cannot inspect installed packages because apm.lock.yaml has "
                f"invalid local dependency ancestry: {exc}. "
                f"{LOCKFILE_RECORDS_INCONSISTENT.sentence()}"
            ) from exc
        for locked in lockfile.get_package_dependencies():
            try:
                installed_dir = locked.to_dependency_ref().get_install_path(apm_modules)
                package_dir = (
                    approval_scan_dir(local_dependency_dirs[locked.get_unique_key()], installed_dir)
                    if locked.is_local_source
                    else installed_dir
                )
            except (OSError, ValueError):
                continue
            if not package_dir.is_dir():
                continue
            name = locked.name or package_dir.name
            version = str(locked.version or "")
            package_manifest = manifest_file(package_dir)
            manifest_data = None
            if package_manifest.is_symlink():
                manifest_data = {}
            elif package_manifest.is_file():
                try:
                    manifest_data = load_yaml(package_manifest)
                    if isinstance(manifest_data, dict):
                        name = manifest_data.get("name", name)
                        version = str(manifest_data.get("version", version))
                except Exception:
                    manifest_data = {}
            approval = locked_dependency_approval(locked, version)
            declaration = scan_package_executables(
                package_dir,
                name,
                approval.version,
                approval_identity=approval.identity,
                manifest_data=manifest_data,
                is_transitive=locked.depth > 1,
                parent_name=locked.resolved_by if locked.depth > 1 else None,
                original_identity=approval.original_identity,
            )
            native = _is_native_plugin(locked.package_type, package_dir, user_scope=user_scope)
            if native:
                declaration = native_declaration(declaration, package_dir)
            if declaration.has_executables:
                results.append(
                    replace(
                        declaration,
                        install_path=str(package_dir),
                        depth=locked.depth,
                        content_verified=installed_content_matches_lock(locked, package_dir),
                        native_plugin=native,
                    )
                )
        return results

    def _scan_dir(base: Path) -> None:
        for pkg_dir in sorted(base.iterdir()):
            if not pkg_dir.is_dir() or pkg_dir.name.startswith("."):
                continue
            # Recurse into _local/ (local path dependencies)
            if pkg_dir.name == "_local":
                _scan_dir(pkg_dir)
                continue
            pkg_yml = manifest_file(pkg_dir)
            name = pkg_dir.name
            version = ""
            manifest_data = None
            if pkg_yml.is_symlink():
                manifest_data = {}
            elif pkg_yml.is_file():
                try:
                    from ..utils.yaml_io import load_yaml

                    manifest_data = load_yaml(pkg_yml)
                    if isinstance(manifest_data, dict):
                        name = manifest_data.get("name", name)
                        version = str(manifest_data.get("version", ""))
                except Exception:
                    manifest_data = {}

            decl = scan_package_executables(
                pkg_dir,
                name,
                version,
                manifest_data=manifest_data,
            )
            native = _is_native_plugin(None, pkg_dir, user_scope=user_scope)
            if native:
                decl = native_declaration(decl, pkg_dir)
            if decl.has_executables:
                results.append(
                    replace(
                        decl,
                        install_path=str(pkg_dir),
                        native_plugin=native,
                    )
                )

    _scan_dir(apm_modules)
    return results


def _scan_installed_packages(manifest: Path) -> list:
    """Compatibility wrapper for tests and older imports."""
    return scan_installed_executable_packages(manifest)


def _is_approved(
    allow_exec: dict[str, dict[str, bool]],
    decl,
) -> bool:
    """Check if a declaration is fully approved."""
    from ..security.executables import _is_fully_approved

    return _is_fully_approved(allow_exec, decl)
