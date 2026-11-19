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
from pathlib import Path
from typing import TYPE_CHECKING

import click

from apm_cli.install.locking import serialized_lifecycle

from ..core.command_logger import CommandLogger
from ..policy import outcome_routing as policy_outcomes
from ..policy.outcome_routing import POLICY_RESOLUTION_FAILURE_OUTCOMES
from ..policy.schema import ApmPolicy
from ..utils.console import _rich_echo, _rich_error, _rich_info, _rich_success, _rich_warning

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ..security.executables import ExecutableDeclaration

_POLICY_RESOLUTION_WARNING = (
    "Org policy could not be resolved; approval is proceeding without org "
    "restrictions. Run 'apm policy status --no-cache' to diagnose."
)
_POLICY_HASH_MISMATCH_WARNING = (
    "Policy hash verification failed; this may indicate policy tampering. "
    "Approval is proceeding without org restrictions. "
    "Run 'apm policy status --no-cache' to verify the policy source."
)


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
    allow, deny, used_alias = load_project_executables(manifest)
    if used_alias:
        from ..security.executables import warn_allow_executables_alias_once

        warn_allow_executables_alias_once()
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
    if user_scope:
        from .. import config

        return config.CONFIG_FILE
    return "apm.yml"


def _load_org_policy(project_root: Path, logger: CommandLogger | None = None) -> ApmPolicy:
    """Best-effort load of the merged org policy. Returns a default on failure."""
    warning: str | None = None
    try:
        from ..policy.discovery import discover_policy_with_chain

        result = discover_policy_with_chain(project_root)
        if getattr(result, "policy", None) is not None:
            return result.policy
        if result.outcome == policy_outcomes.POLICY_HASH_MISMATCH_OUTCOME:
            warning = _POLICY_HASH_MISMATCH_WARNING
        elif result.outcome in POLICY_RESOLUTION_FAILURE_OUTCOMES:
            warning = _POLICY_RESOLUTION_WARNING
    except Exception:
        warning = _POLICY_RESOLUTION_WARNING
    if warning is not None and logger is not None:
        logger.warning(warning)
    return ApmPolicy()


