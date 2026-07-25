"""OpenCode agent frontmatter validation (Phase 1 of #581).

OpenCode's loadAgent() calls Agent.safeParse() on parsed YAML
frontmatter; on validation failure it raises an uncaught
InvalidError and OpenCode fails to start. APM previously deployed
agent files verbatim to .opencode/agents/, so a Claude-style agent
file (tools as string/array, named color) would silently install and
then crash OpenCode at runtime.

This module inspects frontmatter for the known Zod-fatal shapes and
returns human-readable warning messages. It does NOT mutate the
frontmatter and does NOT block installation; the install path emits
the warnings via the diagnostics collector so the user understands
why OpenCode will refuse to load the agent.

Phase 2 (per-target frontmatter transformer) is tracked separately
and is intentionally out of scope here.
"""

from __future__ import annotations

import re
from pathlib import Path

from apm_cli.utils.diagnostics import printable_ascii_text

# OpenCode theme color enum (see sst/opencode config schema).
OPENCODE_THEME_COLORS = frozenset(
    {"primary", "secondary", "accent", "success", "warning", "error", "info"}
)

# Hex color regex: #RGB or #RRGGBB, case-insensitive.
_HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def _ascii_repr(value: object) -> str:
    """Return an ASCII-only repr of ``value``.

    Drop-in replacement for ``!r`` interpolation: ``ascii()`` escapes
    any non-ASCII codepoints (e.g. ``'cy\\xe1n'``) so diagnostics never
    leak raw non-ASCII bytes from user frontmatter into CLI output.
    """
    return ascii(value)


def validate_opencode_frontmatter(
    fm: dict | None,
    source: Path,
    package_name: str | None = None,
) -> list[str]:
    """Return ASCII warning messages for OpenCode-incompatible fields.

    Empty list means no incompatibilities were detected. The caller
    is responsible for surfacing each message via the diagnostics
    collector; this function is pure.

    Args:
        fm: Parsed YAML frontmatter (may be ``None`` or empty dict).
        source: Source agent file path, used in messages so the user
            can locate the offending file.
        package_name: Optional owning APM package name. When provided,
            the warning identifies the agent as ``<pkg>/<file>`` so
            users running multi-package installs can tell which
            dependency the bad frontmatter came from.
    """
    if not fm or not isinstance(fm, dict):
        return []

    messages: list[str] = []
    safe_name = printable_ascii_text(source.name)
    identifier = f"{printable_ascii_text(package_name)}/{safe_name}" if package_name else safe_name

    if "tools" in fm:
        tools = fm["tools"]
        if not isinstance(tools, dict):
            kind = type(tools).__name__
            messages.append(
                f"OpenCode agent '{identifier}' has tools as {kind}; "
                "OpenCode requires a mapping of tool-name to boolean. "
                "OpenCode will reject this agent at load time. "
                "Fix: rewrite the frontmatter as 'tools: {Read: true, Grep: true}'."
            )
        else:
            for key, value in tools.items():
                if not isinstance(key, str) or not isinstance(value, bool):
                    messages.append(
                        f"OpenCode agent '{identifier}' has a non-boolean tool entry "
                        f"({_ascii_repr(key)}: {_ascii_repr(value)}); OpenCode requires "
                        "string-keyed boolean values. "
                        "OpenCode will reject this agent at load time. "
                        "Fix: set every tool entry to 'true' or 'false' "
                        "(e.g. 'Read: true')."
                    )
                    break

    if "color" in fm:
        color = fm["color"]
        if not _is_valid_opencode_color(color):
            messages.append(
                f"OpenCode agent '{identifier}' has color={_ascii_repr(color)}; "
                "OpenCode requires a hex value (e.g. '#aabbcc') or one of "
                f"{sorted(OPENCODE_THEME_COLORS)}. "
                "OpenCode will reject this agent at load time. "
                "Fix: replace the color with a '#rgb' or '#rrggbb' hex literal "
                "or one of the listed theme names."
            )

    return messages


def _is_valid_opencode_color(value: object) -> bool:
    if not isinstance(value, str):
        return False
    if value in OPENCODE_THEME_COLORS:
        return True
    return bool(_HEX_COLOR_RE.match(value))
