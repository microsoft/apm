"""Shared command boundary for explicit contracts, never script fallback."""

from pathlib import Path

import click

from apm_cli.contracts.models import ContractSource


def invoke_contract(
    ctx: click.Context,
    contract: str,
    *,
    harness: str,
    model: str | None,
    verbose: bool,
    planning: bool,
    allow_advisory: bool = False,
    source: ContractSource | None = None,
) -> None:
    """Plan or execute one leaf using the contract-specific error boundary."""
    from ..contracts import frontend, workspace
    from ..contracts.feature_gate import require_contracts_enabled
    from ..contracts.models import ContractError, Outcome
    from ..core.contract_logger import ContractLogger

    logger = ContractLogger(verbose=verbose)
    try:
        require_contracts_enabled()
        logger.start_activity("Reading contract")
        plan = frontend.plan_contract(
            Path(contract),
            Path.cwd(),
            harness=harness,
            model=model,
            source=source,
        )
        inventory = workspace.inspect_workspace(plan)
        logger.stop_activity()
        if planning:
            logger.render_plan(plan, inventory)
            return
        from ..contracts.engine import run_contract

        result = run_contract(plan, logger=logger, allow_advisory=allow_advisory)
        ctx.exit(int(result.outcome))
    except ContractError as exc:
        logger.render_error(exc)
        ctx.exit(int(exc.outcome))
    except KeyboardInterrupt:
        logger.render_error(
            ContractError(
                "Contract command interrupted. Inspect any printed run record before retrying.",
                code="cancelled",
            )
        )
        ctx.exit(int(Outcome.HALTED))
    except OSError as exc:
        logger.render_error(
            ContractError(
                f"Contract filesystem operation failed: {exc}. Inspect permissions and available space.",
                code="filesystem_error",
            )
        )
        ctx.exit(int(Outcome.HALTED))
    finally:
        logger.close()
