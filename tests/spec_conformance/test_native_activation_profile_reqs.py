"""Native plugin activation profile conformance -- sec.8.5.11 (amendment 0.1.65).

The profile is specified before any APM route implements it. Each
requirement test pins its normative clause in the spec text and records
a waiver naming the delivery that will bind it to real behavior: the
Claude user-scope route (#3290), project scope with Copilot (#3921) and
the Codex split fallback (#3920). Those deliveries replace the waivers.

The req-na-004 oracle is checked against a small reference model of the
reconciliation rule so the fixture stays self-consistent before a
consumer runs against it.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from tests.spec_conformance._helpers import assert_spec_contains, load_json_fixture, waive

_ORACLE = ("native-activation", "req-na-004.json")


@pytest.mark.req("req-na-001")
def test_native_activation_requires_documented_part_a_qualification():
    assert_spec_contains(
        "A target **qualifies** for a scope (project or user) and a client version",
        "A target whose qualification\nis unknown does not qualify.",
        "the oldest client\nversion it qualified, and the published source it relies on for each\ncriterion.",
    )
    waive(
        "No APM route natively activates under sec.8.5.11 yet; the conformance "
        "statement disclosure lands with the Claude user-scope route (#3290)."
    )


@pytest.mark.req("req-na-002")
def test_lockfile_drives_deployment_of_the_approved_plugin_root():
    assert_spec_contains(
        "MUST derive the set of\nactivated plugins at a scope only from the lockfile",
        "one root digest covering the path, bytes, and\nexecutable bit of every file in that root",
        "`<profile root>/.claude-plugin/marketplace.json`",
        "MUST\nNOT declare the resolved dependency root to a target as a marketplace",
    )
    waive(
        "Deployment to .agents/plugins is implemented by the Claude user-scope "
        "route (#3290) and the project-scope route for Claude and Copilot (#3921)."
    )


@pytest.mark.req("req-na-003")
def test_declaration_uses_only_documented_activation_entries():
    assert_spec_contains(
        "MUST NOT\nwrite any other settings key for this purpose",
        "MUST NOT locate, invoke, or version-check a target-native\nbinary, CLI, or RPC endpoint",
    )
    waive(
        "Settings-only declaration is implemented by the Claude user-scope route "
        "(#3290) and the shared settings writer for Copilot (#3921)."
    )


@pytest.mark.req("req-na-004")
def test_ownership_derives_from_previous_and_next_lockfile():
    assert_spec_contains(
        "MUST remove\nonly what the previous lockfile produced with the same binding",
        "MUST NOT change an\nexisting `false` enable value",
        "The\nconsumer MUST NOT create a receipt, ledger, or other ownership file",
    )
    load_json_fixture(*_ORACLE)
    waive(
        "Lock-derived settings ownership is implemented by the Claude user-scope "
        "route (#3290), which runs against the native-activation/req-na-004.json oracle."
    )


@pytest.mark.req("req-na-005")
def test_enable_requires_content_bound_approval():
    assert_spec_contains(
        "identified by the root\ndigest of [req-na-002](#req-na-002) rather than by package name",
        "MUST NOT declare it in a disabled state",
    )
    waive(
        "Content-bound enablement is implemented by the Claude user-scope route "
        "(#3290); this profile does not adopt proposed amendment 0.1.64."
    )


@pytest.mark.req("req-na-006")
def test_unqualified_target_gets_explicit_split_install():
    assert_spec_contains(
        "It MUST NOT split-install a plugin silently",
        "names the target, the scope, and the criterion that is\nunmet or unknown",
        "Split (fails A3",
    )
    waive(
        "The explicit split fallback for Codex, which fails A3, is implemented in "
        "#3920; schema-bearing Agent Plugins v1 dependencies stay opaque under req-tg-011."
    )


@pytest.mark.req("req-na-007")
def test_generated_marketplace_name_is_unique_per_project_and_scope():
    assert_spec_contains(
        "stable across clones of the same project at project scope",
        "different between project scope and user scope",
    )
    waive(
        "Per-scope marketplace naming is implemented by the Claude user-scope "
        "route (#3290) and the project-scope route (#3921)."
    )


def _reconcile(case: dict[str, Any]) -> tuple[Any, list[str]]:
    """Reference model of the req-na-004 rule, used only to check the oracle."""
    if "settings_before_raw" in case:
        return case["settings_before_raw"], ["settings-unparseable"]
    name = case["marketplace"]["name"]
    binding = {"source": {"source": "directory", "path": case["marketplace"]["path"]}}
    before = case["settings_before"]
    moved_from = case.get("previous_marketplace")
    if moved_from is not None and moved_from["path"] != case["marketplace"]["path"]:
        return copy.deepcopy(before), ["binding-moved"]
    settings = copy.deepcopy(before)
    marketplaces = settings.setdefault("extraKnownMarketplaces", {})
    enabled = settings.setdefault("enabledPlugins", {})
    existing = marketplaces.get(name)
    if existing is not None and existing != binding:
        return copy.deepcopy(before), ["binding-conflict"]
    diagnostics: list[str] = []
    previous = case["previous_lock_plugins"]
    if previous is None and existing is not None:
        diagnostics.append("previous-lock-missing")
    produced = set(previous or ())
    wanted = set(case["next_lock_plugins"])
    for plugin in sorted(produced - wanted):
        if enabled.get(f"{plugin}@{name}") is True:
            del enabled[f"{plugin}@{name}"]
    for plugin in sorted(wanted):
        enabled.setdefault(f"{plugin}@{name}", True)
    if wanted:
        marketplaces[name] = binding
    elif produced and existing is not None:
        del marketplaces[name]
    return settings, diagnostics


def test_req_na_004_oracle_matches_the_reconciliation_rule():
    oracle = load_json_fixture(*_ORACLE)
    assert oracle["spec_anchor"] == "req-na-004"
    expected = {c["id"]: c for c in oracle["expected"]["cases"]}
    inputs = oracle["input"]["cases"]
    assert [c["id"] for c in inputs] == list(expected)
    for case in inputs:
        settings, diagnostics = _reconcile(case)
        want = expected[case["id"]]
        assert settings == want.get("settings_after", want.get("settings_after_raw")), case["id"]
        assert diagnostics == expected[case["id"]]["diagnostics"], case["id"]
