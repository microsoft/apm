"""G path: apm install --dry-run keeps the world and must preview the removal; the
plain install that follows removes the surface. (#4107)"""

from __future__ import annotations

from tests.lifecycle.security.gate_paths import PathSpec

PATH = PathSpec("dry-run", act=("apm install --dry-run", "apm install"))
