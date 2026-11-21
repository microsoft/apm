"""G path marketplace-install: a package installed from the world marketplace (#4107).

User scope (bin/ deploys only for marketplace plugins at user scope with a
claude target: install/exec_gate.py plugin_bin_deployable). Arrange: register
the marketplace, install ``<pkg>@lifecycle-market``, approve it (GateReplay
maps the approval of a marketplace key to its canonical ``lifecycle/<pkg>``),
and install again. Every install passes ``--trust-bin`` so only the gate,
never the per-run bin trust, decides whether bin/ deploys.
"""

from __future__ import annotations

from tests.lifecycle.security.gate_paths import PathSpec, Step


def _arrange(key: str) -> list[Step]:
    return [
        "apm marketplace add MARKETPLACE_URL",
        f"apm install {key} --trust-bin",
        f"apm approve {key}",
        "apm install --trust-bin",
    ]


PATH = PathSpec(
    "marketplace-install",
    scope="user",
    key_suffix=":marketplace",
    arrange=_arrange,
    act=("apm install --trust-bin",),
)
