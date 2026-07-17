"""Schema parser for the targets/target field in apm.yml (#1154).

Rules:
  - 'targets: [a, b]'  -> ['a', 'b']   (canonical, plural)
  - 'target: a'        -> ['a']         (singular sugar)
  - 'target: "a,b"'    -> ['a', 'b']   (CSV sugar)
  - 'target: [a, b]'   -> ['a', 'b']   (list sugar under singular key, #1188)
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
from apm_cli.core.target_catalog import TARGET_CAPABILITIES, manifest_target_names

# Canonical target names accepted by APM.
CANONICAL_TARGETS: frozenset[str] = manifest_target_names()

# --- Legacy 'all' migration bridge (#2271) ---------------------------------
# 'all' predates the canonical catalog (#1154): manifests published before
# 0.25.0 could legally declare `targets: [all]`, meaning "no restriction".
# The catalog keeps 'all' flag-only by design, but already-published tags
# cannot be re-validated, so the parser folds the token to the
# field-omitted behavior (fall through to --target / auto-detect) and warns
# once per process. Hard rejection returns after a deprecation window.

_legacy_all_warned: bool = False


def _reset_legacy_all_warning() -> None:
    """Test hook: re-arm the once-per-process legacy 'all' warning."""
    global _legacy_all_warned
    _legacy_all_warned = False


def _fold_legacy_all(tokens: list[str]) -> tuple[list[str], bool]:
    """Split the legacy 'all' token out of *tokens*.

    Returns ``(remaining_tokens, folded)`` where *folded* is True when the
    token was present. Callers validate the remainder first and only then
    emit the warning via :func:`_warn_legacy_all_once`, so a manifest that
    still hard-fails on a sibling token does not consume the warning latch.
    """
    if "all" not in tokens:
        return tokens, False
    return [t for t in tokens if t != "all"], True


def _warn_legacy_all_once() -> None:
    """Emit the legacy 'all' deprecation warning at most once per process.

    Dependency graphs can contain many affected manifests; one warning is
    enough. The unlocked check-then-set is a benign race: a concurrent
    parse can at worst duplicate the warning, never suppress it.
    """
    global _legacy_all_warned
    if _legacy_all_warned:
        return
    _legacy_all_warned = True
    from apm_cli.utils.console import _rich_warning

    _rich_warning(
        "'all' in apm.yml targets is deprecated -- treating the field as "
        "omitted so --target / auto-detect decide (its legacy meaning; any "
        "sibling targets listed alongside 'all' are ignored). Remove the "
        "field from the manifest; 'all' will become a hard error in a "
        "future release.",
        symbol="warning",
    )


def _validate_canonical(tokens: list[str]) -> None:
    """Validate every token is in CANONICAL_TARGETS. Raises UnknownTargetError."""
    for token in tokens:
        capability = TARGET_CAPABILITIES.get(token)
        if capability is None or capability.experimental_flag is not None or capability.mcp_only:
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
        tokens, folded = _fold_legacy_all(tokens)
        _validate_canonical(tokens)
        if folded:
            _warn_legacy_all_once()
            return []
        return tokens

    if has_target:
        raw = yaml_data["target"]
        if raw is None:
            return []
        if isinstance(raw, list):
            # YAML list sugar: 'target: [claude, copilot]' or block list.
            # Empty list under singular key falls through to auto-detect
            # (consistent with 'target:' with no value).
            tokens = [str(t).strip() for t in raw if str(t).strip()]
            if not tokens:
                return []
            tokens, folded = _fold_legacy_all(tokens)
            _validate_canonical(tokens)
            if folded:
                _warn_legacy_all_once()
                return []
            return tokens
        raw_str = str(raw).strip()
        if not raw_str:
            return []
        # CSV sugar: "claude,copilot" -> ['claude', 'copilot']
        tokens = [t.strip() for t in raw_str.split(",") if t.strip()]
        tokens, folded = _fold_legacy_all(tokens)
        _validate_canonical(tokens)
        if folded:
            _warn_legacy_all_once()
            return []
        return tokens

    # Neither key present
    return []
