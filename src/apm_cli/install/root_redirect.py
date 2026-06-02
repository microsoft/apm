"""``--root`` (deploy-root redirection) support for ``apm install`` / ``compile``.

The flag lets users install into an arbitrary directory while keeping
sources in ``$PWD`` -- the precedent is ``pip install --target`` and
``npm install --prefix``.  Implementation strategy:

1. ``os.chdir(root)`` so every site that hardcodes ``Path.cwd()`` /
   ``os.getcwd()`` (notably the MCP adapters in
   :mod:`apm_cli.adapters.client`) automatically resolves to the deploy
   root.  Refactoring those sites to use scope helpers would touch a
   long tail of files; the chdir trick is contained.
2. :func:`apm_cli.core.scope.set_source_root_override` pins the original
   working directory so ``apm.yml``, ``.apm/``, and local-path package
   resolution keep reading from ``$PWD``.

Both effects are reverted on exit so global state never leaks across
CLI invocations (test runners, REPL sessions, embedded callers).  The
``finally`` block is defensive: it restores the original working
directory even if that directory has since been removed, and it always
clears the source-root override regardless of how the body exited.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def install_root_redirect(
    root: str | os.PathLike | None,
    *,
    dry_run: bool = False,
) -> Iterator[None]:
    """Redirect deploy-side writes into *root* for the wrapped block.

    When *root* is ``None`` or empty, this is a no-op so callers can
    wrap unconditionally.

    When set, captures the current working directory as the source
    root, ``chdir``s into *root*, and restores both on exit (success
    or exception).

    ``dry_run`` controls whether *root* is created when missing.  In
    write mode the directory is created (mirroring ``pip install
    --target`` and ``npm install --prefix`` UX).  In dry-run the
    context manager refuses to mutate the filesystem -- if *root*
    does not exist a ``click.UsageError`` is raised so the preview
    cannot silently create directories on disk.
    """
    if not root:
        yield
        return

    from ..core.scope import set_source_root_override

    target = Path(root)
    if dry_run:
        if not target.exists():
            import click

            raise click.UsageError(
                f"--root {target} does not exist. "
                "Create the directory before --dry-run, or drop --dry-run "
                "to let install/compile create it."
            )
    else:
        # ``resolve()`` canonicalises the path (expands ``..`` components,
        # makes it absolute, and follows any symlinks) before
        # ``mkdir(parents=True)``, giving us a stable absolute path that
        # matches what ``os.chdir`` will record as the new cwd.
        target = target.resolve()
        target.mkdir(parents=True, exist_ok=True)
    original = Path.cwd()
    set_source_root_override(original)
    os.chdir(target)
    try:
        yield
    finally:
        # Restore both halves of the redirect unconditionally.  If the
        # original directory was removed while inside the block, leave
        # the process where it is rather than crashing the command on
        # the way out -- but always clear the override so it cannot
        # leak into the next invocation.
        with contextlib.suppress(OSError):
            os.chdir(original)
        set_source_root_override(None)


# ``apm compile --root`` and ``apm install --root`` need exactly the
# same chdir + source-root-pin pair: both commands write into *root*
# while reading sources from the captured ``$PWD``.  The alias keeps
# them on a single implementation so the two flags can never drift.
#
# Split the alias into its own ``contextmanager`` only if compile
# develops needs that install doesn't (e.g. compile-only environment
# tweaks, an output-only sandbox).  Until then, sharing prevents
# silent divergence.
compile_root_redirect = install_root_redirect
