"""Install command context finalization."""

from __future__ import annotations

import sys
from contextlib import AbstractContextManager
from typing import Any


def close_install_contexts(
    root_redirect: AbstractContextManager[Any],
    transaction: AbstractContextManager[Any] | None,
) -> None:
    """Restore the working directory and always close the transaction."""
    try:
        root_redirect.__exit__(None, None, None)
    finally:
        if transaction is not None:
            transaction.__exit__(*sys.exc_info())
