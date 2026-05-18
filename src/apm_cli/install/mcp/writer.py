"""Persist MCP entries into ``apm.yml`` (idempotent W3 R3 / F8 contract).

Extracted from ``commands/install.py`` per the architecture-invariants
LOC budget. ``add_mcp_to_apm_yml`` is the single chokepoint that mutates
``apm.yml`` for ``apm install --mcp``; the diff helper used to render
replacement previews is colocated as a private module-level helper.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click

from ...constants import APM_YML_FILENAME
from ...core.null_logger import NullCommandLogger

MCPEntry = str | dict[str, Any]


@dataclass(frozen=True, slots=True)
class _MCPWriteOpts:
    dev: bool = False
    force: bool = False
    project_root: Path | None = None
    manifest_path: Path | None = None
    logger: Any = None


@dataclass(frozen=True, slots=True)
class _MCPExistingEntryState:
    mcp_list: list[MCPEntry]
    existing_idx: int
    existing_entry: MCPEntry
    log: Any
    apm_yml_path: Path
    force: bool


def _load_mcp_list(data: dict[str, Any], section_name: str, apm_yml_path: Path) -> list[MCPEntry]:
    if section_name not in data or not isinstance(data[section_name], dict):
        data[section_name] = {}
    if "mcp" not in data[section_name] or data[section_name]["mcp"] is None:
        data[section_name]["mcp"] = []
    mcp_list = data[section_name]["mcp"]
    if not isinstance(mcp_list, list):
        raise click.UsageError(f"{apm_yml_path}: '{section_name}.mcp' must be a list")
    return mcp_list


def _find_existing_mcp_entry(
    mcp_list: list[MCPEntry],
    name: str,
) -> tuple[int | None, MCPEntry | None]:
    for index, item in enumerate(mcp_list):
        if isinstance(item, str):
            item_name = item
        elif isinstance(item, dict):
            item_name = item.get("name")
        else:
            item_name = None
        if item_name == name:
            return index, item
    return None, None


def _diff_entry(
    old: MCPEntry | None,
    new: MCPEntry | None,
) -> list[str]:
    """Return a short list of ``key: old -> new`` strings for human display."""
    if isinstance(old, str) and isinstance(new, str):
        if old == new:
            return []
        return [f"  {old} -> {new}"]
    old_d = {"name": old} if isinstance(old, str) else (old or {})
    new_d = {"name": new} if isinstance(new, str) else (new or {})
    keys = list(old_d.keys()) + [k for k in new_d if k not in old_d]
    diff: list[str] = []
    for k in keys:
        ov = old_d.get(k, "<absent>")
        nv = new_d.get(k, "<absent>")
        if ov != nv:
            diff.append(f"  {k}: {ov!r} -> {nv!r}")
    return diff


def _handle_existing_mcp_entry(
    name: str,
    entry: MCPEntry,
    diff: list[str],
    state: _MCPExistingEntryState,
) -> tuple[str, list[str]]:
    is_tty = sys.stdin.isatty() and sys.stdout.isatty()
    if state.force:
        state.mcp_list[state.existing_idx] = entry
        return "replaced", diff
    if is_tty:
        state.log.warning(f"MCP server '{name}' already exists. Replacement diff:")
        for line in diff:
            state.log.tree_item(line)
        if not click.confirm(f"Replace MCP server '{name}'?", default=False):
            return "skipped", diff
        state.mcp_list[state.existing_idx] = entry
        return "replaced", diff
    raise click.UsageError(
        f"MCP server '{name}' already exists in {state.apm_yml_path}. "
        "Use --force to replace (non-interactive)."
    )


def add_mcp_to_apm_yml(
    name: str,
    entry: MCPEntry,
    *,
    opts: _MCPWriteOpts | None = None,
    **kwargs: Any,
) -> tuple[str, list[str] | None]:
    """Persist ``entry`` to ``apm.yml`` under ``dependencies.mcp`` (or
    ``devDependencies.mcp`` when ``dev=True``).

    Idempotency policy (W3 R3, security F8):
    - Existing entry + ``--force``: replace silently, return
      ``("replaced", diff)``.
    - Existing entry + interactive TTY: prompt, return
      ``("replaced", diff)`` or ``("skipped", diff)``.
    - Existing entry + non-TTY (CI): raise :class:`click.UsageError` so
      the CLI exits with code 2.
    - New entry: append, return ``("added", None)``.
    """
    from ...utils.yaml_io import dump_yaml, load_yaml

    if opts is None:
        opts = _MCPWriteOpts(**kwargs)

    log = opts.logger if opts.logger is not None else NullCommandLogger()
    apm_yml_path = opts.manifest_path or Path(APM_YML_FILENAME)
    if not apm_yml_path.exists():
        raise click.UsageError(f"{apm_yml_path}: no apm.yml found. Run 'apm init' first.")
    data = load_yaml(apm_yml_path) or {}

    section_name = "devDependencies" if opts.dev else "dependencies"
    mcp_list = _load_mcp_list(data, section_name, apm_yml_path)
    existing_idx, existing_entry = _find_existing_mcp_entry(mcp_list, name)

    status = "added"
    diff = None
    if existing_idx is not None:
        diff = _diff_entry(existing_entry, entry)
        if not diff:
            return "skipped", []
        status, diff = _handle_existing_mcp_entry(
            name,
            entry,
            diff,
            _MCPExistingEntryState(
                mcp_list=mcp_list,
                existing_idx=existing_idx,
                existing_entry=existing_entry,
                log=log,
                apm_yml_path=apm_yml_path,
                force=opts.force,
            ),
        )
        if status == "skipped":
            return status, diff
    else:
        mcp_list.append(entry)

    data[section_name]["mcp"] = mcp_list
    dump_yaml(data, apm_yml_path)
    return status, diff


# Backward-compatibility alias for tests and legacy callers that imported
# the underscore-prefixed name from apm_cli.commands.install.
_add_mcp_to_apm_yml = add_mcp_to_apm_yml
