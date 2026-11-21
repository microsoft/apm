"""G path: apm update --yes after the gate change removes the surface. (#4107)"""

from __future__ import annotations

from tests.lifecycle.security.gate_paths import PathSpec

PATH = PathSpec("update", act=("apm update --yes",))
