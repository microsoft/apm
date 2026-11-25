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
_TRACED = "apm_install_fast_path_traced"


# Test seam only, never documented: APM_INSTALL_FAST_PATH=decide prints the
# fast-path decision, writes nothing in any branch and exits DECIDE_EXIT, so it
# can never pass for a successful install (S3 decline-invariant cells).
DECIDE_EXIT = 3


def _decide_only() -> bool:
    """APM_INSTALL_FAST_PATH=decide: print the decision, write nothing, exit DECIDE_EXIT."""
    import os

    return os.environ.get("APM_INSTALL_FAST_PATH", "").strip().lower() == "decide"


def _exit_decided(**_kwargs: Any) -> None:
    click.get_current_context().exit(DECIDE_EXIT)


def _decided() -> click.Command:
    return click.Command(
        "install",
        callback=_exit_decided,
        context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
        add_help_option=False,
    )


def fast_install_command(ctx: click.Context, lazy: Any) -> click.Command | None:
    """Return a light ``install`` command for a fast-path-eligible argv, else None."""
    from apm_cli.install.noop_diff import disabled

    raw = tuple(ctx.meta.get("apm_raw_args") or ())
    if disabled():
        return None
    if raw not in _FAST_ARGV:
        if raw[:1] == ("install",) and not ctx.meta.get(_TRACED):
            from apm_cli.install.diff_apply import _trace

            # click may resolve the command more than once: one decision line per invocation.
            ctx.meta[_TRACED] = True
            _trace("full run (options)")
        if raw[:1] == ("install",) and _decide_only():
            return _decided()
        return None

    def run(frozen: bool | None) -> Any:
        from apm_cli.install.service import InstallService, resolve_frozen

        # The command's own frozen decision (CI with a lockfile selects --frozen and
        # says so), and its lockfile-write chokepoint for the whole callback.
        frozen = resolve_frozen(frozen, False, False, None)
        with InstallService.frozen_writes(frozen):
            return _run(frozen)

    def _run(frozen: bool) -> Any:
        from pathlib import Path
        from types import SimpleNamespace

        from apm_cli.cache.deploy_store import process_umask
        from apm_cli.cache.paths import read_only_caches
        from apm_cli.install import diff_apply
        from apm_cli.install.locking import lifecycle_operation
        from apm_cli.install.service import InstallService
        from apm_cli.install.transaction import ApplyRestoreIncomplete

        process_umask()  # read the umask once, before any worker thread exists
        current = click.get_current_context()
        root, argv = Path.cwd(), list(raw)
        disposition = InstallService.frozen_disposition(SimpleNamespace(frozen=frozen))
        options = {"frozen": disposition.replay}
        decide = _decide_only()
        from apm_cli.security.executables import speculative_consent

        # Deciding only reads: no lifecycle lock (it writes lock state) and no cache upkeep.
        # The fast path never migrates personal consent; the full install does (#5428).
        with read_only_caches() if decide else lifecycle_operation(), speculative_consent():
            try:
                done = diff_apply.try_apply(
                    options, argv, root, frozen_disposition=disposition, decide_only=decide
                )
            except ApplyRestoreIncomplete as exc:
                # The tree is not the pre-run tree: report it, never run the pipeline on it.
                from apm_cli.core.command_logger import InstallLogger

                logger = InstallLogger(verbose=False, dry_run=False, partial=False)
                logger.error(str(exc))
                for path, reason in exc.issues:
                    logger.error(f"Deploy restore path {path}: {reason}")
                current.exit(1)
        if decide:
            current.exit(DECIDE_EXIT)
        if done:
            return None
        current.meta[DECLINED] = True
        real = lazy.resolved()
        sub = real.make_context(
            "install", ["--frozen"] if disposition.replay else [], parent=current.parent
        )
        with sub:
            return real.invoke(sub)

    return click.Command(
        "install",
        callback=run,
        params=[click.Option(["--frozen"], is_flag=True, default=None)],
        add_help_option=False,
    )
