"""Scratch-tree helpers for the audit replay in :mod:`apm_cli.install.drift`.

Clearing a scratch path, building the replay's integrator set, walking the
governed roots, and telling a gate-retired hook owner from a local edit (RA-028).
``drift`` re-exports them.
"""

from __future__ import annotations

import json
from pathlib import Path
from shutil import rmtree


def _owned_entries_by_source(canonical: bytes) -> dict[str | None, list[str]]:
    """Group canonical hook-ownership JSON (``{event: [entries]}``) by ``_apm_source``.

    Entries without a string marker, and malformed events, group under ``None``.
    """
    grouped: dict[str | None, list[str]] = {}
    data = json.loads(canonical or b"{}")
    for event_name, entries in data.items() if isinstance(data, dict) else ():
        for entry in entries if isinstance(entries, list) else [entries]:
            source = entry.get("_apm_source") if isinstance(entry, dict) else None
            grouped.setdefault(source if isinstance(source, str) else None, []).append(
                event_name + json.dumps(entry, sort_keys=True)
            )
    return {source: sorted(items) for source, items in grouped.items()}


def _only_retired_sources_differ(replayed: bytes, on_disk: bytes) -> bool:
    """Return whether every differing owner is on disk but absent from the replay.

    The replay applies the current executables gate, so an owner it no longer
    deploys at all was retired by the gate. A user edit to an APM entry drops
    that entry's ownership marker instead, so its owner stays in the replay:
    that drift keeps its local-edit classification.
    """
    expected = _owned_entries_by_source(replayed)
    actual = _owned_entries_by_source(on_disk)
    changed = {
        source
        for source in expected.keys() | actual.keys()
        if expected.get(source) != actual.get(source)
    }
    return (
        bool(changed)
        and None not in changed
        and all(source in actual and source not in expected for source in changed)
    )


def _clear_path(path: Path) -> None:
    """Remove one file tree so a fresh materialization can replace it."""
    if not path.exists():
        return
    if path.is_dir():
        rmtree(path)
        return
    path.unlink()


def _make_integrators():
    """Build a fresh integrator set for one replay run.

    Mirrors ``apm_cli.install.phases.targets:208-215`` so the replay
    behaves identically to a real ``apm install --integrate``.
    """
    from apm_cli.integration.agent_integrator import AgentIntegrator
    from apm_cli.integration.command_integrator import CommandIntegrator
    from apm_cli.integration.hook_integrator import HookIntegrator
    from apm_cli.integration.instruction_integrator import InstructionIntegrator
    from apm_cli.integration.prompt_integrator import PromptIntegrator
    from apm_cli.integration.skill_integrator import SkillIntegrator

    return {
        "prompt": PromptIntegrator(),
        "agent": AgentIntegrator(),
        "skill": SkillIntegrator(),
        "command": CommandIntegrator(),
        "hook": HookIntegrator(),
        "instruction": InstructionIntegrator(),
    }


def _walk_managed(root: Path, governed_roots: set[str]) -> dict[str, Path]:
    """Return a mapping of project-relative posix paths to absolute paths."""
    out: dict[str, Path] = {}
    if not root.exists():
        return out
    for top in governed_roots:
        base = root / top
        if not base.exists():
            continue
        if base.is_file() and not base.is_symlink():
            out[top] = base
            continue
        for p in base.rglob("*"):
            if p.is_file() and not p.is_symlink():
                rel = p.relative_to(root).as_posix()
                out[rel] = p
    # AGENTS.md is a flat top-level file in some target layouts.
    agents_md = root / "AGENTS.md"
    if agents_md.is_file() and not agents_md.is_symlink():
        out["AGENTS.md"] = agents_md
    return out
