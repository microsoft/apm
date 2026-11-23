"""G path: User scope: apm install -g --frozen refuses on the out-of-date lock and
keeps the world; apm install -g removes the surface. (#4107)"""

from __future__ import annotations

from tests.lifecycle.security.gate_paths import FROZEN_REFUSAL, PathSpec

PATH = PathSpec(
    "frozen-g", scope="user", act=("apm install --frozen", "apm install"), announce=FROZEN_REFUSAL
)
