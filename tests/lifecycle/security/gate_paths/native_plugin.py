"""G path native-plugin: a Claude plugin installed from git at user scope (#4107).

The approved plugin lands whole under ``~/.agents/plugins/<name>`` and is
enabled in Claude settings; the gate removes it as one unit. Every install
passes ``--trust-bin`` so only the gate, never the per-run bin trust,
decides what deploys.
"""

from __future__ import annotations

from tests.lifecycle.security.gate_paths import PathSpec, Step


def _arrange(key: str) -> list[Step]:
    return [f"apm install {key} --trust-bin", f"apm approve {key}", "apm install --trust-bin"]


PATH = PathSpec(
    "native-plugin",
    scope="user",
    key_suffix=":git",
    arrange=_arrange,
    act=("apm install --trust-bin",),
)
