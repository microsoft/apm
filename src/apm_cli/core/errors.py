"""Error hierarchy and renderers for target resolution (#1154).

All error classes inherit from ``click.UsageError`` so Click surfaces
exit code 2 automatically. Renderers return ASCII-only strings with
a three-section structure:
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

_SIGNAL_LIST = (
    ".claude/, CLAUDE.md, .cursor/, .cursorrules, "
    ".github/copilot-instructions.md, .codex/, .gemini/, GEMINI.md, "
    ".opencode/, .windsurf/"
)


def render_no_harness_error(project_root: Path | None = None) -> str:
    """Render the 3-section error for 'no signal detected'.

    Convergence item 10: simplified copy with discovery-before-action.
    """
    return (
        "[x] No harness detected\n"
        "\n"
        f"APM scanned for harness markers ({_SIGNAL_LIST})"
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
    return (
        f"[x] Multiple harnesses detected: {detected_csv}\n"
        "\n"
        f"APM found signals for {detected_csv} but cannot decide which\n"
        "to deploy to. Pin your target explicitly.\n"
        "\n"
        "Fix with one of:\n"
        "\n"
        f"  apm install <pkg> --target {detected[0]}\n"
        "  apm install <pkg> --dry-run            # preview what each target does\n"
        "  apm targets                            # see all detected harnesses\n"
        "\n"
        "Or declare in apm.yml:\n"
        "\n"
        "  targets:\n"
        f"    - {detected[0]}"
    )


def render_unknown_target_error(value: str, valid: list[str]) -> str:
    """Render the 3-section error for unknown target token."""
    # Hide ``agent-skills`` from the user-facing suggestion surface (#1208).
    # ``agent-skills`` is a meta-target (multi-harness fan-out to
    # ``.agents/skills/``) and is intentionally excluded from the
    # ``apm targets`` table -- it has no single ``deploy_dir`` and is not
    # what a beginner who mistyped a harness name should be steered toward.
    # The canonical set still accepts it so power users who pass it
    # explicitly via ``--target agent-skills`` (or list it in apm.yml)
    # continue to work; we just don't advertise it here.
    visible = [t for t in valid if t != "agent-skills"]
    visible_sorted = sorted(visible)
    suggestion = (
        "copilot"
        if "copilot" in visible_sorted
        else (visible_sorted[0] if visible_sorted else "claude")
    )
    # When the caller passes only the hidden meta-target (or an empty
    # list), fall back to the safety-net suggestion so the "Valid
    # targets:" line never renders as a bare colon. In practice all
    # production call sites pass the full canonical set, so this is a
    # defense-in-depth path for tests and future callers.
    valid_csv = ", ".join(visible_sorted) if visible_sorted else suggestion
    # Strip bracket/quote noise that can leak in from misparsed tokens
    # (e.g. "['copilot'"). Defense-in-depth: callers should pass clean
    # values, but this keeps the headline readable if they don't. Fall
    # back to the raw value (or "<empty>") if stripping consumes
    # everything, so the headline remains actionable.
    display_value = value.strip("[]'\" ") or value or "<empty>"
    return (
        f"[x] Unknown target '{display_value}'\n"
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
        "[x] Cannot use both 'target:' and 'targets:' in apm.yml\n"
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
