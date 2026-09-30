"""In-memory source mutations prove the GitLab sparse ownership boundaries."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from scripts.architecture_linter.runner import registered_rules, run_selected_rules

pytestmark = pytest.mark.component

ROOT = Path(__file__).resolve().parents[2]
RULE_ID = "transport-platform-gitlab-sparse-plan"
AUTH_RULE_ID = "transport-platform-git-child-environment"
CONSUMER = "src/apm_cli/deps/download_strategies.py"


def test_gitlab_sparse_plan_is_registered_and_compliant() -> None:
    """The owner registry and executable rule agree on the integrated consumer."""
    registry = json.loads(
        (ROOT / ".apm/architecture/owners/transport-auth-platform.json").read_text(encoding="utf-8")
    )
    owner = next(item for item in registry["owners"] if item["id"] == "git-transport-selection")
    assert owner["guards"] == [RULE_ID]
    assert set(owner["selectors"]) == {
        CONSUMER,
        "src/apm_cli/deps/transport_selection.py",
    }
    assert [rule.id for rule in registered_rules() if rule.id == RULE_ID] == [RULE_ID]
    report = run_selected_rules(ROOT, (RULE_ID, AUTH_RULE_ID))
    assert not report.failures
    assert not report.violations


@pytest.mark.parametrize(
    ("old", "new", "edge"),
    [
        (
            "initial_transport_scheme(dep_ref, self._host._protocol_pref)",
            "'https'",
            "initial-scheme helper",
        ),
        (
            "        plan = self._host._transport_selector.select(",
            "        plan = bypass_selector(",
            "selector plan execution",
        ),
        (
            "anonymous_plan = self._host._transport_selector.select(",
            "anonymous_plan = bypass_selector(",
            "selector plan execution",
        ),
        (
            "for attempt in plan.attempts:",
            "for attempt in bypass_attempts:",
            "selector plan execution",
        ),
        (
            "resolver = self._host.auth_resolver",
            "resolver = bypass_auth",
            "AuthResolver routing",
        ),
        (
            "attempt_ctx = resolver.resolve_for_remote(",
            "attempt_ctx = bypass_auth(",
            "AuthResolver routing",
        ),
        (
            "resolver.git_env_for_remote(attempt_ctx, effective_url)",
            "bypass_env(attempt_ctx, effective_url)",
            "AuthResolver routing",
        ),
        (
            "resolver.build_native_git_credential_env(host_info, effective_url)",
            "bypass_native_env(host_info, effective_url)",
            "AuthResolver routing",
        ),
        (
            "requested_url=requested_url,",
            "requested_url=candidate_url,",
            "prepared attempt arguments",
        ),
        (
            "return requested_url",
            "return self.build_repo_url(repo_ref, dep_ref=dep_ref)",
            "prepared URL callback",
        ),
        (
            "build_repo_url_fn=_prepared_repo_url,",
            "build_repo_url_fn=self.build_repo_url,",
            "prepared URL callback",
        ),
        (
            "if rest_eligible:",
            "if True:",
            "executed HTTPS REST gate",
        ),
        (
            "rest_eligible = False",
            "rest_eligible = True",
            "executed HTTPS REST gate",
        ),
        (
            "rest_eligible = rest_eligible or self._gitlab_rest_eligible(",
            "rest_eligible = rest_eligible or bypass_gate(",
            "executed HTTPS REST gate",
        ),
        (
            "effective_url, host_info.api_base",
            "candidate_url, host_info.api_base",
            "executed HTTPS REST gate",
        ),
    ],
    ids=[
        "initial-scheme",
        "selector",
        "initial-selector",
        "executed-plan",
        "auth-owner",
        "auth-resolution",
        "managed-env",
        "native-env",
        "prepared-arguments",
        "rebuilt-url",
        "callback",
        "unconditional-rest",
        "preauthorized-rest",
        "rest-predicate",
        "requested-not-effective-origin",
    ],
)
def test_gitlab_sparse_guard_rejects_executable_edge_bypass(old: str, new: str, edge: str) -> None:
    """Each syntactically valid bypass fails even with owner names in comments."""
    source = (ROOT / CONSUMER).read_text(encoding="utf-8")
    start = source.index("    def _download_gitlab_file_via_git(")
    end = source.index("    def _download_gitlab_file_via_rest(")
    bounded = source[start:end]
    assert old in bounded, f"Mutation seam changed: {old}"
    mutated = (
        source[:start]
        + bounded.replace(old, new, 1)
        + source[end:]
        + f"\n# Retained ownership evidence: {old}\n"
    )
    ast.parse(mutated)
    rule_ids = (RULE_ID, AUTH_RULE_ID) if edge == "prepared URL callback" else (RULE_ID,)
    report = run_selected_rules(ROOT, rule_ids, source_overrides={CONSUMER: mutated})
    assert not report.failures
    assert any(
        violation.rule_id == RULE_ID and edge in violation.message
        for violation in report.violations
    ), report.violations
    if AUTH_RULE_ID in rule_ids:
        assert any(violation.rule_id == AUTH_RULE_ID for violation in report.violations)


@pytest.mark.parametrize("builder", ["candidate", "attempt"])
def test_both_guards_reject_credentials_in_prepared_url(builder: str) -> None:
    """Both guard families retain executable proof that every builder is tokenless."""
    source = (ROOT / CONSUMER).read_text(encoding="utf-8")
    start = source.index("    def download_gitlab_file(")
    end = source.index("    def _download_gitlab_file_via_rest(")
    bounded = source[start:end]
    old = 'token="",'
    assert bounded.count(old) == 2
    before, after = bounded.split(old, 1) if builder == "candidate" else bounded.rsplit(old, 1)
    mutated = source[:start] + before + "token=managed_token," + after + source[end:]
    ast.parse(mutated)
    report = run_selected_rules(ROOT, (RULE_ID, AUTH_RULE_ID), source_overrides={CONSUMER: mutated})
    assert not report.failures
    assert {violation.rule_id for violation in report.violations} == {RULE_ID, AUTH_RULE_ID}
