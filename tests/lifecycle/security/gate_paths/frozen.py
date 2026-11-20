"""G path: A gate change leaves the lock out of date: apm install --frozen refuses
and keeps the world; the plain install that follows removes the surface. (#4107)"""

from __future__ import annotations

from tests.lifecycle.security.gate_paths import FROZEN_REFUSAL, PathSpec

PATH = PathSpec("frozen", act=("apm install --frozen", "apm install"), announce=FROZEN_REFUSAL)
