"""Install engine parity: every doubt declines to the full pipeline with identical state.

PROMISE: P-INV-I4 -- a stricter policy, an edited or deleted deployed file, a changed
manifest, a foreign lockfile, a store miss, hidden Unicode, lifecycle scripts, a
non-default flag or an unpinned source is honored: the fast engine declines and the
full pipeline decides. Oracle and cell contract in _engine_parity.py.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.lifecycle.scenarios._deploy_diff import revoke_user_approval
from tests.lifecycle.scenarios._engine_parity import (
    BOTH,
    DIFF,
    DOUBTS,
    FRESH_DOUBTS,
    FULL,
    NEEDS_ANY,
    NEEDS_FRESH,
    SOURCES,
    Twin,
    _assert_decision,
    _converge,
    _converge_exec_user_approved,
    _declare,
    _fresh_checkout,
    _lifecycle_scripts,
    _marketplace_source,
    _publish_skill,
    _remove_deployed,
    make_twin,
)
from tests.lifecycle.world import World
from tests.lifecycle.world.services import ORG

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.lifecycle_smoke,
    pytest.mark.promise("P-INV-I1", "P-INV-I4"),
    pytest.mark.skipif(sys.platform == "win32", reason="fast engines are POSIX-only"),
]


@pytest.fixture
def twin(lifecycle_template: World, tmp_path: Path) -> Twin:
    return make_twin(lifecycle_template, tmp_path)


@pytest.mark.parametrize(
    "trigger", [pytest.param(t, marks=NEEDS_ANY.marks) for t in sorted(DOUBTS)]
)
def test_doubt_trigger_declines_to_the_full_pipeline(twin: Twin, trigger: str) -> None:
    change, argv, code = DOUBTS[trigger]
    _converge(twin, *BOTH)
    twin.each(change)
    decision = twin.run(*argv)
    assert twin.codes[-1] == code, (
        f"{trigger}: exit {twin.codes[-1]}, expected {code}{twin.evidence}"
    )
    _assert_decision(decision, FULL)


@NEEDS_ANY
def test_external_scanners_flag_declines_to_the_full_pipeline(twin: Twin) -> None:
    _converge(twin, *BOTH)
    twin.ok("experimental", "enable", "external_scanners")
    _assert_decision(twin.ok("install"), FULL)


@pytest.mark.parametrize(
    "char",
    [
        pytest.param("\u200b", id="zero-width-space", marks=NEEDS_ANY.marks),
        pytest.param("\ufeff", id="mid-file-bom", marks=NEEDS_ANY.marks),
        pytest.param("\u202e", id="bidi-override", marks=NEEDS_ANY.marks),
    ],
)
def test_hidden_unicode_in_a_deployed_file_takes_the_full_scan(twin: Twin, char: str) -> None:
    """The ASCII guard: a non-ASCII hidden character means a full scan, same findings."""
    _publish_skill(twin, f"Use p{char}skill. Hidden character mid-file.", tag="v1.0.0")
    twin.each(lambda w: _declare(w, "p-skill:git"))
    twin.run("install")  # a critical finding may block: parity holds either way
    _assert_decision(twin.run("install"), FULL)


@pytest.mark.parametrize(
    "source", [pytest.param(s, marks=NEEDS_ANY.marks) for s in sorted(SOURCES)]
)
def test_source_without_an_immutable_pin_declines_to_the_full_pipeline(
    twin: Twin, source: str
) -> None:
    SOURCES[source](twin)
    _assert_decision(twin.ok("install"), FULL)


@NEEDS_FRESH
def test_fresh_install_on_a_warm_cache_applies_the_diff(twin: Twin) -> None:
    """Exit, output, project bytes AND the written lockfile bytes equal the full pipeline."""
    _converge(twin, "p-skill:git")
    twin.each(_fresh_checkout)
    _assert_decision(twin.ok("install"), DIFF)


@pytest.mark.parametrize(
    "doubt", [pytest.param(d, marks=NEEDS_ANY.marks) for d in sorted(FRESH_DOUBTS)]
)
def test_fresh_install_doubt_declines_to_the_full_pipeline(twin: Twin, doubt: str) -> None:
    keys, change = FRESH_DOUBTS[doubt]
    _converge(twin, *keys)
    twin.each(change)
    _assert_decision(twin.ok("install"), FULL)


@NEEDS_ANY
def test_fresh_marketplace_install_declines_to_the_full_pipeline(twin: Twin) -> None:
    _marketplace_source(twin)
    twin.each(_fresh_checkout)
    _assert_decision(twin.ok("install"), FULL)


@pytest.mark.parametrize(
    "tier", [pytest.param(t, marks=NEEDS_ANY.marks) for t in ("project", "user")]
)
def test_lifecycle_scripts_decline_to_the_full_pipeline(twin: Twin, tier: str) -> None:
    """Scripts fire around the full pipeline only, so their presence must decline."""
    twin.each(lambda w: _lifecycle_scripts(w, tier))
    _converge(twin, "p-skill:git")
    decision = twin.ok("install")
    _assert_decision(decision, FULL)
    assert "lifecycle scripts" in decision, decision


@NEEDS_ANY
def test_escaped_hidden_unicode_in_a_deployed_hook_declines(twin: Twin) -> None:
    """A JSON hook carrying a literal backslash-u202e escape decodes to a bidi override."""
    hook = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo p-exec"}]}]}}
    hook["hooks"]["SessionStart"][0]["hooks"][0]["command"] = "echo p-exec\u202e"
    text = json.dumps(hook, indent=2, ensure_ascii=True) + "\n"
    assert "\\u202e" in text and text.isascii()
    twin.fast.services.publish_revision(
        f"{ORG}/p-exec", {".apm/hooks/session.json": text}, tag="v1.0.0"
    )
    twin.each(lambda w: _declare(w, *BOTH))
    twin.run("install")  # a scanner finding may block: parity holds either way
    twin.ok("approve", f"{ORG}/p-exec")  # the grant itself must succeed
    twin.run("install")
    decision = twin.run("install")
    _assert_decision(decision, FULL)
    # Either guard may decline first: the content scan, or the deploy store refusing a
    # record whose replayed body is not printable ASCII. Both decline before any write.
    reasons = ("non-ASCII deployed content", "no matching package record")
    assert any(reason in decision for reason in reasons), decision


@NEEDS_ANY
def test_revoked_approval_with_apm_modules_absent_declines(twin: Twin) -> None:
    """Fresh-checkout shape (lock kept, apm_modules and deployed tree gone) + a revoked grant."""
    _converge_exec_user_approved(twin)
    twin.each(_remove_deployed)
    twin.each(revoke_user_approval)
    _assert_decision(twin.ok("install"), FULL)
