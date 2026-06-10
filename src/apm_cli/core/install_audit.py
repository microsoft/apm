"""Effective install-time audit decision resolver.

Combines the four sources that can request ``apm audit`` to run during
``apm install`` into one decision, with an explicit, testable precedence:

1. The ``external_scanners`` experimental flag is the master switch. When it
   is off, the install-time audit is always skipped (``off``) regardless of
   every other source -- nothing runs and no cost is paid.
2. With the flag on, a *base* mode is chosen from, in order: the CLI override
   (``apm install --audit <mode>`` / ``--no-audit``), then the
   ``audit-on-install`` value in ``apm config``, then the built-in default
   ``off``.
3. An ``apm-policy.yml`` ``security.audit.on_install`` rule acts as a **floor**
   (governance): it can raise the base mode (e.g. force ``block``) but a weaker
   CLI/config value can never relax it below the policy mode. ``--no-policy``
   disables this floor for the invocation.

Modes form an escalation ladder ``off < warn < block``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import for type hints only
    from apm_cli.install.context import InstallContext
    from apm_cli.security.external.options import ScannerOptions

#: Severity ladder shared with the policy/config layers.
_LEVELS = {"off": 0, "warn": 1, "block": 2}


@dataclass(frozen=True)
class InstallAuditDecision:
    """Resolved install-time audit decision.

    Attributes:
        mode: Effective mode -- ``off`` | ``warn`` | ``block``.
        external: External SARIF scanner names to run (empty unless policy
            requires them AND ``mode`` is not ``off``).
        source: Human-readable origin of the effective mode, for messaging.
        options_by_name: Per-scanner :class:`ScannerOptions` resolved from the
            ``external.<name>.*`` config layer under the policy ``allow_args``
            floor. Empty when ``mode`` is ``off`` or no scanners are required.
    """

    mode: str
    external: tuple[str, ...]
    source: str
    options_by_name: dict[str, ScannerOptions] = field(default_factory=dict)


def resolve_install_audit_mode(
    *,
    flag_enabled: bool,
    cli_override: str | None,
    policy_mode: str | None,
    config_mode: str | None,
) -> tuple[str, str]:
    """Resolve the effective install-time audit mode and its source.

    Args:
        flag_enabled: Whether the ``external_scanners`` experimental flag is on.
        cli_override: ``off`` | ``warn`` | ``block`` from the CLI, or ``None``.
        policy_mode: ``off`` | ``warn`` | ``block`` from policy, or ``None`` for
            "no opinion".
        config_mode: ``off`` | ``warn`` | ``block`` from ``apm config``, or
            ``None``.

    Returns:
        ``(mode, source)`` where *mode* is one of ``off`` | ``warn`` | ``block``.
    """
    if not flag_enabled:
        return "off", "external-scanners experimental flag disabled"

    if cli_override is not None:
        base, base_src = cli_override, "--audit CLI flag"
    elif config_mode is not None and config_mode != "off":
        base, base_src = config_mode, "apm config audit-on-install"
    else:
        base, base_src = (config_mode or "off"), "default"

    # Policy is a floor: it can only raise the base mode, never relax it.
    if policy_mode is not None and _LEVELS[policy_mode] >= _LEVELS[base]:
        return policy_mode, "apm-policy.yml security.audit.on_install"
    return base, base_src


def resolve_audit_override_from_cli(no_audit: bool, audit_mode: str | None) -> str | None:
    """Collapse the ``--audit``/``--no-audit`` CLI flags into one override.

    Args:
        no_audit: ``True`` when ``--no-audit`` was passed.
        audit_mode: The ``--audit <mode>`` value, or ``None`` when absent.

    Returns:
        ``"off"`` when ``--no-audit`` is set, the lower-cased ``--audit`` mode
        when supplied, or ``None`` to defer to config/policy.

    Raises:
        ValueError: When both ``--no-audit`` and ``--audit`` are supplied.
    """
    if no_audit and audit_mode is not None:
        raise ValueError("--no-audit and --audit are mutually exclusive.")
    if no_audit:
        return "off"
    return audit_mode.lower() if audit_mode else None


def decide_for_install(ctx: InstallContext) -> InstallAuditDecision:
    """Build the :class:`InstallAuditDecision` for an install pipeline run."""
    from apm_cli.config import get_audit_on_install
    from apm_cli.core.experimental import is_enabled

    flag_enabled = is_enabled("external_scanners")

    # Policy floor + required scanners -- skipped entirely under --no-policy.
    policy_mode: str | None = None
    policy_external: tuple[str, ...] = ()
    policy_scanners: tuple[tuple[str, object], ...] | None = None
    if not getattr(ctx, "no_policy", False):
        fetch = getattr(ctx, "policy_fetch", None)
        policy = getattr(fetch, "policy", None) if fetch is not None else None
        audit = getattr(getattr(policy, "security", None), "audit", None)
        if audit is not None:
            policy_mode = audit.on_install
            policy_external = audit.external or ()
            policy_scanners = getattr(audit, "scanners", None)

    cli_override = getattr(ctx, "audit_override", None)
    config_mode = get_audit_on_install()

    mode, source = resolve_install_audit_mode(
        flag_enabled=flag_enabled,
        cli_override=cli_override,
        policy_mode=policy_mode,
        config_mode=config_mode,
    )

    external = policy_external if mode != "off" else ()
    options_by_name = (
        _resolve_install_scanner_options(external, policy_scanners) if external else {}
    )
    return InstallAuditDecision(
        mode=mode, external=external, source=source, options_by_name=options_by_name
    )


def _resolve_install_scanner_options(
    external: tuple[str, ...],
    policy_scanners: tuple[tuple[str, object], ...] | None,
) -> dict[str, ScannerOptions]:
    """Resolve per-scanner options for the install path (config under policy floor).

    The install path has no CLI flags; it folds the ``external.<name>.*`` config
    layer under the policy ``allow_args`` governance floor. Policy never
    contributes argv or forces LLM on (restrict-only stance).
    """
    from apm_cli.config import get_scanner_options
    from apm_cli.security.external.options import resolve_scanner_options

    gov_map = dict(policy_scanners or ())
    options: dict[str, ScannerOptions] = {}
    for name in external:
        config_llm, config_args = get_scanner_options(name)
        gov = gov_map.get(name)
        policy_allow_args = getattr(gov, "allow_args", None) if gov is not None else None
        options[name] = resolve_scanner_options(
            cli_llm=None,
            cli_args=None,
            config_llm=config_llm,
            config_args=config_args,
            policy_allow_args=policy_allow_args,
        )
    return options
