"""Unit tests for the target resolution v2 algorithm (#1154).

# Implementation contract assumed by these tests (TDD-red):
#
# Module: apm_cli.core.target_detection
#   - detect_signals(project_root: Path) -> list[Signal]
#       Returns Signal objects for every detection-whitelist marker found
#       under project_root. AGENTS.md and bare .github/ are NOT signals.
#       Each Signal exposes:
#           .target  : canonical target name ('claude', 'copilot', ...)
#           .source  : human-readable signal descriptor
#                      (e.g. 'CLAUDE.md', '.claude/', '.cursorrules',
#                      '.github/copilot-instructions.md')
#
#   - resolve_targets(
#         project_root: Path,
#         *,
#         flag: str | list[str] | None = None,
#         yaml_targets: list[str] | None = None,
#     ) -> ResolvedTargets
#       Priority: flag > yaml_targets > auto-detect signals.
#       Zero auto-detect signals + no flag + no yaml -> raises NoHarnessError.
#       2+ auto-detect signals + no flag + no yaml -> raises AmbiguousHarnessError.
#       Explicit (flag or yaml) -> ResolvedTargets.auto_create == True for
#       every resolved target (three-guard collapse).
#       Auto-detect path also sets auto_create=True for the resolved target
#       so missing-but-detected dirs (e.g. CLAUDE.md without .claude/) get
#       materialized.
#
#   ResolvedTargets fields:
#       .targets   : list[str]  (sorted, canonical names)
#       .source    : str        (one of '--target flag', 'apm.yml',
#                                'auto-detect from <signal_csv>')
#       .auto_create : bool
#
#   - expand_all_targets(
#         project_root: Path,
#         *,
#         yaml_targets: list[str] | None = None,
#     ) -> list[str]
#       Resolves --target all to (signals U yaml_targets), NOT all 7
#       supported targets. Empty result -> NoHarnessError.
#
# Module: apm_cli.core.apm_yml
#   - parse_targets_field(yaml_data: dict) -> list[str]
#       Accepts 'targets:' list OR 'target:' singular sugar; returns
#       canonical list. BOTH present -> ConflictingTargetsError.
#       'target: "claude,copilot"' CSV is also accepted.
#
# Module: apm_cli.core.errors
#   - NoHarnessError, AmbiguousHarnessError, UnknownTargetError,
#     ConflictingTargetsError -- all subclass click.UsageError so Click
#     surfaces exit code 2 automatically.
"""

from __future__ import annotations

import pytest

# These imports are expected to fail on current main; that is the TDD-red point.
from apm_cli.core.apm_yml import parse_targets_field
from apm_cli.core.errors import (
    ConflictingTargetsError,
    UnknownTargetError,
)
from apm_cli.core.target_catalog import TARGET_CAPABILITIES
from apm_cli.models.manifest_targets import option_target_names

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Signal whitelist
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Resolution priority
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# apm.yml schema
# ---------------------------------------------------------------------------


def test_option_target_names_none_preserves_autodetect():
    assert option_target_names(None) is None


def test_option_target_names_aliases_to_manifest_names():
    assert option_target_names(["claude", "vscode"]) == ["claude", "copilot"]


def test_option_target_names_all_expands_to_manifest_names():
    targets = option_target_names("all")
    assert targets is not None
    assert "all" not in targets
    assert "vscode" not in targets
    assert "copilot" in targets
    assert parse_targets_field({"targets": targets}) == targets


def test_option_target_names_drops_experimental_targets():
    assert option_target_names(["openclaw", "hermes", "grok-cloud", "agy"]) == [
        "antigravity",
        "hermes",
    ]


@pytest.mark.parametrize(
    ("alias", "canonical"),
    sorted(
        (alias, capability.name)
        for capability in TARGET_CAPABILITIES.values()
        for alias in capability.aliases
    ),
)
def test_option_target_names_canonicalizes_every_alias(alias, canonical):
    """Every catalog alias persists exactly like its canonical spelling (#3842)."""
    persisted = option_target_names(alias)
    assert persisted == option_target_names(canonical)
    assert persisted is None or alias not in persisted


def test_option_target_names_preserves_grok_build():
    assert option_target_names(["grok-build"]) == ["grok-build"]
    assert parse_targets_field({"targets": ["grok-build"]}) == ["grok-build"]


def test_schema_targets_list_valid():
    out = parse_targets_field({"targets": ["claude", "copilot"]})
    assert sorted(out) == ["claude", "copilot"]


def test_schema_target_singular_sugar():
    out = parse_targets_field({"target": "claude"})
    assert out == ["claude"]


def test_schema_both_target_and_targets_error():
    with pytest.raises(ConflictingTargetsError):
        parse_targets_field({"target": "claude", "targets": ["copilot"]})


# ---------------------------------------------------------------------------
# CSV / unknown-target validation
# ---------------------------------------------------------------------------


def test_csv_target_parsing():
    out = parse_targets_field({"target": "claude,cursor"})
    assert sorted(out) == ["claude", "cursor"]


