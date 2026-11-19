"""Runtimes an MCP install selects at user scope (``apm install -g``).

Split out of :mod:`apm_cli.integration.mcp_integrator_install`, which
re-exports these names for existing callers.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apm_cli.core.target_detection import EffectiveTargetDecision


def partition_user_scope_runtimes(
    target_runtimes: list[str],
) -> tuple[list[str], list[str]]:
    """Partition runtime names by their adapter's user-scope capability."""
    from apm_cli.factory import ClientFactory

    supported: list[str] = []
    skipped: list[str] = []
    for runtime in target_runtimes:
        try:
            client = ClientFactory.create_client(runtime)
        except ValueError:
            skipped.append(runtime)
            continue
        destination = supported if client.supports_user_scope else skipped
        destination.append(runtime)
    return supported, skipped


def user_scope_runtime_labels() -> list[str]:
    """Display names of the runtimes whose adapters support user scope (S11, #3967).

    Derived from each adapter's ``supports_user_scope`` and its remote-shape
    label, so help and errors never keep a second list.
    """
    from apm_cli.adapters.client._remote_shapes import REMOTE_SHAPES
    from apm_cli.factory import ClientFactory

    labels: list[str] = []
    for runtime, shape in REMOTE_SHAPES.items():
        try:
            client = ClientFactory.create_client(runtime)
        except ValueError:
            continue
        if client.supports_user_scope and shape.label not in labels:
            labels.append(shape.label)
    return labels


def unavailable_user_scope_targets_message(
    target_decision: EffectiveTargetDecision,
    scoped_targets: list[str] | None,
    skipped_targets: list[str],
) -> str:
    """Render recovery that distinguishes disabled from workspace-only targets."""
    original_targets = set(target_decision.runtime_targets or [])
    disabled_targets = original_targets - set(scoped_targets or [])
    experimental_hint = "enable selected experimental targets, " if disabled_targets else ""
    rendered_targets = ", ".join(sorted(original_targets or set(skipped_targets)))
    return (
        "Selected targets are unavailable for user-scope MCP installation "
        f"({rendered_targets}; source: {target_decision.source}); "
        f"{experimental_hint}choose a global-capable --target or omit --global"
    )


def discover_user_scope_mcp_runtimes(
    project_root: Path,
    *,
    exclude: str | None = None,
) -> tuple[list[str], list[str]]:
    """Discover installed MCP runtimes and partition them for user scope."""
    from apm_cli.integration.mcp_integrator_install import _discover_installed_runtimes

    discovered = _discover_installed_runtimes(project_root, user_scope=True)
    discovered = filter_excluded_mcp_runtimes(discovered, exclude)
    return partition_user_scope_runtimes(discovered)


# One warning per process: frozen install resolves MCP runtimes twice.
_unknown_exclusions_warned: set[str] = set()


def filter_excluded_mcp_runtimes(
    target_runtimes: list[str],
    exclude: str | None,
) -> list[str]:
    """Apply one canonical runtime exclusion, including target aliases.

    The one ``--exclude`` reader for MCP and LSP runtime sets, at project and
    user scope alike.

    ``--exclude`` reads its value like ``--target`` (C4, #3967): case-folded,
    a deprecated alias warns once and selects its replacement, and a value
    that names no target or runtime warns instead of being ignored silently.
    """
    if not exclude:
        return list(target_runtimes)
    from apm_cli.core.target_catalog import canonical_target_token
    from apm_cli.core.target_detection import (
        EffectiveTargetDecision,
        warn_deprecated_target_alias_flag,
    )

    token = exclude.strip().lower()
    warn_deprecated_target_alias_flag(token, flag="--exclude")
    canonical = canonical_target_token(token)
    exclusions = {token, canonical}
    try:
        exclusions.update(EffectiveTargetDecision(canonical, "--exclude").runtime_equivalents or ())
    except KeyError:
        from apm_cli.integration.targets import KNOWN_TARGETS, RUNTIME_TO_CANONICAL_TARGET

        if (
            canonical not in KNOWN_TARGETS
            and canonical not in RUNTIME_TO_CANONICAL_TARGET
            and token not in _unknown_exclusions_warned
        ):
            from apm_cli.utils.console import _rich_warning

            _unknown_exclusions_warned.add(token)

            _rich_warning(
                f"--exclude {exclude!r} names no target or MCP runtime; nothing was excluded.",
                symbol="warning",
            )
    return [runtime for runtime in target_runtimes if runtime not in exclusions]
