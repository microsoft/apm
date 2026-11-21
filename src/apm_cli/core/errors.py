"""Error hierarchy and renderers for target resolution (#1154).

All error classes inherit from ``click.UsageError`` so Click surfaces
exit code 2 automatically. Renderers return plain ASCII text with no status
symbol: the CLI output owner (Click's ``Error:`` or ``CommandLogger.error``'s
``[x]``) adds exactly one. They use a three-section structure:
  (a) headline (what APM saw)
  (b) actionable commands (>= 3 lines starting with 'apm ')
  (c) apm.yml snippet
"""

from __future__ import annotations

from pathlib import Path

import click

# ---------------------------------------------------------------------------
# Error classes
# ---------------------------------------------------------------------------


class TargetResolutionError(click.UsageError):
    """Base for all target-resolution user errors (exit code 2)."""

    pass


class NoHarnessError(TargetResolutionError):
    """No harness signal detected and no explicit target set."""

    pass


class AmbiguousHarnessError(TargetResolutionError):
    """Multiple distinct harness signals detected -- user must disambiguate."""

    pass


class UnknownTargetError(TargetResolutionError):
    """A target token is not in the canonical set."""

    pass


class ConflictingTargetsError(TargetResolutionError):
    """apm.yml contains both 'target:' and 'targets:' (mutex)."""

    pass


class EmptyTargetsListError(TargetResolutionError):
    """apm.yml 'targets:' is present but empty list."""

    pass


# ---------------------------------------------------------------------------
# Renderer helpers
# ---------------------------------------------------------------------------


def _signal_list() -> str:
    """The harness markers detection scans, derived from its one owner (C5d)."""
    from apm_cli.core.target_detection import SIGNAL_WHITELIST

    return ", ".join(
        f"{path}/" if kind == "dir" else path for _target, kind, path in SIGNAL_WHITELIST
    )


def render_no_harness_error(project_root: Path | None = None) -> str:
    """Render the 3-section error for 'no signal detected'.

    Convergence item 10: simplified copy with discovery-before-action.
    """
    return (
        "No harness detected\n"
        "\n"
        f"APM scanned for harness markers ({_signal_list()})"
        " but found none in this project.\n"
        "\n"
        "Previously APM defaulted to copilot; this is now explicit.\n"
        "\n"
        "Fix with one of:\n"
        "\n"
        "  apm targets                            # see all supported harnesses\n"
        "  apm install <pkg> --target claude      # deploy to a specific harness\n"
        "  apm install <pkg> --target copilot     # or any supported target\n"
        "\n"
        "Or declare in apm.yml:\n"
        "\n"
        "  targets:\n"
        "    - claude"
    )


def render_ambiguous_error(project_root: Path | None, detected: list[str]) -> str:
    """Render the 3-section error for 'multiple harnesses detected'."""
    detected_csv = ", ".join(detected)
    # Both remedies name the whole detected set, so either one converges.
    declared = "\n".join(f"    - {target}" for target in detected)
    return (
        f"Multiple harnesses detected: {detected_csv}\n"
        "\n"
        f"APM found signals for {detected_csv} but cannot decide which\n"
        "to deploy to. Pin your targets explicitly.\n"
        "\n"
        "Fix with one of:\n"
        "\n"
        f"  apm install --target {','.join(detected)}\n"
        f"  apm install --target {','.join(detected)} --dry-run   # preview first\n"
        "  apm targets                            # see all detected harnesses\n"
        "\n"
        "Or declare in apm.yml:\n"
        "\n"
        "  targets:\n"
        f"{declared}"
    )


def render_unknown_target_error(
    value: str,
    valid: list[str],
    *,
    command: str = "install",
) -> str:
    """Render the 3-section error for unknown target token."""
    visible_sorted = sorted(valid)
    suggestion = (
        "copilot"
        if "copilot" in visible_sorted
        else (visible_sorted[0] if visible_sorted else "claude")
    )
    from apm_cli.core.target_catalog import target_value_label

    valid_csv = (
        ", ".join(target_value_label(value) for value in visible_sorted)
        if visible_sorted
        else suggestion
    )
    # Strip bracket/quote noise that can leak in from misparsed tokens
    # (e.g. "['copilot'"). Defense-in-depth: callers should pass clean
    # values, but this keeps the headline readable if they don't. Fall
    # back to the raw value (or "<empty>") if stripping consumes
    # everything, so the headline remains actionable.
    display_value = value.strip("[]'\" ") or value or "<empty>"
    if command == "compile":
        return (
            f"Unknown target '{display_value}'\n"
            "\n"
            f"Valid targets: {valid_csv}\n"
            "\n"
            "Fix with one of:\n"
            "\n"
            "  apm targets                            # see all supported harnesses\n"
            f"  apm compile --target {suggestion}\n"
            "  apm compile --dry-run"
        )
    return (
        f"Unknown target '{display_value}'\n"
        "\n"
        f"Valid targets: {valid_csv}\n"
        "\n"
        "Fix with one of:\n"
        "\n"
        "  apm targets                            # see all supported harnesses\n"
        f"  apm install <pkg> --target {suggestion}\n"
        "  apm install <pkg> --dry-run\n"
        "\n"
        "Or declare in apm.yml:\n"
        "\n"
        "  targets:\n"
        f"    - {suggestion}"
    )


def render_conflicting_schema_error() -> str:
    """Render the 3-section error for target/targets mutex."""
    return (
        "Cannot use both 'target:' and 'targets:' in apm.yml\n"
        "\n"
        "Use the canonical plural form:\n"
        "\n"
        "Fix with one of:\n"
        "\n"
        "  apm targets                            # see all supported harnesses\n"
        "  apm install <pkg> --target claude\n"
        "  apm init                               # regenerate apm.yml\n"
        "\n"
        "Or update apm.yml to use the canonical form:\n"
        "\n"
        "  targets:\n"
        "    - claude\n"
        "    - copilot"
    )
