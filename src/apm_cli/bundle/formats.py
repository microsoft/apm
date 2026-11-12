"""Canonical bundle format authority for ``apm pack``."""

from __future__ import annotations

import enum
import re


class BundleFormat(str, enum.Enum):
    """Supported bundle output families."""

    AGENT_PLUGIN = "agent-plugin"
    CLAUDE_PLUGIN = "claude-plugin"
    APM = "apm"

    @property
    def lock_value(self) -> str:
        """Return the value written to ``pack.format``."""
        return self.value

    @property
    def display_name(self) -> str:
        """Return a human-friendly label."""
        return {
            BundleFormat.AGENT_PLUGIN: "Agent Plugin",
            BundleFormat.CLAUDE_PLUGIN: "Claude plugin",
            BundleFormat.APM: "APM",
        }[self]


PREFERRED_PLUGIN_FORMAT = BundleFormat.CLAUDE_PLUGIN
# NOTE(reservation #2522-4): flipping PREFERRED_PLUGIN_FORMAT to AGENT_PLUGIN
# is the single switch that activates agent_plugin_warning() below (currently
# unreachable while Claude stays the no-flag default). Do not schedule that
# flip until a real client passes credential-free native-lifecycle
# qualification; see Section 8.5.5 (req-tg-011) of openapm-v0.1.md.

_CLI_CHOICES = ("plugin", "agent-plugin", "claude", "claude-plugin", "apm")
_SELECTOR_ALIASES: dict[str, BundleFormat] = {
    "plugin": BundleFormat.CLAUDE_PLUGIN,
    "agent-plugin": BundleFormat.AGENT_PLUGIN,
    "agent_plugin": BundleFormat.AGENT_PLUGIN,
    "claude": BundleFormat.CLAUDE_PLUGIN,
    "claude-plugin": BundleFormat.CLAUDE_PLUGIN,
    "claude_plugin": BundleFormat.CLAUDE_PLUGIN,
    "apm": BundleFormat.APM,
}
_VERSION_RE = re.compile(r"^(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)")


def cli_format_choices() -> tuple[str, ...]:
    """Return accepted ``--format`` selector tokens."""
    return _CLI_CHOICES


def cli_plugin_format_choices() -> tuple[str, ...]:
    """Return plugin-authoring ``--format`` selector tokens."""
    return tuple(choice for choice in _CLI_CHOICES if choice != BundleFormat.APM.value)


def coerce_bundle_format(value: str | BundleFormat | None) -> BundleFormat:
    """Normalize *value* to a :class:`BundleFormat`."""
    if value is None:
        return PREFERRED_PLUGIN_FORMAT
    if isinstance(value, BundleFormat):
        return value
    key = value.strip().lower().replace(" ", "-").replace("_", "-")
    if key in _SELECTOR_ALIASES:
        return _SELECTOR_ALIASES[key]
    for member in BundleFormat:
        if key == member.value:
            return member
    known = ", ".join(_CLI_CHOICES)
    raise ValueError(f"Unknown bundle format {value!r}. Known selectors: {known}")


def resolve_bundle_format(
    fmt: str | None,
    *,
    claude_plugin: bool = False,
) -> BundleFormat:
    """Resolve CLI selectors to a canonical bundle format.

    ``--format plugin`` preserves its historical Claude exporter behavior.
    ``--format agent-plugin`` explicitly selects portable Agent Plugins output.
    ``--claude-plugin`` is an explicit shortcut for the current default.
    Multiple explicit selectors raise ``ValueError`` so the caller can surface
    a Click usage error, including when two selectors resolve to the same format.
    """
    selections: list[BundleFormat] = []
    if fmt is not None:
        selections.append(coerce_bundle_format(fmt))
    if claude_plugin:
        selections.append(BundleFormat.CLAUDE_PLUGIN)

    if len(selections) > 1:
        selector_text = ", ".join(format_selection_text(fmt, claude_plugin))
        raise ValueError(f"Choose one bundle format selector; received: {selector_text}")
    if selections:
        return selections[0]
    return PREFERRED_PLUGIN_FORMAT


def format_selection_text(
    fmt: str | None,
    claude_plugin: bool,
) -> tuple[str, ...]:
    """Return the explicit selectors used by the caller."""
    selected: list[str] = []
    if fmt is not None:
        selected.append(f"--format {fmt}")
    if claude_plugin:
        selected.append("--claude-plugin")
    return tuple(selected)


def agent_plugin_warning(version: str | None = None) -> str | None:
    """Return the transition warning for the Agent Plugin default window."""
    if PREFERRED_PLUGIN_FORMAT is not BundleFormat.AGENT_PLUGIN:
        return None
    if version is None:
        from ..version import get_version

        version = get_version()
    match = _VERSION_RE.match(version)
    if match is None:
        return None
    major = int(match.group("major"))
    minor = int(match.group("minor"))
    if major == 0 and 29 <= minor <= 33:
        return (
            "apm pack now defaults to Agent Plugin output; use --format claude-plugin "
            "for the legacy Claude plugin bundle."
        )
    return None


__all__ = [
    "PREFERRED_PLUGIN_FORMAT",
    "BundleFormat",
    "agent_plugin_warning",
    "cli_format_choices",
    "cli_plugin_format_choices",
    "coerce_bundle_format",
    "format_selection_text",
    "resolve_bundle_format",
]
