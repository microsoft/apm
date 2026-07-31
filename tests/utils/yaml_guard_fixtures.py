"""Bounded YAML fixtures shared by parser and installed-CLI tests."""

from __future__ import annotations

LARGE_LOCKFILE_PROVENANCE_ROWS = 10_000
PRIVATE_LOCKFILE_MARKER = "DO_NOT_LEAK_LOCK_CONTENT"


def large_anchor_free_lockfile(expansion_weight: int) -> str:
    """Return a realistic tree-shaped lockfile just beyond the old budget."""
    value = "x" * (expansion_weight // LARGE_LOCKFILE_PROVENANCE_ROWS + 1)
    provenance = "".join(
        f"  server-{index:05d}: {value}\n" for index in range(LARGE_LOCKFILE_PROVENANCE_ROWS)
    )
    return (
        'lockfile_version: "1"\n'
        'generated_at: "2026-07-31T00:00:00Z"\n'
        'apm_version: "0.26.0"\n'
        "dependencies: []\n"
        "deployments: []\n"
        "mcp_servers: []\n"
        "mcp_config_provenance:\n"
        f"{provenance}"
    )


def compact_alias_bomb_lockfile(*, levels: int = 8, fanout: int = 9) -> str:
    """Return a sub-kilobyte lockfile whose aliases expand past the budget."""
    lines = [
        'lockfile_version: "1"',
        'generated_at: "2026-07-31T00:00:00Z"',
        "dependencies: []",
        "deployments: []",
        "mcp_config_provenance:",
        f"  private-note: {PRIVATE_LOCKFILE_MARKER}",
        "  level0: &level0 [x]",
    ]
    previous = "level0"
    for level in range(1, levels + 1):
        anchor = f"level{level}"
        aliases = ", ".join([f"*{previous}"] * fanout)
        lines.append(f"  {anchor}: &{anchor} [{aliases}]")
        previous = anchor
    return "\n".join(lines) + "\n"
