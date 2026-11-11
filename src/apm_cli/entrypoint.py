"""Lightweight installed entrypoint for standalone version reporting."""

import sys


def main() -> None:
    """Report a standalone version without importing Git-dependent commands."""
    if sys.argv[1:] == ["--version"]:
        import click

        from apm_cli.version_output import print_version

        command = click.Command(
            "apm",
            params=[
                click.Option(
                    ["--version"],
                    is_flag=True,
                    callback=print_version,
                    expose_value=False,
                    is_eager=True,
                )
            ],
        )
        command.main(args=sys.argv[1:])
        return

    import click

    from apm_cli.harnesses.registry import HarnessAdapterError, get_harness_registry

    try:
        get_harness_registry()
        from apm_cli.cli import main as cli_main

        cli_main()
    except HarnessAdapterError as exc:
        click.ClickException(str(exc)).show()
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