def test_unknown_target_rejected():
    with pytest.raises(UnknownTargetError) as exc_info:
        parse_targets_field({"target": "unknown"})
    msg = str(exc_info.value).lower()
    # Must list at least one canonical target in the suggestion.
    assert "claude" in msg or "copilot" in msg


# ---------------------------------------------------------------------------
# YAML list under 'target:' singular key (#1188)
# ---------------------------------------------------------------------------


def test_target_singular_with_yaml_list_two_items():
    """Regression: 'target: [copilot, claude]' YAML flow-list form (#1188)."""
    out = parse_targets_field({"target": ["copilot", "claude"]})
    assert sorted(out) == ["claude", "copilot"]


def test_target_singular_with_yaml_list_single_item():
    """Single-item list under 'target:' must equal scalar form."""
    out = parse_targets_field({"target": ["copilot"]})
    assert out == ["copilot"]


def test_target_singular_with_yaml_list_whitespace_tolerated():
    """List elements with surrounding whitespace are stripped."""
    out = parse_targets_field({"target": ["  copilot  ", "claude\t"]})
    assert sorted(out) == ["claude", "copilot"]


def test_target_singular_with_empty_list_falls_through_to_autodetect():
    """'target: []' under SINGULAR key returns [] (auto-detect upstream),
    matching 'target:' with no value. Only PLURAL 'targets: []' raises."""
    out = parse_targets_field({"target": []})
    assert out == []


def test_target_singular_with_yaml_list_unknown_token_rejected():
    """Garbled tokens from list parsing must surface a clean error."""
    with pytest.raises(UnknownTargetError) as exc_info:
        parse_targets_field({"target": ["nonsense"]})
    msg = str(exc_info.value)
    headline = msg.splitlines()[0]
    # Headline must contain the bare token, not a Python list repr.
    assert "'nonsense'" in headline
    # No list-repr leakage: the leading "[x]" symbol is fine, but no
    # "['nonsense'" or similar should appear as the value.
    assert "['nonsense'" not in headline
    assert '["nonsense"' not in headline


def test_target_singular_with_yaml_list_non_string_coerced():
    """Non-string list elements coerce via str() and are validated."""
    with pytest.raises(UnknownTargetError):
        parse_targets_field({"target": [42]})


def test_target_singular_with_all_token_folds_to_auto_detect():
    """Legacy 'all' folds the field to auto-detect (deprecation bridge, #2271).

    'all' stays a CLI flag-only meta-target by design, but manifests
    published before the canonical catalog (#1154) carry it inside
    already-released tags. The parser treats such a field as omitted
    (fall through to --target / auto-detect) instead of hard-failing,
    until the deprecation window closes.
    """
    from apm_cli.core.apm_yml import _reset_legacy_all_warning

    _reset_legacy_all_warning()
    # Mutation guard: deleting the legacy-all fold makes this raise UnknownTargetError.
    assert parse_targets_field({"target": ["all", "claude"]}) == []


def test_targets_plural_with_all_folds_to_auto_detect(capsys):
    """`targets: [all]` returns [] and warns once per process."""
    from apm_cli.core.apm_yml import _reset_legacy_all_warning

    _reset_legacy_all_warning()
    assert parse_targets_field({"targets": ["all"]}) == []
    out = capsys.readouterr()
    combined = out.out + out.err
    assert "deprecated" in combined
    # Second parse in the same process stays silent (once-per-process guard).
    assert parse_targets_field({"targets": ["all"]}) == []
    out = capsys.readouterr()
    assert "deprecated" not in (out.out + out.err)


def test_target_csv_with_all_folds_to_auto_detect():
    """CSV sugar containing 'all' folds the whole field to auto-detect."""
    from apm_cli.core.apm_yml import _reset_legacy_all_warning

    _reset_legacy_all_warning()
    assert parse_targets_field({"target": "all"}) == []
    assert parse_targets_field({"target": "all,claude"}) == []


def test_targets_with_all_still_validates_siblings(capsys):
    """Unknown sibling tokens are still rejected even when 'all' folds.

    The hard-failing manifest must not consume the once-per-process
    warning latch: a later valid legacy manifest still warns.
    """
    from apm_cli.core.apm_yml import _reset_legacy_all_warning

    _reset_legacy_all_warning()
    with pytest.raises(UnknownTargetError):
        parse_targets_field({"targets": ["all", "nonsense"]})
    capsys.readouterr()
    assert parse_targets_field({"targets": ["all"]}) == []
    out = capsys.readouterr()
    assert "deprecated" in (out.out + out.err)


def test_target_singular_with_yaml_list_deduplicates():
    """Duplicates collapse, including a deprecated alias of a listed target."""
    assert parse_targets_field({"target": ["copilot", "copilot"]}) == ["copilot"]
    assert parse_targets_field({"targets": ["vscode", "claude", "copilot"]}) == [
        "copilot",
        "claude",
    ]


# ---------------------------------------------------------------------------
# --target all expansion
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Three-guard collapse: explicit always materializes
# ---------------------------------------------------------------------------
