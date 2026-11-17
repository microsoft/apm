"""Owner check for authenticated-host auth-outcome classification and the chain entry.

Ports the ``host-credential-resolution`` owner decision "authenticated host
access: escalation chain entry point and auth-outcome classification" (T1
brief sections 5.1-5.3, 5.6 items 3, 4, 6):

* ``core/auth_outcome.py::classify_auth_outcome`` is the only classifier of
  "did this attempt fail for an auth reason".
* ``AuthResolver.try_with_fallback`` is the only chain entry; it delegates the
  Azure DevOps chain to ``core/auth_chain.py::run_ado_chain``. The legacy
  ``execute_with_bearer_fallback`` side door must not come back.
* Every Azure DevOps egress routes through ``try_with_fallback`` and presents
  only the credential the chain selected (no ambient netrc). Call sites never
  classify status codes, HTML sign-in pages, or Git stderr themselves.
* The helper step stays inside the chain: no other module runs
  ``git credential fill`` for a chain credential.

A second rule, ``transport-platform-credential-report-owner``, keeps
``git credential approve``/``reject`` (the git-credential contract's step 4)
inside the chain owner: ``auth_chain.report_helper_outcome`` is the only
caller of the runner in ``token_manager.py``, and it rejects only on a
classifier-verified rejection.
"""

from __future__ import annotations

import re

from scripts.architecture_linter.checks.transport_platform_shared import (
    GROUP,
    _count_checks,
    _forbid_scan,
    _load,
    _require_subs,
    _src_python,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import source_text, violation
from scripts.architecture_linter.models import Rule, Violation

_RULE_ID = "transport-platform-auth-outcome-owner"
_REPORT_RULE_ID = "transport-platform-credential-report-owner"

_AUTH = "src/apm_cli/core/auth.py"
_OUTCOME = "src/apm_cli/core/auth_outcome.py"
_CHAIN = "src/apm_cli/core/auth_chain.py"
_CREDENTIAL_FILL_OWNER = "src/apm_cli/core/token_manager.py"
_GITHUB_HOST = "src/apm_cli/utils/github_host.py"
_DOWNLOAD = "src/apm_cli/deps/download_strategies.py"

_LEGACY_SIGNALS = re.compile(
    r"\bexecute_with_bearer_fallback\(|\bis_ado_auth_failure_signal\("
    r"|\bis_public_github_auth_failure\("
)
_HELPER_FILL = re.compile(r"\bresolve_git_helper_credential\(")
# Argv lists, shell strings, and f-strings that would mutate the user's store.
_CREDENTIAL_STORE_MUTATION = re.compile(
    r"""["']credential["']\s*,\s*["'](?:approve|reject)["']"""
    r"|\bcredential\s+(?:approve|reject)\b"
)
_REPORT_ACTION_LITERAL = r"""["'](?:approve|reject)["']"""
_REPORT_RUNNER_CALL = re.compile(r"\breport_git_helper_credential\(")
_REPORT_OWNER_CALL = re.compile(r"\breport_helper_outcome\(")
# Hand-rolled auth classification that the routed call sites must not regrow.
_LOCAL_CLASSIFICATION = re.compile(
    r"status_code\s*(?:==|in)\s*[\(\{\[]?\s*40[13]\b"
    r"|[\"']text/html[\"']"
    r"|raise\s+\w+\([^)]*unauthorized"
)

# Azure DevOps egress consumers and the chain routing each must keep.
_ROUTED_CONSUMERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("src/apm_cli/install/validation.py", ("auth_resolver.try_with_fallback(", "remote_url=")),
    (
        "src/apm_cli/install/pipeline.py",
        ("auth_resolver.try_with_fallback(", "remote_url=probe_url"),
    ),
    (
        "src/apm_cli/deps/github_downloader_validation.py",
        ("downloader.auth_resolver.try_with_fallback(", "remote_url=primary_attempt.url"),
    ),
    (
        "src/apm_cli/deps/clone_engine.py",
        ("host.auth_resolver.try_with_fallback(", "remote_url=attempt.effective_url or ado_url"),
    ),
    (
        "src/apm_cli/deps/git_reference_resolver.py",
        ("host.auth_resolver.try_with_fallback(", "remote_url=transport_attempt.effective_url"),
    ),
    (
        "src/apm_cli/marketplace/ref_resolver.py",
        ("self._auth_resolver.try_with_fallback(", "remote_url=url"),
    ),
    (
        "src/apm_cli/marketplace/client.py",
        (
            "**authorization_headers(git_env)",
            "ambient_credentials=False",
            "follow_cross_origin_without_credentials=False",
            "raise_for_auth_outcome(",
        ),
    ),
    (
        "src/apm_cli/policy/discovery.py",
        (
            "headers=authorization_headers(git_env)",
            "_policy_auth_resolver().try_with_fallback(",
            "org=owner,",
        ),
    ),
)
_CLASSIFICATION_FREE = (
    *(path for path, _needles in _ROUTED_CONSUMERS),
    "src/apm_cli/policy/_gitlab.py",
)


