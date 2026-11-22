"""Early entry for the install fast paths, before ``commands/install.py`` is imported.

Only a bare ``apm install`` or ``apm install --frozen`` qualifies; any other
argument (or a root option) takes the normal command. The fast paths (no-op and
diff-apply) run under the lifecycle lock; when they decline, the real command
runs as if this entry did not exist.
"""

from __future__ import annotations

from typing import Any

import click

DECLINED = "apm_install_fast_path_declined"
_FAST_ARGV = (("install",), ("install", "--frozen"))


def fast_install_command(ctx: click.Context, lazy: Any) -> click.Command | None:
    """Return a light ``install`` command for a fast-path-eligible argv, else None."""
    from apm_cli.install.noop_diff import disabled

    raw = tuple(ctx.meta.get("apm_raw_args") or ())
    if raw not in _FAST_ARGV or disabled():
        return None

    def run(frozen: bool) -> Any:
        from pathlib import Path

        from apm_cli.install import diff_apply
        from apm_cli.install.locking import lifecycle_operation
        from apm_cli.install.noop_diff import try_noop

        current = click.get_current_context()
        root, argv = Path.cwd(), list(raw)
        options = {"frozen": frozen}
        with lifecycle_operation():
            done = try_noop(options, argv, root) or diff_apply.try_apply(options, argv, root)
        if done:
            return None
        current.meta[DECLINED] = True
        real = lazy.resolved()
        sub = real.make_context("install", ["--frozen"] if frozen else [], parent=current.parent)
        with sub:
            return real.invoke(sub)

    return click.Command(
        "install",
        callback=run,
        params=[click.Option(["--frozen"], is_flag=True)],
        add_help_option=False,
    )
