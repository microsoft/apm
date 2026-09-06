"""Static dual guard for the release-metadata discovery authority."""

from pathlib import Path

import pytest

from scripts.architecture_linter.runner import run_selected_rules

pytestmark = pytest.mark.component
ROOT = Path(__file__).resolve().parents[2]
RULE = "transport-platform-release-metadata-discovery"
LOOKUP = "src/apm_cli/utils/version_checker.py"
COMMAND = "src/apm_cli/commands/self_update.py"


def test_release_metadata_owner_rule_passes() -> None:
    """The production command and lookup route through registered owners."""
    report = run_selected_rules(ROOT, (RULE,))
    assert report.failures == ()
    assert report.violations == ()


@pytest.mark.parametrize(
    "path,old,new",
    [
        (LOOKUP, "and effective_repo == _DEFAULT_REPO", "and True"),
        (LOOKUP, "and github_url == _PUBLIC_GITHUB_URL", "and True"),
        (LOOKUP, "and release_metadata_url is None", "and True"),
        (LOOKUP, "and not no_direct_fallback_enabled()", "and True"),
        (COMMAND, "return get_latest_version_from_github(", "return parallel_lookup("),
    ],
)
def test_release_metadata_guard_rejects_routing_or_boundary_removal(
    path: str, old: str, new: str
) -> None:
    """A missing route or recovery boundary must trip the static half."""
    source = (ROOT / path).read_text(encoding="utf-8")
    assert old in source
    report = run_selected_rules(ROOT, (RULE,), source_overrides={path: source.replace(old, new)})
    assert report.failures == ()
    assert {violation.rule_id for violation in report.violations} == {RULE}


@pytest.mark.parametrize(
    "addition",
    [
        "\ndef get_latest_version_from_github():\n    return None\n",
        "\ndef parallel_lookup():\n    return requests.get('https://api.github.com/repos/microsoft/apm/releases/latest')\n",
    ],
)
def test_release_metadata_guard_rejects_second_owner(addition: str) -> None:
    """Reintroducing command-local metadata retrieval is forbidden."""
    source = (ROOT / COMMAND).read_text(encoding="utf-8") + addition
    report = run_selected_rules(ROOT, (RULE,), source_overrides={COMMAND: source})
    assert report.failures == ()
    assert {violation.rule_id for violation in report.violations} == {RULE}
