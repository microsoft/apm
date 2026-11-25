"""G path: The CI probe: audit --ci and install --frozen agree on the out-of-date
lock (I3); the plain install that follows removes the surface. (#4107)"""

from __future__ import annotations

from tests.lifecycle.security.gate_paths import FROZEN_REFUSAL, PathSpec

PATH = PathSpec(
    "audit-ci",
    act=({"pair": ["apm audit --ci", "apm install --frozen"]}, "apm install"),
    announce=FROZEN_REFUSAL,
)