def _download_ado_file_body(provider: FactsProvider, inventory: frozenset[str]):
    facts, failures = _load(provider, inventory, _RULE_ID, _DOWNLOAD)
    if failures:
        return None, failures
    text = source_text(facts)
    start = text.find("    def download_ado_file(")
    if start < 0:
        return None, (
            violation(_RULE_ID, _DOWNLOAD, "download_ado_file must exist to route ADO files"),
        )
    end = text.find("\n    def ", start + 1)
    return text[start : end if end > 0 else len(text)], ()


def _check_download_ado_file(
    provider: FactsProvider, inventory: frozenset[str]
) -> tuple[Violation, ...]:
    body, failures = _download_ado_file_body(provider, inventory)
    if failures:
        return failures
    problems = [
        needle
        for needle in ("resolver.try_with_fallback(", "allow_netrc=False")
        if needle not in body
    ]
    findings = (
        [
            violation(
                _RULE_ID,
                _DOWNLOAD,
                "download_ado_file must route through try_with_fallback without ambient "
                f"netrc; missing: {', '.join(repr(item) for item in problems)}",
            )
        ]
        if problems
        else []
    )
    if _LOCAL_CLASSIFICATION.search(body) or "execute_with_bearer_fallback" in body:
        findings.append(
            violation(
                _RULE_ID,
                _DOWNLOAD,
                "download_ado_file must leave auth classification to classify_auth_outcome",
            )
        )
    return tuple(findings)


def _check_auth_outcome_owner(provider: FactsProvider) -> tuple[Violation, ...]:
    inventory = frozenset(provider.inventory)
    findings: list[Violation] = []
    findings.extend(
        _count_checks(
            provider,
            inventory,
            _RULE_ID,
            _OUTCOME,
            (("sub", "def classify_auth_outcome(", 1, "eq"),),
            "Auth-outcome classification must have one canonical owner",
        )
    )
    findings.extend(
        _count_checks(
            provider,
            inventory,
            _RULE_ID,
            _CHAIN,
            (
                ("sub", "def run_ado_chain(", 1, "eq"),
                ("sub", "classify_auth_outcome(", 1, "ge"),
            ),
            "The ADO chain must classify every attempt through the canonical classifier",
        )
    )
    findings.extend(
        _count_checks(
            provider,
            inventory,
            _RULE_ID,
            _AUTH,
            (
                ("sub", "return run_ado_chain(", 1, "eq"),
                ("sub", "def execute_with_bearer_fallback(", 0, "eq"),
            ),
            "AuthResolver.try_with_fallback must be the only ADO chain entry",
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inventory,
            _RULE_ID,
            _CREDENTIAL_FILL_OWNER,
            ('env["GCM_INTERACTIVE"] = "0"', "env=credential_fill_env("),
            "git credential fill must never open an interactive sign-in when unattended",
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inventory,
            _RULE_ID,
            _src_python(provider, exclude={_AUTH, _OUTCOME, _CHAIN, _GITHUB_HOST}),
            _LEGACY_SIGNALS,
            "Call sites must not classify auth outcomes or reopen the bearer side door; "
            "route through AuthResolver.try_with_fallback",
            exempt=False,
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inventory,
            _RULE_ID,
            _src_python(provider, exclude={_AUTH, _CHAIN, _CREDENTIAL_FILL_OWNER}),
            _HELPER_FILL,
            "Chain helper credentials are obtained only by the AuthResolver chain",
            exempt=False,
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inventory,
            _RULE_ID,
            _CLASSIFICATION_FREE,
            _LOCAL_CLASSIFICATION,
            "Routed call sites must report raw outcomes; classify_auth_outcome decides",
            exempt=False,
        )
    )
    for path, needles in _ROUTED_CONSUMERS:
        findings.extend(
            _require_subs(
                provider,
                inventory,
                _RULE_ID,
                path,
                needles,
                "Azure DevOps egress must route through AuthResolver.try_with_fallback",
            )
        )
    findings.extend(_check_download_ado_file(provider, inventory))
    return tuple(findings)


