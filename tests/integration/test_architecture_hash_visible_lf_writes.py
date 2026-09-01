"""Architecture guardrails for generated files inside raw-hashed package trees."""

from __future__ import annotations

from pathlib import Path

from scripts.architecture_linter.runner import registered_rules, run_selected_rules

_RULES_BY_ID = {rule.id: rule for rule in registered_rules()}


def test_hash_visible_writes_route_through_lf_helpers() -> None:
    """The registered rule accepts every canonical hash-visible writer."""
    root = Path(__file__).parents[2]
    report = run_selected_rules(
        root,
        ("marketplace-integrations-hash-visible-lf-writers",),
    )
    rule = _RULES_BY_ID["marketplace-integrations-hash-visible-lf-writers"]

    assert report.failures == ()
    assert report.violations == ()
    assert "Hash-visible generated files route through canonical LF writers" in rule.description


def test_hash_visible_write_guard_rejects_platform_native_bypass() -> None:
    """The guard rejects restoring a direct platform-native text write."""
    root = Path(__file__).parents[2]
    path = "src/apm_cli/deps/plugin_parser.py"
    source = (root / path).read_text(encoding="utf-8")
    expected = "write_text_lf(apm_yml_path, apm_yml_content)"
    assert expected in source
    mutated = source.replace(
        expected,
        'apm_yml_path.write_text(apm_yml_content, encoding="utf-8")',
        1,
    )

    report = run_selected_rules(
        root,
        ("marketplace-integrations-hash-visible-lf-writers",),
        source_overrides={path: mutated},
    )

    messages = [violation.message for violation in report.violations]
    assert report.failures == ()
    assert any(
        "synthesize_apm_yml_from_plugin must call write_text_lf exactly once" in m for m in messages
    )
    assert any("bypasses canonical LF writer via write_text" in m for m in messages)
