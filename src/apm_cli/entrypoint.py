"""Lightweight installed entrypoint for standalone version reporting."""

import sys


def main() -> None:
    """Report a standalone version without importing Git-dependent commands."""
    import gc

    # PROCESS-WIDE: a short-lived CLI that allocates many small acyclic objects
    # pays hundreds of young-generation collections; raise the gen-0 threshold.
    gc.set_threshold(100_000, 50, 50)
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

    from apm_cli.install import verdict_record

    argv = sys.argv[1:]
    if verdict_record.replay(argv):
        return
    with verdict_record.recording(argv):
        # Imported inside the record: TLS startup reads its settings at import, so
        # the entry state precedes it and the reads are logged.
        from apm_cli.cli import main as cli_main

        cli_main()


if __name__ == "__main__":
    main()