def load_org_policy(project_root: Path, logger: CommandLogger | None = None) -> ApmPolicy:
    """Public wrapper for best-effort merged org policy loading."""
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
@serialized_lifecycle
def approve_cmd(
    packages: tuple[str, ...],
    pending: bool,
    approve_all: bool,
    recommended: bool,
    list_decisions: bool,
    user_scope: bool,
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
    """
    logger = CommandLogger("approve")
    try:
        manifest = _find_manifest()

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

        if not packages:
            _rich_error("Specify at least one package, or use --pending / --all / --recommended.")
            sys.exit(1)

        _approve_packages(manifest, user_scope, allow, deny, packages, logger=logger)
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
@serialized_lifecycle
def deny_cmd(packages: tuple[str, ...], user_scope: bool) -> None:
    """Deny executable primitives for packages (a narrowing override).

    Writes a deny entry to the project ``executables.deny`` (default) or your
    personal APM user config (``--user``). Deny always wins.

    Example:

        apm deny owner/repo
    """
    manifest = _find_manifest()
    allow, deny = _load_store(manifest, user_scope)

    declarations = _scan_installed_packages(manifest)

    changed = 0
    for pkg in packages:
        decl = _find_declaration(declarations, pkg)
        if decl is None:
            # Allow denying a package that is not (or no longer) installed.
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
        _rich_info("Run 'apm install' to remove executables already deployed.", symbol="info")


def _effective_trust_context(manifest: Path, *, logger: CommandLogger):
    """Build the org / project / user trust context the install gate resolves."""
    from ..models.manifest_io import load_manifest
    from ..security.executables import build_exec_trust_context

    data = load_manifest(manifest)
    project_data = data if isinstance(data, dict) else {}
    policy = _load_org_policy(manifest.parent, logger=logger)
    return build_exec_trust_context(policy=policy, project_data=project_data)


def explain_decision(package: str, *, logger: CommandLogger) -> None:
    """Explain the effective executable-trust decision for a package.

    Shows, per executable type the package declares, whether it is allowed,
    which precedence layer decided, and which lower-authority layers were
    shadowed by that decision.

    Backs the ``apm policy explain <pkg>`` subcommand.

    Example:

        apm policy explain owner/repo
    """
    manifest = _find_manifest()
    from ..security.executables import resolve_exec_decision

    ctx = _effective_trust_context(manifest, logger=logger)

    decl = _find_declaration(_scan_installed_packages(manifest), package)

    if decl is None:
        _rich_warning(f"{package}: not found among installed packages with executables.")
        if not ctx.gate_enabled:
            _rich_info("The executable-trust gate is disabled for this project.", symbol="info")
        return

    if not ctx.gate_enabled:
        _rich_info(f"{decl.package_key}: gate disabled -- all executables deploy.", symbol="info")
        return

    _rich_echo(f"{decl.package_key}: {decl.summary_line()}")
    decisions = [
        resolve_exec_decision(ctx, decl.package_key, exec_type) for exec_type in decl.exec_types
    ]
    has_block = any(not decision.allowed for decision in decisions)
    verdict = "blocked" if has_block else "allowed"
    _rich_echo(f"  verdict: {verdict}")
    for exec_type, decision in zip(decl.exec_types, decisions, strict=True):
        state = "[+] allowed" if decision.allowed else "[x] blocked"
        _rich_echo(f"  {exec_type:<7} {state}  (layer: {decision.deciding_layer})")
        if decision.shadowed_layers:
            _rich_echo(f"          shadowed: {', '.join(decision.shadowed_layers)}")
    if has_block:
        _rich_info(
            f"To trust it: `apm approve {decl.package_name}` "
            f"(or `apm approve --user {decl.package_name}` for this machine only).",
            symbol="info",
        )


def _list_decisions(manifest: Path, *, logger: CommandLogger) -> None:
    """Print the effective trust decision + deciding layer per installed package."""
    from ..security.executables import resolve_exec_decision

    ctx = _effective_trust_context(manifest, logger=logger)

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

    rows: list[tuple[object, list[tuple[str, object]]]] = []
    allowed_count = blocked_count = 0
    from ..security.trust import has_item_pins, pinned_exec_status

    for decl in declarations:
        row: list[tuple[str, object]] = []
        keys = (decl.package_key, _identity(decl))
        pinned = (
            pinned_exec_status(ctx, keys, Path(decl.install_path), decl.exec_types)
            if decl.install_path and ctx.gate_enabled
            else {}
        )
        for exec_type in decl.exec_types:
            decision = resolve_exec_decision(ctx, decl.package_key, exec_type)
            if exec_type in pinned:
                allowed = pinned[exec_type] == "deployed"
                label = "project-allow, pinned" if allowed else pinned[exec_type]
                decision = (allowed, label)
            elif (
                decision.allowed
                and exec_type in ("hooks", "mcp")
                and decision.deciding_layer
                in (
                    "project-allow",
                    "user-allow",
                )
            ):
                decision = (True, f"{decision.deciding_layer}, any content")
            elif not decision.allowed and has_item_pins(ctx, keys, exec_type):
                decision = (False, "pinned, none match")
            else:
                decision = (decision.allowed, decision.deciding_layer)
            row.append((exec_type, decision))
            if decision[0]:
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
    """Return the installed declaration that *pkg* names, or None.

    Lockfile-derived names (the approval key, or the locked identity a
    ``replace:`` rule redirected) are tried before the package's own
    ``apm.yml`` name, so another package cannot capture a grant or deny by
    declaring a lookalike name. Exact matches are tried before prefix forms;
    ``acme/hooky#1.0.0`` still finds a replaced ``acme/hooky``.
    """

    def _original_prefix(decl: ExecutableDeclaration) -> bool:
        original = decl.original_identity
        return bool(original) and (original.startswith(pkg + "#") or pkg.startswith(original + "#"))

    tiers = (
        lambda d: pkg in (d.package_key, d.original_identity),
        lambda d: pkg == d.package_name,
        lambda d: d.package_key.startswith(pkg + "#") or _original_prefix(d),
        lambda d: d.package_name.startswith(pkg),
    )
    for matches in tiers:
        for decl in declarations:
            if matches(decl):
                return decl
    return None


def _find_matching_key(grant_map: dict[str, dict[str, bool]], pkg: str) -> str | None:
    """Find a key in *grant_map* that matches *pkg* (exact or prefix)."""
    if pkg in grant_map:
        return pkg
    for key in grant_map:
        if key.startswith(pkg + "#"):
            return key
    return None


def _show_pending(manifest: Path, *, logger: CommandLogger) -> None:
    """List installed packages whose executables no trust layer admits.

    Resolves through the same owner as the install gate, evaluated as if the
    gate were on so a project can review before opting in. Pinned items show
    whether their definition changed since approval.
    """
    from dataclasses import replace

    pending = _pending_declarations(manifest, logger=logger)
    if not pending:
        _rich_success("All packages with executables are approved.")
        return

    ctx = replace(_effective_trust_context(manifest, logger=logger), gate_enabled=True)
    _rich_warning(f"{len(pending)} package(s) with unapproved or changed executables:")
    for decl in pending:
        pins = _pins_for(decl)
        _rich_echo(f"  {_identity(decl)}: {decl.summary_line()}")
        _echo_pins(decl, pins, _item_states(ctx, decl, pins))
    _rich_echo("")
    _rich_info(
        "Review each definition, then run 'apm approve <package>' to pin it.",
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
    _record_and_save(manifest, user_scope, allow, deny, selected, logger=logger)


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
    _record_and_save(manifest, user_scope, allow, deny, pending, logger=logger)


def _approve_packages(
    manifest: Path,
    user_scope: bool,
    allow: dict[str, dict[str, bool]],
    deny: dict[str, dict[str, bool]],
    packages: tuple[str, ...],
    *,
    logger: CommandLogger,
) -> None:
    """Approve specific packages by name."""
    declarations = _scan_installed_packages(manifest)

    selected = []
    for pkg in packages:
        decl = _find_declaration(declarations, pkg)

        if decl is None:
            _rich_warning(f"{pkg}: not found in installed packages")
            continue

        if not decl.has_executables:
            _rich_info(f"{pkg}: no executable primitives to approve.", symbol="info")
            continue
        selected.append(decl)
    if selected:
        _record_and_save(manifest, user_scope, allow, deny, selected, logger=logger)


def _identity(decl: ExecutableDeclaration) -> str:
    """Version-blind approval key: pins bind content, so versions add nothing."""
    from ..security.executables import _strip_version

    return _strip_version(decl.package_key)


def _pins_for(decl: ExecutableDeclaration) -> list:
    from ..security.trust import package_pins

    if decl.install_path is None:
        return []
    return package_pins(Path(decl.install_path), transitive=decl.depth > 1)


def _type_counts(decl: ExecutableDeclaration, pins: list) -> str:
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

    width = max((len(pin.item) for pin in pins), default=0)
    for pin in pins:
        digest = short_fingerprint(pin.fingerprint) if pin.fingerprint else "--------"
        state = f"  [{states[(pin.exec_type, pin.item)]}]" if states else ""
        _rich_echo(f"      {pin.exec_type:<6} {pin.item:<{width}}  {pin.summary}  {digest}{state}")
    for exec_type, count, noun in (
        ("bin", decl.bin_count, "executable(s)"),
        ("lsp", decl.lsp_count, "LSP server(s)"),
        ("canvas", decl.canvas_count, "canvas extension(s)"),
    ):
        if count:
            _rich_echo(f"      {exec_type:<6} {count} {noun} (approved by type)")


def _record_declaration(allow: dict, decl: ExecutableDeclaration) -> bool:
    """List one package's definitions and record pinned approval in *allow*."""
    from ..security.trust import record_pins

    identity = _identity(decl)
    if not decl.content_verified:
        _rich_error(
            f"{identity}: installed files differ from apm.lock.yaml; not approved. "
            "Run 'apm install' to restore them, then retry."
        )
        return False
    pins = _pins_for(decl)
    via = f" (via {decl.parent_name})" if decl.parent_name else ""
    _rich_info(f"{identity}{via} declares:", symbol="info")
    _echo_pins(decl, pins)
    unpinnable = [pin for pin in pins if pin.fingerprint is None]
    if unpinnable:
        for pin in unpinnable:
            _rich_error(f"{identity}: cannot pin {pin.exec_type} '{pin.item}': {pin.error}")
        return False
    narrowed = record_pins(allow, identity, decl.exec_types, pins)
    for exec_type in narrowed:
        count = sum(1 for pin in pins if pin.exec_type == exec_type)
        noun = {"hooks": "hook file", "mcp": "MCP server"}[exec_type]
        _rich_info(
            f"{identity} {exec_type}: was approved for any content; "
            f"now pinned to {count} {noun}{'s' if count != 1 else ''}",
            symbol="info",
        )
    _rich_success(
        f"Approved {identity} at these exact definitions ({_type_counts(decl, pins)})",
        symbol="check",
    )
    return True


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
        resolve_exec_decision,
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
            for exec_type in decl.exec_types
            if not resolve_exec_decision(after, decl.package_key, exec_type).allowed
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
) -> None:
    """Record pinned approvals for *declarations* and persist the store once."""
    recorded = [decl for decl in declarations if _record_declaration(allow, decl)]
    if not recorded:
        return
    if not user_scope:
        parked = _first_approval_parks(manifest, recorded, allow, deny, logger=logger)
        if parked and not _confirm_first_approval(parked):
            _rich_info("Nothing written.", symbol="info")
            return
    _save_store(manifest, user_scope, allow, deny)
    share = "" if user_scope else "; commit apm.yml to share"
    _rich_info(
        f"Updated {_store_label(user_scope)} ({len(recorded)} approved). "
        f"Run 'apm install' to deploy{share}.",
        symbol="info",
    )


