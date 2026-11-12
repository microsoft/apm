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

    from apm_cli.cli import main as cli_main

    cli_main()


if __name__ == "__main__":
    main()
