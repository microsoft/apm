"""Architecture coverage for effective host-provider identity ownership."""

import json
from pathlib import Path

import pytest

from scripts.architecture_linter.runner import registered_rules, run_selected_rules

pytestmark = pytest.mark.component

ROOT = Path(__file__).resolve().parents[2]
RULE_ID = "transport-platform-host-credential-resolution"
OWNER = "src/apm_cli/core/host_providers.py"
CONSUMER = "src/apm_cli/drift.py"
LOCK_SEED_CONSUMER = "src/apm_cli/deps/tiered_ref_resolver.py"
REGISTRY = ".apm/architecture/owners/transport-auth-platform.json"


def test_effective_host_provider_identity_has_registered_owner_and_consumer() -> None:
    owner = (ROOT / OWNER).read_text(encoding="utf-8")
    consumer = (ROOT / CONSUMER).read_text(encoding="utf-8")
    lock_seed_consumer = (ROOT / LOCK_SEED_CONSUMER).read_text(encoding="utf-8")
    registry = json.loads((ROOT / REGISTRY).read_text(encoding="utf-8"))
    owners = {entry["id"]: entry for entry in registry["owners"]}
    rule = next(rule for rule in registered_rules() if rule.id == RULE_ID)
    report = run_selected_rules(ROOT, (RULE_ID,))

    assert owner.count("def effective_host_provider_identity(") == 1
    assert "return provider.kind, provider.credential_purpose" in owner
    assert consumer.count("effective_host_provider_identity(") == 2
    assert lock_seed_consumer.count("effective_host_provider_identity(") == 1
    assert OWNER in owners["host-credential-resolution"]["selectors"]
    assert CONSUMER in owners["host-credential-resolution"]["selectors"]
    assert LOCK_SEED_CONSUMER in owners["git-ref-freshness"]["selectors"]
    assert "dependency-scoped lock seeds" in owners["host-credential-resolution"]["decision"]
    assert rule.guard_ids[-1] == RULE_ID
    assert report.failures == ()
    assert report.violations == ()


@pytest.mark.parametrize(
    ("path", "old", "new", "message"),
    [
        (
            OWNER,
            "return provider.kind, provider.credential_purpose",
            "return provider.kind, provider.kind",
            "Effective host-provider identity must derive backend and credential route",
        ),
        (
            CONSUMER,
            "locked_provider = effective_host_provider_identity(",
            "locked_provider = manifest_provider_identity(",
            "Dependency drift must",
        ),
        (
            LOCK_SEED_CONSUMER,
            "effective_host_provider_identity(",
            "raw_host_provider_identity(",
            "Dependency-scoped lock-seed identity must",
        ),
    ],
)
def test_effective_host_provider_identity_guard_rejects_owner_or_consumer_bypass(
    path: str, old: str, new: str, message: str
) -> None:
    source = (ROOT / path).read_text(encoding="utf-8")
    mutated = source.replace(old, new, 1)
    assert mutated != source
    report = run_selected_rules(ROOT, (RULE_ID,), source_overrides={path: mutated})
    assert report.failures == ()
    assert any(
        violation.rule_id == RULE_ID and message in violation.message
        for violation in report.violations
    )
