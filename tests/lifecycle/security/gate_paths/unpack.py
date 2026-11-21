"""G path unpack: ``apm unpack`` of the world's --format apm p-exec bundle (#4107).

``apm unpack`` cannot apply the executables gate, so it refuses every
executable file (bundle/unpacker.py) and writes nothing: after any gate
change it never widens what is deployed. The row arranges p-exec the usual
way, changes the gate, pins the refusal, then installs to converge.
"""

from __future__ import annotations

from tests.lifecycle.security.gate_paths import PathSpec

PATH = PathSpec(
    "unpack",
    act=({"run": "apm unpack WORLD:p-exec:apm-bundle", "exit": 1}, "apm install"),
    announce=r"Refusing to unpack \d+ executable file",
)
