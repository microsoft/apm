"""Interactive target selection helpers for ``apm init``."""

from __future__ import annotations

import click

from ..core.target_detection import EXPLICIT_ONLY_TARGETS
from ._helpers import INFO, RESET

_PROMPT_TARGETS_ORDERED: list[str] = [
    "copilot",
    "claude",
    "cursor",
    "opencode",
    "codex",
    "gemini",
    "windsurf",
]


def _is_done_response(response: str) -> bool:
    """Return True when the response ends the toggle loop (empty or 'done')."""
    s = response.strip()
    return not s or s.lower() == "done"


def _parse_chunk(chunk: str, max_n: int) -> tuple[list[int], str | None]:
    """Parse a single toggle chunk (number or N-M range). Returns (indices, error)."""
    if "-" in chunk:
        parts = chunk.split("-")
        if len(parts) != 2 or not all(part.isdigit() for part in parts):
            return [], f"Invalid range '{chunk}'. Use form 'N-M'."
        lo, hi = int(parts[0]), int(parts[1])
        if lo < 1 or hi > max_n or lo > hi:
            return [], f"Range '{chunk}' out of bounds (valid: 1-{max_n})."
        return list(range(lo - 1, hi)), None
    if not chunk.isdigit():
        return [], f"Invalid token '{chunk}'."
    number = int(chunk)
    if number < 1 or number > max_n:
        return [], f"Number {number} out of bounds (valid: 1-{max_n})."
    return [number - 1], None


def _parse_toggle_input(response: str, max_n: int) -> tuple[list[int], str | None]:
    """Parse toggle input. Returns (zero-based indices, error message or None)."""
    response = response.strip().lower().replace(" ", "")
    if not response:
        return [], None
    if response in ("all", "none"):
        return list(range(max_n)), None
    indices: list[int] = []
    for chunk in response.split(","):
        if not chunk:
            continue
        chunk_indices, err = _parse_chunk(chunk, max_n)
        if err:
            return [], err
        indices.extend(chunk_indices)
    return indices, None


def _render_target_choices(targets: list, selected: list, signal_hints: dict) -> str:
    """Render the numbered toggle list for target selection."""
    lines = []
    for index, target in enumerate(targets, start=1):
        mark = "[x]" if selected[index - 1] else "[ ]"
        hint = signal_hints.get(target, "")
        line = f"  {index}. {mark} {target}"
        if hint:
            line += f"  {hint}"
        lines.append(line)
    return "\n".join(lines)


def _prompt_target_selection(
    prechecked: set[str],
    signal_hints: dict[str, str],
) -> list[str] | None:
    """Interactive numbered-toggle target selection."""
    targets = [target for target in _PROMPT_TARGETS_ORDERED if target not in EXPLICIT_ONLY_TARGETS]
    selected: list[bool] = [target in prechecked for target in targets]

    click.echo("\nSelect targets for this project:")
    click.echo(_render_target_choices(targets, selected, signal_hints))
    if not any(signal_hints.values()):
        click.echo("  (no signals detected)")

    click.echo(
        f"\n{INFO}[i] Tip: select the tools your team uses. You can change this later"
        f"\n    with 'apm targets set <target,...>' or edit apm.yml directly.{RESET}"
    )
    click.echo(
        f"{INFO}[i] Type a number to toggle, ranges like '1-3' or '1,3,5' for multiple,"
        f"\n    'all' / 'none' to flip every entry, or press Enter to confirm.{RESET}"
    )

    while True:
        response = click.prompt(
            f"Toggle (1-{len(targets)}, ranges, 'all'/'none', or Enter to confirm)",
            default="",
            show_default=False,
        )
        if _is_done_response(response):
            break

        indices, err = _parse_toggle_input(response, len(targets))
        if err:
            click.echo(f"  {err}")
            continue
        for idx in indices:
            selected[idx] = not selected[idx]
        click.echo(_render_target_choices(targets, selected, signal_hints))

    chosen = [targets[index] for index, is_selected in enumerate(selected) if is_selected]
    if chosen:
        return chosen

    click.echo(
        f"\n{INFO}[!] No targets selected. APM will auto-detect targets from your"
        "\n    filesystem on every compile (e.g. .github/ -> copilot)."
        f"\n    To pin targets later: apm targets set <target,...>{RESET}"
    )
    if click.confirm("\nContinue without pinning targets?", default=True):
        return None
    return _prompt_target_selection(prechecked, signal_hints)
