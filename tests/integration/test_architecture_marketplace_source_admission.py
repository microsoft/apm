"""Architecture guardrails for marketplace source admission."""

from __future__ import annotations

from pathlib import Path

from scripts.architecture_linter.runner import registered_rules, run_selected_rules

_RULE_ID = "marketplace-integrations-source-admission"
_RULES_BY_ID = {rule.id: rule for rule in registered_rules()}


def test_marketplace_source_admission_has_single_owner() -> None:
    """Commands, persisted models, and clients must consume one source parser."""
    root = Path(__file__).parents[2]
    owner = (root / "src/apm_cli/marketplace/source_identity.py").read_text(encoding="utf-8")
    command = (root / "src/apm_cli/commands/marketplace/__init__.py").read_text(encoding="utf-8")
    model = (root / "src/apm_cli/marketplace/models.py").read_text(encoding="utf-8")
    client = (root / "src/apm_cli/marketplace/client.py").read_text(encoding="utf-8")
    rule = _RULES_BY_ID[_RULE_ID]

    assert owner.count("def parse_marketplace_source(") == 1
    assert "identity = parse_marketplace_source(source, host_flag)" in command
    assert "identity = parse_marketplace_source(self.url)" in model
    assert "def _host_from_url(" not in client
    assert "host = source.host" in client
    assert "is_valid_fqdn" not in command
    assert (
        "Marketplace source admission stays owned by marketplace/source_identity.py"
        in rule.description
    )


def test_marketplace_source_admission_guard_rejects_client_host_parser() -> None:
    """The registered rule rejects a client-side host parser."""
    root = Path(__file__).parents[2]
    path = "src/apm_cli/marketplace/client.py"
    mutated = (root / path).read_text(encoding="utf-8")
    mutated += "\n\ndef _host_from_url(url: str) -> str:\n    return url\n"

    report = run_selected_rules(
        root,
        (_RULE_ID,),
        source_overrides={path: mutated},
    )

    assert report.failures == ()
    assert any(violation.rule_id == _RULE_ID for violation in report.violations)


def test_registration_source_grammar_has_single_owner() -> None:
    """``marketplace add`` and the user-manifest reconcile share one SOURCE parser."""
    root = Path(__file__).parents[2]
    owner = (root / "src/apm_cli/marketplace/registration.py").read_text(encoding="utf-8")
    command = (root / "src/apm_cli/commands/marketplace/__init__.py").read_text(encoding="utf-8")
    reconcile = (root / "src/apm_cli/marketplace/reconcile.py").read_text(encoding="utf-8")

    assert owner.count("def parse_registration_source(") == 1
    assert "identity = parse_marketplace_source(source_arg, host)" in owner
    assert "parsed = parse_registration_source(source, ref=explicit_ref, host=host)" in command
    assert "parsed = parse_registration_source(item.source, ref=item.ref)" in reconcile
    assert "split_source_fragment_ref(source" not in command
    assert "url_names_remote_manifest(" not in command
    assert "split_source_fragment_ref(" not in reconcile


def test_source_admission_guard_rejects_add_side_fragment_parser() -> None:
    """The registered rule rejects ``marketplace add`` re-parsing the SOURCE grammar."""
    root = Path(__file__).parents[2]
    path = "src/apm_cli/commands/marketplace/__init__.py"
    original = (root / path).read_text(encoding="utf-8")
    needle = "            parsed = parse_registration_source(source, ref=explicit_ref, host=host)"
    assert original.count(needle) == 1
    # Keep the shared-parser call so only the re-parse is under test.
    mutated = original.replace(
        needle,
        "            source_arg, fragment_ref = _split_source_fragment_ref(source)\n" + needle,
    )

    report = run_selected_rules(root, (_RULE_ID,), source_overrides={path: mutated})

    assert report.failures == ()
    messages = [v.message for v in report.violations if v.rule_id == _RULE_ID]
    assert (
        "Marketplace SOURCE must be parsed through registration.parse_registration_source"
        in messages
    )


def test_source_admission_guard_rejects_reconcile_side_fragment_parser() -> None:
    """The user-manifest reconcile cannot re-parse SOURCE beside the shared owner."""
    root = Path(__file__).parents[2]
    path = "src/apm_cli/marketplace/reconcile.py"
    original = (root / path).read_text(encoding="utf-8")
    needle = "        parsed = parse_registration_source(item.source, ref=item.ref)"
    assert original.count(needle) == 1
    mutated = original.replace(
        needle, needle + "\n        _url, _frag = split_source_fragment_ref(item.source)"
    )

    report = run_selected_rules(root, (_RULE_ID,), source_overrides={path: mutated})

    assert report.failures == ()
    messages = [v.message for v in report.violations if v.rule_id == _RULE_ID]
    assert (
        "Marketplace SOURCE must be parsed through registration.parse_registration_source"
        in messages
    )
