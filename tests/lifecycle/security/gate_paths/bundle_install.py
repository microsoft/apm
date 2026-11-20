"""G path bundle-install: a local plugin bundle of p-exec (#4107).

Arrange: install the bundle, approve this exact bundle the way the
install output tells the user to (``executables.allow`` keyed by
``p-exec#<version>@sha256:<digest>``), and install it again. ``local-deny`` pins the same key under
``executables.deny``; ``revoke-approval`` drops it (the shared default).
"""

from __future__ import annotations

from tests.lifecycle.security.gate_paths import PathSpec, Step


def _arrange(key: str) -> list[Step]:
    return [f"apm install {key}", {"edit": "bundle-approve", "pkg": key}, f"apm install {key}"]


PATH = PathSpec(
    "bundle-install",
    key_suffix=":bundle",
    arrange=_arrange,
    # A local bundle is not recorded in apm.yml: only reinstalling it re-applies the gate.
    act=("apm install {key}",),
    change_steps={"local-deny": lambda key, kind: [{"edit": "bundle-deny", "pkg": key}]},
)
