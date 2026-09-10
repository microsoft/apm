"""Companion launcher for one explicit local or packaged contract."""

from pathlib import Path

import click

from apm_cli.commands.contracts import invoke_contract
from apm_cli.contracts.frontend import admit_caller_policy
from apm_cli.contracts.models import ContractError, ContractLimits, Outcome
from apm_cli.core.contract_logger import ContractLogger
from apm_cli.core.output_mode import configure_output_mode, detect_output_mode
from apm_cli.core.tls_trust import configure_process_tls_trust
from apm_cli.install.contract_source import prepare_contract_source
from apm_cli.version import get_version


@click.command(
    name="apmx",
    context_settings={"help_option_names": ["-h", "--help"]},
    help=(
        "Run one explicit CONTRACT file; no script or default-job fallback.\n\n"
        "Package contracts are package-relative .contract.md paths. Inputs and "
        "retained evidence belong to the calling directory, not the package."
    ),
)
@click.argument("contract", type=str)
@click.option(
    "--from", "package_ref", metavar="PACKAGE_REF", help="Select a contract from an APM package."
)
@click.option("--on", "harness", required=True, type=str, help="Native harness.")
@click.option("--model", metavar="MODEL", help="Native model identifier.")
@click.option(
    "--plan", "planning", is_flag=True, help="Inspect locally, offline and without execution."
)
@click.option(
    "--allow-advisory", is_flag=True, help="Accept native execution without host isolation."
)
@click.option(
    "--verbose", "-v", is_flag=True, help="Show detailed planning and execution observations."
)
@click.version_option(version=get_version(), prog_name="apmx")
@click.pass_context
def main(
    ctx: click.Context,
    contract: str,
    package_ref: str | None,
    harness: str,
    model: str | None,
    planning: bool,
    allow_advisory: bool,
    verbose: bool,
) -> None:
    """Dispatch one explicitly selected contract through the canonical boundary."""
    configure_output_mode(detect_output_mode([]))
    configure_process_tls_trust()
    ctx.ensure_object(dict)
    logger = ContractLogger(verbose=verbose)
    caller_root = Path.cwd().resolve()
    limits = ContractLimits()
    try:
        if not contract.endswith(".contract.md"):
            raise click.UsageError("CONTRACT must name one explicit .contract.md file.")
        if package_ref is None:
            invoke_contract(
                ctx,
                contract,
                harness=harness,
                model=model,
                verbose=verbose,
                planning=planning,
                allow_advisory=allow_advisory,
            )
            return
        admit_caller_policy(caller_root, limits=limits)
        if not planning and not allow_advisory:
            raise ContractError(
                "Native execution is not isolated. Review host access and pass "
                "--allow-advisory to execute; use --plan for offline inspection.",
                code="advisory_consent_required",
                outcome=Outcome.UNPROVEN,
            )
        with prepare_contract_source(
            package_ref, contract, caller_root=caller_root, planning=planning, limits=limits
        ) as source:
            invoke_contract(
                ctx,
                contract,
                harness=harness,
                model=model,
                verbose=verbose,
                planning=planning,
                allow_advisory=allow_advisory,
                source=source,
            )
    except ContractError as exc:
        logger.render_error(exc)
        ctx.exit(int(exc.outcome))
    except OSError:
        logger.render_error(
            ContractError(
                "Package preparation failed. Check source permissions and available space.",
                code="source_filesystem",
            )
        )
        ctx.exit(int(Outcome.HALTED))
    except KeyboardInterrupt:
        logger.render_error(
            ContractError(
                "Package preparation interrupted. Retry after inspecting any retained run record.",
                code="cancelled",
            )
        )
        ctx.exit(int(Outcome.HALTED))
    finally:
        logger.close()


if __name__ == "__main__":
    main()
