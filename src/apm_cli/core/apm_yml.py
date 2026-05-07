"""Schema parser for the targets/target field in apm.yml (#1154).

Rules:
  - 'targets: [a, b]'  -> ['a', 'b']   (canonical, plural)
  - 'target: a'        -> ['a']         (singular sugar)
  - 'target: "a,b"'    -> ['a', 'b']   (CSV sugar)
  - both present       -> raise ConflictingTargetsError
  - neither present    -> []            (empty = auto-detect upstream)

Validates each token against CANONICAL_TARGETS.
"""

from __future__ import annotations

from apm_cli.core.errors import (
    ConflictingTargetsError,
    EmptyTargetsListError,
    UnknownTargetError,
    render_conflicting_schema_error,
    render_unknown_target_error,
)

# Canonical target names accepted by APM.
CANONICAL_TARGETS: frozenset[str] = frozenset(
    {
        "claude",
        "copilot",
        "cursor",
        "opencode",
        "codex",
        "gemini",
        "windsurf",
        "agent-skills",
    }
)


def _validate_canonical(tokens: list[str]) -> None:
    """Validate every token is in CANONICAL_TARGETS. Raises UnknownTargetError."""
    for token in tokens:
        if token not in CANONICAL_TARGETS:
            raise UnknownTargetError(render_unknown_target_error(token, sorted(CANONICAL_TARGETS)))


def parse_targets_field(yaml_data: dict) -> list[str]:
    """Parse targets/target from raw apm.yml data dict.

    Returns a canonical list of target names. Empty list means neither
    key was present (caller should fall through to auto-detect).
    """
    has_targets = "targets" in yaml_data
    has_target = "target" in yaml_data

    # Mutex check
    if has_targets and has_target:
        raise ConflictingTargetsError(render_conflicting_schema_error())

    if has_targets:
        raw = yaml_data["targets"]
        if raw is None or (isinstance(raw, list) and len(raw) == 0):
            raise EmptyTargetsListError(
                "[x] 'targets:' in apm.yml is empty\n"
                "\n"
                "The targets list must contain at least one target.\n"
                "\n"
                "Fix with one of:\n"
                "\n"
                "  apm targets                            # see all supported harnesses\n"
                "  apm install <pkg> --target claude\n"
                "  apm init\n"
                "\n"
                "Or update apm.yml:\n"
                "\n"
                "  targets:\n"
                "    - claude"
            )
        if not isinstance(raw, list):
            # Single value under targets: key, treat as one-element list
            raw = [str(raw)]
        tokens = [str(t).strip() for t in raw if str(t).strip()]
        _validate_canonical(tokens)
        return tokens

    if has_target:
        raw = yaml_data["target"]
        if raw is None:
            return []
        raw_str = str(raw).strip()
        if not raw_str:
            return []
        # CSV sugar: "claude,copilot" -> ['claude', 'copilot']
        tokens = [t.strip() for t in raw_str.split(",") if t.strip()]
        _validate_canonical(tokens)
        return tokens

    # Neither key present
    return []