def _check_credential_report_owner(provider: FactsProvider) -> tuple[Violation, ...]:
    inventory = frozenset(provider.inventory)
    findings: list[Violation] = []
    findings.extend(
        _count_checks(
            provider,
            inventory,
            _REPORT_RULE_ID,
            _CREDENTIAL_FILL_OWNER,
            (
                ("sub", "def report_git_helper_credential(", 1, "eq"),
                ("re", _REPORT_ACTION_LITERAL, 1, "eq"),
            ),
            "git credential approve/reject must have one runner next to fill",
        )
    )
    findings.extend(
        _count_checks(
            provider,
            inventory,
            _REPORT_RULE_ID,
            _CHAIN,
            (
                ("sub", "def report_helper_outcome(", 1, "eq"),
                ("re", _REPORT_OWNER_CALL.pattern, 2, "eq"),
                ("re", _REPORT_RUNNER_CALL.pattern, 1, "eq"),
                ("sub", "    elif outcome.is_verified_rejection:", 1, "eq"),
                ("sub", "    if not _HELPER_REPORTS.claim(key, action):", 1, "eq"),
                (
                    "sub",
                    "report_helper_outcome(resolver, host_info, helper_path, credential,",
                    1,
                    "eq",
                ),
            ),
            "Only the chain owner reports a helper credential, once per key and action, "
            "and rejects only on a verified rejection",
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inventory,
            _REPORT_RULE_ID,
            _OUTCOME,
            ("rejection_verified=credential_presented and status == 401",),
            "Only an HTTP 401 for a presented credential verifies a rejection",
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inventory,
            _REPORT_RULE_ID,
            _src_python(provider, exclude={_CREDENTIAL_FILL_OWNER}),
            _CREDENTIAL_STORE_MUTATION,
            "git credential approve/reject runs only in GitHubTokenManager."
            "report_git_helper_credential",
            exempt=False,
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inventory,
            _REPORT_RULE_ID,
            _src_python(provider, exclude={_CREDENTIAL_FILL_OWNER, _CHAIN}),
            _REPORT_RUNNER_CALL,
            "Only auth_chain.report_helper_outcome reports helper credentials",
            exempt=False,
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inventory,
            _REPORT_RULE_ID,
            _src_python(provider, exclude={_CHAIN}),
            _REPORT_OWNER_CALL,
            "Only run_ado_chain reports a helper credential outcome",
            exempt=False,
        )
    )
    return tuple(findings)


RULES: tuple[Rule, ...] = (
    Rule(
        id=_REPORT_RULE_ID,
        group=GROUP,
        guard_ids=(_REPORT_RULE_ID,),
        description=(
            "git credential approve/reject is reported only by the ADO chain owner for "
            "helper credentials, and reject only after a verified HTTP 401."
        ),
        check=_check_credential_report_owner,
    ),
    Rule(
        id=_RULE_ID,
        group=GROUP,
        guard_ids=(_RULE_ID,),
        description=(
            "Auth outcomes are classified only by core/auth_outcome.py and every "
            "authenticated ADO egress routes through AuthResolver.try_with_fallback."
        ),
        check=_check_auth_outcome_owner,
    ),
)

COLLECTORS: tuple[object, ...] = ()

__all__ = ["COLLECTORS", "RULES"]
