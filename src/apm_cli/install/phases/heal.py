"""Heal-pipeline dispatcher.

Called from :func:`apm_cli.install.phases.integrate._resolve_download_strategy`
once the initial ``lockfile_match`` decision is computed but BEFORE the
``skip_download`` decision is finalised. The dispatcher walks
``HEAL_CHAIN`` in registration order, honours ``exclusive_group``
short-circuiting, and renders user-facing messages via diagnostics +
logger -- keeping individual heals pure (no I/O, no side effects beyond
mutating the :class:`HealContext`).

Why a per-dep mid-flow dispatcher instead of a top-level pipeline phase:
heals consume the freshly-resolved ``resolved_ref`` and the
``lockfile_match`` decision computed locally inside
``_resolve_download_strategy``. Lifting this to a standalone phase
would force a second remote-ref resolution (network cost) for every
dep. The dispatcher pattern keeps heals discoverable, individually
testable, and pluggable while avoiding the duplicated network round
trip.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from apm_cli.install.heals import HEAL_CHAIN, HealContext, HealMessageLevel

if TYPE_CHECKING:
    from apm_cli.install.context import InstallContext


def run_heal_chain(
    ctx: InstallContext,
    dep_ref: Any,
    *,
    resolved_ref: Any,
    existing_lockfile: Any,
    lockfile_match: bool,
    lockfile_match_via_content_hash_only: bool,
    update_refs: bool,
    ref_changed: bool,
) -> tuple[bool, bool]:
    """Run the heal chain for one dependency.

    Returns the post-heal ``(lockfile_match, ref_changed)`` tuple.
    Side effects:
    - extends ``ctx.expected_hash_change_deps`` with any bypass keys
      emitted by heals (consumed by the supply-chain hard-block in
      ``sources.py``);
    - renders WARN messages via ``ctx.diagnostics`` + ``ctx.logger``;
    - renders INFO messages via ``ctx.logger.verbose_detail`` only.

    Heals do NOT touch ``ctx`` directly -- the dispatcher is the sole
    bridge between the heal chain and the broader install context.
    """
    package_key = dep_ref.get_unique_key()

    hctx = HealContext(
        dep_ref=dep_ref,
        package_key=package_key,
        resolved_ref=resolved_ref,
        existing_lockfile=existing_lockfile,
        lockfile_match=lockfile_match,
        lockfile_match_via_content_hash_only=lockfile_match_via_content_hash_only,
        update_refs=update_refs,
        ref_changed=ref_changed,
    )

    for heal in HEAL_CHAIN:
        if heal.exclusive_group and heal.exclusive_group in hctx.fired_groups:
            continue
        if not heal.applies(hctx):
            continue
        heal.execute(hctx)
        if heal.exclusive_group:
            hctx.fired_groups.add(heal.exclusive_group)

    if hctx.bypass_keys:
        ctx.expected_hash_change_deps.update(hctx.bypass_keys)

    diagnostics = ctx.diagnostics
    logger = ctx.logger
    for msg in hctx.messages:
        if msg.level == HealMessageLevel.WARN:
            diagnostics.warn(msg.text, package=msg.package_key)
            if logger:
                logger.progress(msg.text)
        elif logger:
            logger.verbose_detail(msg.text)

    return hctx.lockfile_match, hctx.ref_changed
