"""Keep package HTTPS trust decisions behind their existing owner."""

from pathlib import Path

import pytest

from scripts.architecture_linter.runner import run_selected_rules

pytestmark = pytest.mark.component
ROOT = Path(__file__).resolve().parents[2]
RULE = "transport-platform-tls-trust-injection"


def test_tls_trust_owner_routes_current_consumers() -> None:
    report = run_selected_rules(ROOT, (RULE,))
    assert report.failures == ()
    assert report.violations == ()


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        (
            "src/apm_cli/core/script_executors.py",
            "explicit_ca = explicit_ca_bundle_path()",
            'explicit_ca = os.environ.get("REQUESTS_CA_BUNDLE", "")',
        ),
        (
            "src/apm_cli/cli.py",
            "    configure_process_tls_trust()",
            "    truststore.inject_into_ssl()",
        ),
        (
            "src/apm_cli/core/_child_tls/_apm_tls_bootstrap.py",
            '    for var in ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):',
            '    for var in ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "APM_EXTRA_CA_BUNDLE"):',
        ),
    ],
    ids=["replacement-bypass", "cli-injection", "child-additive-policy"],
)
def test_tls_boundary_rejects_split_authority(path: str, old: str, new: str) -> None:
    source = (ROOT / path).read_text(encoding="utf-8")
    assert old in source
    report = run_selected_rules(ROOT, (RULE,), source_overrides={path: source.replace(old, new, 1)})
    assert report.failures == ()
    assert {finding.rule_id for finding in report.violations} == {RULE}
