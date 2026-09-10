"""Experimental feature gate for explicit contract planning and execution."""

from __future__ import annotations

from .models import ContractError, Outcome

FLAG_NAME = "contracts"
DISPLAY_NAME = "contracts"
ENABLE_COMMAND = f"apm experimental enable {DISPLAY_NAME}"


def require_contracts_enabled() -> None:
    """Refuse contract planning and execution until the user opts in."""
    from apm_cli.core.experimental import is_enabled

    if is_enabled(FLAG_NAME):
        return
    raise ContractError(
        f"Contract planning and execution require the experimental {DISPLAY_NAME} feature. "
        f"Enable with: {ENABLE_COMMAND}.",
        code="experimental_feature_disabled",
        outcome=Outcome.UNPROVEN,
    )
