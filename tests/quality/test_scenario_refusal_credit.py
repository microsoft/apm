"""A scenario refusal credits only a declared refusal (#4101, lead ruling).

``EXPECTED_REFUSALS`` (tests/lifecycle/scenarios/_kit.py) declares the T-6b
pairs whose designed outcome is a refusal. ``credit_problems`` is the gate
``credit_step`` applies: a refusal of an undeclared pair, a declared pair whose
command succeeds, a wrong exit, a missing cause and a refusal that changed
durable state are all refused. These negative controls keep the gate closed.
"""

from __future__ import annotations

import pytest

from tests.lifecycle.scenarios._kit import EXPECTED_REFUSALS, credit_problems

RATCHET_TEST_SCOPE = "repository"

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-LAW-ownership.preserve_unowned")]

_DECLARED = "pair|OP=uninstall|DEPLOYED=edited"
_UNDECLARED = "pair|OP=install|DEPLOYED=edited"


def _cause(key: str) -> str:
    return {
        _DECLARED: "[x] Uninstall refused: deleting these tracked files needs deployment evidence",
    }[key]


def test_a_refusal_of_an_undeclared_pair_fails() -> None:
    problems = credit_problems([_UNDECLARED], exit=1, output="[x] no", changed=(), refusal=True)

    assert problems == [f"{_UNDECLARED}: refusal is not a declared outcome (EXPECTED_REFUSALS)"]


def test_a_declared_refusal_whose_command_succeeds_fails() -> None:
    problems = credit_problems([_DECLARED], exit=0, output="", changed=(), refusal=False)

    assert len(problems) == 1 and "declared refusal" in problems[0], problems


def test_a_declared_refusal_with_another_exit_or_cause_fails() -> None:
    wrong_exit = credit_problems(
        [_DECLARED], exit=2, output=_cause(_DECLARED), changed=(), refusal=True
    )
    wrong_cause = credit_problems([_DECLARED], exit=1, output="[x] other", changed=(), refusal=True)

    assert wrong_exit == [f"{_DECLARED}: exited 2, declared 1"]
    assert wrong_cause == [
        f"{_DECLARED}: output does not name the cause {EXPECTED_REFUSALS[_DECLARED].cause!r}"
    ]


def test_a_refusal_that_changed_durable_state_fails() -> None:
    problems = credit_problems(
        [_DECLARED],
        exit=1,
        output=_cause(_DECLARED),
        changed=("project:apm.yml", "home:.apm/deployment-receipts/x.json"),
        refusal=True,
    )

    assert problems == ["a refusal must change nothing, changed: ['project:apm.yml']"]


def test_a_declared_refusal_that_refuses_cleanly_credits() -> None:
    assert (
        credit_problems([_DECLARED], exit=1, output=_cause(_DECLARED), changed=(), refusal=True)
        == []
    )


def test_every_declared_refusal_names_its_law_and_source() -> None:
    for key, refusal in EXPECTED_REFUSALS.items():
        # A refusal credit proves a user-state or org-policy pair (T-6b, T-7b).
        assert key.startswith("pair|") and ("DEPLOYED=" in key or "GATE=denied-dependency" in key)
        assert refusal.exit != 0, key
        assert refusal.upholds.startswith(("P-LAW-", "P-INV-")), key
        assert refusal.source, key


_POLICY = "pair|SRC=inline-mcp|GATE=denied-dependency"
_PACKAGE_BLOCK = (
    "[x] Blocked by org policy at org:acme/.github (deny). Nothing was changed. "
    "Remove `p-skill` from apm.yml, or contact your policy admin to allow it."
)
_MCP_BLOCK = "[x] MCP server(s) blocked by org policy. MCP configs were NOT written."


@pytest.mark.parametrize("output", [_PACKAGE_BLOCK, _MCP_BLOCK], ids=["package", "mcp"])
def test_a_policy_refusal_credits_only_with_its_policy_cause(output) -> None:
    assert credit_problems([_POLICY], exit=1, output=output, changed=(), refusal=True) == []


def test_a_non_policy_refusal_that_wrote_nothing_earns_no_policy_credit() -> None:
    """Any exit-1 failure can end in "Nothing was written"; only a policy cause credits."""
    unrelated = (
        "[x] Cannot create apm.yml for --target vscode: an IDE is not a target. "
        "Nothing was written."
    )
    problems = credit_problems([_POLICY], exit=1, output=unrelated, changed=(), refusal=True)

    assert problems == [
        f"{_POLICY}: output does not name the cause {EXPECTED_REFUSALS[_POLICY].cause!r}"
    ]
