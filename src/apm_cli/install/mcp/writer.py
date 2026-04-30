"""Persist MCP entries into ``apm.yml`` (idempotent W3 R3 / F8 contract).

Extracted from ``commands/install.py`` per the architecture-invariants
LOC budget. ``add_mcp_to_apm_yml`` is the single chokepoint that mutates
``apm.yml`` for ``apm install --mcp``; the diff helper used to render
replacement previews is colocated as a private module-level helper.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union  # noqa: F401, UP035

import click

from ...constants import APM_YML_FILENAME
from ...core.null_logger import NullCommandLogger

MCPEntry = Union[str, dict[str, Any]]  # noqa: UP007


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
    keys = list(old_d.keys()) + [k for k in new_d.keys() if k not in old_d]  # noqa: SIM118
    diff: list[str] = []
    for k in keys:
        ov = old_d.get(k, "<absent>")
        nv = new_d.get(k, "<absent>")
        if ov != nv:
            diff.append(f"  {k}: {ov!r} -> {nv!r}")
    return diff


def add_mcp_to_apm_yml(
    name: str,
    entry: MCPEntry,
    *,
    dev: bool = False,
    force: bool = False,
    project_root: Path | None = None,
    manifest_path: Path | None = None,
    logger=None,
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

    log = logger if logger is not None else NullCommandLogger()
    apm_yml_path = manifest_path or Path(APM_YML_FILENAME)
    if not apm_yml_path.exists():
        raise click.UsageError(f"{apm_yml_path}: no apm.yml found. Run 'apm init' first.")
    data = load_yaml(apm_yml_path) or {}

    section_name = "devDependencies" if dev else "dependencies"
    if section_name not in data or not isinstance(data[section_name], dict):
        data[section_name] = {}
    if "mcp" not in data[section_name] or data[section_name]["mcp"] is None:
        data[section_name]["mcp"] = []
    mcp_list = data[section_name]["mcp"]
    if not isinstance(mcp_list, list):
        raise click.UsageError(f"{apm_yml_path}: '{section_name}.mcp' must be a list")

    existing_idx = None
    existing_entry = None
    for i, item in enumerate(mcp_list):
        item_name = (
            item
            if isinstance(item, str)
            else (item.get("name") if isinstance(item, dict) else None)
        )
        if item_name == name:
            existing_idx = i
            existing_entry = item
            break

    status = "added"
    diff = None
    if existing_idx is not None:
        diff = _diff_entry(existing_entry, entry)
        if not diff:
            return "skipped", []
        is_tty = sys.stdin.isatty() and sys.stdout.isatty()
        if force:
            mcp_list[existing_idx] = entry
            status = "replaced"
        elif is_tty:
            log.warning(f"MCP server '{name}' already exists. Replacement diff:")
            # Diff lines drive the confirm prompt below: emit unconditionally
            # (tree_item is always-on, no --verbose gating) so users always
            # see what they are about to confirm.
            for line in diff:
                log.tree_item(line)
            if not click.confirm(f"Replace MCP server '{name}'?", default=False):
                return "skipped", diff
            mcp_list[existing_idx] = entry
            status = "replaced"
        else:
            raise click.UsageError(
                f"MCP server '{name}' already exists in {apm_yml_path}. "
                f"Use --force to replace (non-interactive)."
            )
    else:
        mcp_list.append(entry)

    data[section_name]["mcp"] = mcp_list
    dump_yaml(data, apm_yml_path)
    return status, diff
