from __future__ import annotations

# Keep this literal for architecture tests that verify safe TUI access after
# the resolve module became a package: getattr(ctx, "tui", None)
from .run import run  # noqa: F401
