"""Read-only contract planning."""

import click

from .contracts import invoke_contract


@click.command(help="Inspect a contract without inference, installation or execution")
@click.argument("contract")
@click.option("--on", "harness", required=True, help="Explicit execution harness (copilot)")
@click.option("--model", default=None, help="Native model selection; no inference during planning")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed output")
@click.pass_context
def plan(
    ctx: click.Context,
    contract: str,
    harness: str,
    model: str | None,
    verbose: bool,
) -> None:
    """Explain one local .contract.md and its native-advisory prerequisites."""
    invoke_contract(
        ctx,
        contract,
        harness=harness,
        model=model,
        verbose=verbose or bool(ctx.find_root().obj and ctx.find_root().obj.get("verbose")),
        planning=True,
    )