def _item_states(ctx, decl: ExecutableDeclaration, pins: list) -> dict[tuple[str, str], str]:
    """Per-item approval state for display: approved, changed or not approved."""
    from ..security.trust import TrustSubject, admit, pinned_layer

    keys = (decl.package_key, _identity(decl))
    states = {}
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

    from ..security.executables import resolve_exec_decision
    from ..security.trust import has_item_pins, pinned_exec_status

    ctx = replace(_effective_trust_context(manifest, logger=logger), gate_enabled=True)
    pending = []
    for decl in _scan_installed_packages(manifest):
        keys = (decl.package_key, _identity(decl))
        pinned = (
            pinned_exec_status(ctx, keys, Path(decl.install_path), decl.exec_types)
            if decl.install_path
            else {}
        )
        if any(status != "deployed" for status in pinned.values()) or any(
            not resolve_exec_decision(ctx, decl.package_key, exec_type).allowed
            for exec_type in decl.exec_types
            if not (exec_type in pinned or has_item_pins(ctx, keys, exec_type))
        ):
            pending.append(decl)
    return pending


def scan_installed_executable_packages(manifest: Path) -> list:
    """Scan all installed packages under apm_modules/ for executables."""
    from dataclasses import replace

    from ..security.executables import (
        locked_dependency_approval,
        scan_package_executables,
    )
    from ..security.trust import installed_content_matches_lock

    apm_modules = manifest.parent / "apm_modules"
    results: list[ExecutableDeclaration] = []

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
                f"invalid local dependency ancestry: {exc}. Run 'apm install' "
                "to regenerate the lockfile, then retry."
            ) from exc
        for locked in lockfile.get_package_dependencies():
            try:
                package_dir = (
                    local_dependency_dirs[locked.get_unique_key()]
                    if locked.is_local_source
                    else locked.to_dependency_ref().get_install_path(apm_modules)
                )
            except (OSError, ValueError):
                continue
            if not package_dir.is_dir():
                continue
            name = locked.name or package_dir.name
            version = str(locked.version or "")
            package_manifest = package_dir / "apm.yml"
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
            if declaration.has_executables:
                results.append(
                    replace(
                        declaration,
                        install_path=str(package_dir),
                        depth=locked.depth,
                        content_verified=installed_content_matches_lock(locked, package_dir),
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
            pkg_yml = pkg_dir / "apm.yml"
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
            if decl.has_executables:
                results.append(replace(decl, install_path=str(pkg_dir)))

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
