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
