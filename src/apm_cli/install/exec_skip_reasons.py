"""Why the executables gate withheld a package's executable, and how to clear it.

The single owner of the wording install and pack (#4714) print for a parked
executable (hooks, bin/, canvas): a denied type points to ``apm policy
explain``, any other names the ``apm approve`` command that clears it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apm_cli.install.context import InstallContext


def executable_skip_reason(
    ctx: InstallContext | None, package_name: str, label: str, exec_type: str, kind: str
) -> str:
    """Return why *kind* of *package_name* was skipped, with the next step.

    The gate records hard-denied types in ``ctx.package_denied_exec_types``;
    a denied type points to ``apm policy explain`` because approving cannot
    lift a deny. Any other skip is approvable.
    """
    from apm_cli.core.scope import is_user_scope

    denied = getattr(ctx, "package_denied_exec_types", None) or {}
    return withheld_executable_reason(
        label,
        exec_type,
        kind,
        denied=exec_type in denied.get(package_name, frozenset()),
        user_scope=is_user_scope(getattr(ctx, "scope", None)),
    )


def withheld_executable_reason(
    label: str,
    exec_type: str,
    kind: str,
    *,
    denied: bool | None,
    user_scope: bool = False,
    outcome: str = "skipped",
) -> str:
    """Name why the gate withheld *kind* of *label* and the command that clears it.

    The one wording install and pack (#4714) print for a parked executable:
    a denied type points to ``apm policy explain``, any other is approvable.
    ``denied=None`` (the caller cannot tell which) points to the explanation,
    which names the verdict of each type and how to allow it.
    """
    from apm_cli.security.trust import approve_command, explain_command

    explain = explain_command(label, user_scope=user_scope)
    if denied is None:
        return (
            f"{kind} {outcome} (withheld by the executables gate). "
            f"Run '{explain}' to see why and how to allow it."
        )
    if denied:
        return f"{kind} {outcome} (denied). Run '{explain}' to see which deny applies."
    from apm_cli.core import remediation
    from apm_cli.security.executables import EXEC_TYPE_BIN, EXEC_TYPE_CANVAS, EXEC_TYPE_HOOKS

    # Only these kinds are withheld as unapproved here; another kind is a caller bug.
    approve = {
        EXEC_TYPE_BIN: remediation.BIN_EXECUTABLES_UNAPPROVED,
        EXEC_TYPE_CANVAS: remediation.CANVAS_SKIPPED_UNAPPROVED,
        EXEC_TYPE_HOOKS: remediation.HOOKS_UNAPPROVED,
    }[exec_type]
    command = approve_command(label, user_scope=user_scope)
    return (
        f"{kind} {outcome} (not approved in executables.allow). "
        f"{approve.bind(package=label).with_command(command).sentence()}"
    )
