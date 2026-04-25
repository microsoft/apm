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
CLI invocations (test runners, REPL sessions, embedded callers).
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional


@contextmanager
def install_root_redirect(root: Optional[str | os.PathLike]) -> Iterator[None]:
    """Redirect deploy-side writes into *root* for the wrapped block.

    When *root* is ``None`` or empty, this is a no-op so callers can
    wrap unconditionally.  When set, ensures *root* exists, captures
    the current working directory as the source root, ``chdir``s into
    *root*, and restores both on exit (success or exception).
    """
    if not root:
        yield
        return

    from ..core.scope import set_source_root_override

    target = Path(root)
    target.mkdir(parents=True, exist_ok=True)
    original = Path.cwd()
    set_source_root_override(original)
    os.chdir(target)
    try:
        yield
    finally:
        os.chdir(original)
        set_source_root_override(None)


# Alias used by ``apm compile --root``; semantics are identical so the
# two commands share a single implementation.
compile_root_redirect = install_root_redirect
