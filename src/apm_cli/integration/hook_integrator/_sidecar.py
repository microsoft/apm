"""Sidecar ownership re-injection for schema-strict targets."""

from __future__ import annotations


def _reinject_apm_source_from_sidecar(hooks: dict, sidecar_data: dict) -> None:
    """Restore _apm_source markers from sidecar into in-memory hook entries.

    Schema-strict targets (e.g. Claude) do not persist ``_apm_source`` in
    their settings file.  Instead, ownership metadata is stored in a
    sidecar file.  This helper re-injects those markers so the rest of
    the integration logic can work with them as normal.

    Each sidecar entry is consumed at most once to prevent falsely claiming
    user-owned hooks that happen to have identical content to an APM hook.

    Args:
        hooks: The ``"hooks"`` dict loaded from the target config file
            (mutated in-place).
        sidecar_data: The dict loaded from the sidecar file.
    """
    for event_name, sidecar_entries in sidecar_data.items():
        if event_name not in hooks or not isinstance(sidecar_entries, list):
            continue
        # Build a consumable pool of (normalised-content, source) pairs.
        pool: list[tuple[dict, str]] = []
        for sc_entry in sidecar_entries:
            if isinstance(sc_entry, dict) and "_apm_source" in sc_entry:
                cmp = {k: v for k, v in sorted(sc_entry.items()) if k != "_apm_source"}
                pool.append((cmp, sc_entry["_apm_source"]))

        for disk_entry in hooks[event_name]:
            if not isinstance(disk_entry, dict) or "_apm_source" in disk_entry:
                continue
            disk_cmp = {k: v for k, v in sorted(disk_entry.items()) if k != "_apm_source"}
            for i, (sc_cmp, source) in enumerate(pool):
                if disk_cmp == sc_cmp:
                    disk_entry["_apm_source"] = source
                    pool.pop(i)
                    break
