"""Mutation coverage for the Unix installation ownership boundary."""

from pathlib import Path

import pytest

from scripts.architecture_linter.runner import registered_rules, run_selected_rules

pytestmark = pytest.mark.component
ROOT = Path(__file__).resolve().parents[2]
RULE_ID = "transport-platform-unix-install-ownership"


def test_unix_install_owner_registered_and_clean() -> None:
    """The owner registry and the CI entrypoint must include the same rule."""
    rule = next(rule for rule in registered_rules() if rule.id == RULE_ID)
    report = run_selected_rules(ROOT, (RULE_ID,))
    assert rule.guard_ids == (RULE_ID,)
    assert not report.violations
    assert not report.failures


@pytest.mark.parametrize(
    ("path", "mutation"),
    [
        ("install.sh", '\nsudo mkdir -p "$APM_LIB_DIR"\n'),
        (
            "src/apm_cli/commands/self_update.py",
            "\ndef apm_resolve_install_paths():\n    return '/usr/local/bin'\n",
        ),
        (
            "src/apm_cli/commands/self_update.py",
            "\nclass CompetingOwner:\n    def apm_resolve_install_paths(self):\n        pass\n",
        ),
        (
            "scripts/lint-auth-signals.sh",
            "\nif true; then\n    apm_probe_installation() { :; }\nfi\n",
        ),
        (
            "src/apm_cli/commands/self_update.py",
            "\nasync def apm_require_owned_bundle():\n    pass\n",
        ),
        (
            "scripts/lint-auth-signals.sh",
            "\nfunction apm_require_owned_bundle () { :; }\n",
        ),
        pytest.param(
            "scripts/lint-auth-signals.sh",
            "\nfunction apm_require_owned_bundle { :; }\n",
            id="bash-function-without-parentheses",
        ),
    ],
)
def test_unix_install_boundary_rejects_elevation_or_second_owner(path: str, mutation: str) -> None:
    """Reintroducing escalation or a competing owner must fail the static guard."""
    source = (ROOT / path).read_text(encoding="utf-8")
    report = run_selected_rules(ROOT, (RULE_ID,), source_overrides={path: source + mutation})
    assert any(violation.rule_id == RULE_ID for violation in report.violations)


def test_unix_install_boundary_requires_self_update_identity() -> None:
    """Self-update cannot silently stop identifying the installation being updated."""
    path = "src/apm_cli/commands/self_update.py"
    source = (ROOT / path).read_text(encoding="utf-8")
    mutated = source.replace(
        'env["APM_SELF_UPDATE_SOURCE"] = os.path.abspath(', "ignored = os.path.abspath("
    )
    report = run_selected_rules(ROOT, (RULE_ID,), source_overrides={path: mutated})
    assert any(violation.rule_id == RULE_ID for violation in report.violations)
