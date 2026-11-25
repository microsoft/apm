"""G path lifecycle-scripts: the project apm.yml lifecycle: block (#4107).

Arrange: write a post-install script that appends to ``script-runs.log``,
trust it, and install a package so it runs once. ``revoke-approval`` is
``apm lifecycle untrust`` (core/script_trust.py); ``org-deny`` is the org
``executables.deny_all`` ceiling (the shared default).
"""

from __future__ import annotations

from tests.lifecycle.security.gate_paths import PathSpec, Step


def _arrange(key: str) -> list[Step]:
    return [{"edit": "lifecycle"}, "apm lifecycle trust", f"apm install {key}"]


PATH = PathSpec(
    "lifecycle-scripts",
    arrange=_arrange,
    act=("apm install",),
    change_steps={"revoke-approval": lambda key, kind: ["apm lifecycle untrust"]},
)
