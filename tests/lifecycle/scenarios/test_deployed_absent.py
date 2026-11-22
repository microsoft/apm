"""A package whose deployed files are absent, across the lifecycle factors (#4097).

PROMISE: P-INV-I1 -- after every command the lockfile matches what is
deployed: a resolving install puts back what the lock records but disk
lacks, and uninstall forgets a package whose files are already gone (source:
docs/src/content/docs/reference/cli/install.md, reference/cli/uninstall.md).
PROMISE: P-INV-I4 -- an absent deployment is never widened by a gate that
does not allow it: a denied executable stays undeployed and an org-denied
dependency is refused before anything is written.

DEPLOYED=absent (T-3): nothing of the package is on disk before the step,
either because it was never deployed or because the user removed every file
the lock records (an input edit). Each row applies I1-I9 through the scenario
kit and credits the pairs its factor assignment implies.
"""

from __future__ import annotations

import re

import pytest

from tests.lifecycle.scenarios._kit import Scenario, credits
from tests.lifecycle.scenarios.test_network_offline import go_offline
from tests.lifecycle.world import use_policy

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-INV-I1", "P-INV-I4"),
]

_ABSENT = {"DEPLOYED": "absent"}
_SKILL = ".agents/skills/p-skill/SKILL.md"
_HOOK = ".github/hooks/p-exec-session.json"
_NO_POLICY_SAYS = r"Policy enforcement disabled by --no-policy for this invocation"


def _allowing(world, *packages: str, targets=("copilot",)) -> Scenario:
    use_policy(world, "allow")
    return Scenario(world, packages, policy="allow", targets=targets)


def _remove_deployed(scenario: Scenario) -> frozenset[str]:
    """The user deletes every file the lock records as deployed (an input edit)."""
    lock = scenario.lock()
    assert lock is not None
    removed = {
        path
        for dep in lock.get_package_dependencies()
        for path in dep.deployed_files
        if (scenario.deploy_root / path).is_file()
    }
    assert removed, "nothing was deployed to remove"
    for path in removed:
        (scenario.deploy_root / path).unlink()
    scenario.source_moved()
    return frozenset(removed)


# -- the gate ----------------------------------------------------------------------


def test_a_denied_executable_stays_undeployed(lifecycle_world) -> None:
    scenario = _allowing(lifecycle_world, "p-exec")
    scenario.run("install p-exec")
    scenario.run("deny p-exec")
    credit = {**_ABSENT, "GATE": "denied-executable", "PRIM": "hook"}
    scenario.run("install", credit=[credit])
    assert not (lifecycle_world.project / _HOOK).exists()
    scenario.finish()


def test_a_revoked_hook_the_user_removed_stays_removed(lifecycle_world) -> None:
    scenario = _allowing(lifecycle_world, "p-exec")
    scenario.run("install p-exec")
    scenario.run("approve p-exec")
    scenario.run("install")
    scenario.run("deny p-exec")
    _remove_deployed(scenario)
    credit = {**_ABSENT, "GATE": "revoked-after-deploy", "PRIM": "hook"}
    scenario.run("install", credit=[credit])
    assert not (lifecycle_world.project / _HOOK).exists()
    scenario.finish()


def test_an_org_denied_dependency_is_refused_before_any_deploy(lifecycle_world) -> None:
    scenario = _allowing(lifecycle_world, "p-skill")
    use_policy(lifecycle_world, "deny-all-deps")
    scenario.model = scenario.model.with_policy("deny-all-deps", ("p-skill",))
    scenario.source_moved()
    step = scenario.run(
        "install p-skill",
        credit=[{**_ABSENT, "GATE": "denied-dependency"}],
        refusal=True,
    )
    assert "Blocked by org policy" in " ".join(step.output.split()), step.output
    assert not (lifecycle_world.project / _SKILL).exists()
    scenario.finish()


def test_no_policy_deploys_an_org_denied_dependency_and_says_so(lifecycle_world) -> None:
    scenario = _allowing(lifecycle_world, "p-skill")
    use_policy(lifecycle_world, "deny-all-deps")
    scenario.source_moved()
    scenario.run(
        "install --no-policy p-skill",
        credit=[{**_ABSENT, "NOPOLICY": "on"}],
        says=[_NO_POLICY_SAYS],
    )
    assert (lifecycle_world.project / _SKILL).is_file()
    scenario.finish()


# -- redeploy what the lock records ------------------------------------------------


def test_an_offline_install_redeploys_removed_files_from_the_cache(lifecycle_world) -> None:
    scenario = _allowing(lifecycle_world, "p-skill")
    scenario.run("install p-skill")
    removed = _remove_deployed(scenario)
    go_offline(lifecycle_world)
    scenario.run("install", credit=[{**_ABSENT, "NET": "offline"}])
    scenario.assert_deployed()
    assert all((lifecycle_world.project / path).is_file() for path in removed)
    scenario.finish()


_TARGETS = {
    "single-narrowed": (("copilot",), ("claude",)),
    "single-widened": (("copilot",), ("copilot", "claude")),
    "multi-narrowed": (("copilot", "claude"), ("copilot",)),
    "multi-widened": (("copilot", "claude"), ("copilot", "claude", "cursor")),
}


@pytest.mark.parametrize("change", sorted(_TARGETS))
def test_a_target_change_redeploys_removed_files(lifecycle_world, change) -> None:
    before, after = _TARGETS[change]
    scenario = _allowing(lifecycle_world, "p-skill", targets=before)
    scenario.run("install p-skill")
    _remove_deployed(scenario)
    scenario.set_targets(*after)
    scenario.run("install", credit=[{**_ABSENT, "TGT": change}])
    scenario.assert_deployed()
    scenario.finish()


# -- other commands ------------------------------------------------------------------


def test_uninstall_forgets_a_package_whose_files_are_gone(lifecycle_world) -> None:
    scenario = _allowing(lifecycle_world, "p-skill")
    scenario.run("install p-skill")
    _remove_deployed(scenario)
    scenario.run("uninstall p-skill", credit=[{**_ABSENT, "OP": "uninstall"}])
    assert not scenario.lock_names(), scenario.lock_names()
    lock = scenario.lock_path
    assert not lock.exists() or "p-skill" not in lock.read_text("utf-8"), lock.read_text("utf-8")
    scenario.finish()


def test_a_marketplace_add_leaves_a_parked_package_undeployed(lifecycle_world) -> None:
    scenario = _allowing(lifecycle_world, "p-exec")
    # Unapproved: p-exec is declared and locked, and none of its files is deployed.
    scenario.run("install p-exec")
    step = scenario.run(
        f"marketplace add {lifecycle_world.services.marketplace_url}",
        credit=[{**_ABSENT, "OP": "marketplace"}],
        forbidden={"project": ("*",)},
        allowed={},
    )
    assert not any(item.startswith("project:") for item in step.changed), step.changed
    assert not (lifecycle_world.project / _HOOK).exists()
    scenario.finish()


# The bundle packs p-exec, so its files carry that package name.
_BUNDLE_KINDS = {"p-exec": ("hook", "mcp", "bin")}


@pytest.mark.parametrize("scope", ("project", "user"))
def test_an_approved_local_bundle_deploys_from_nothing(lifecycle_world, scope) -> None:
    use_policy(lifecycle_world, "allow")
    specs = {"p-exec": lifecycle_world.packages["p-exec:bundle"].spec}
    scenario = Scenario(lifecycle_world, specs, policy="allow", kinds=_BUNDLE_KINDS, scope=scope)
    first = scenario.run("install p-exec")
    # Run the approval the parked install prints (Step 9: every hint is a test).
    hint = re.search(r"apm approve(?: -g)? '([^']+)'", first.output)
    assert hint, first.output
    approved = scenario.model.approve("p-exec", _BUNDLE_KINDS["p-exec"])
    scenario.run(("approve", hint.group(1)), requested=approved)
    credit = {**_ABSENT, "OP": "bundle-install", "SRC": "local-bundle", "SCOPE": scope}
    step = scenario.run("install p-exec", credit=[{**credit, "GATE": "allowed"}])
    scenario.assert_deployed()
    assert "Wired 1 MCP server(s) from bundle" in step.output, step.output
    scenario.finish()


CREDITS = credits(
    __file__,
    {
        "test_a_denied_executable_stays_undeployed": [
            {**_ABSENT, "GATE": "denied-executable", "PRIM": "hook"}
        ],
        "test_a_revoked_hook_the_user_removed_stays_removed": [
            {**_ABSENT, "GATE": "revoked-after-deploy", "PRIM": "hook"}
        ],
        "test_an_org_denied_dependency_is_refused_before_any_deploy": [
            {**_ABSENT, "GATE": "denied-dependency"}
        ],
        "test_no_policy_deploys_an_org_denied_dependency_and_says_so": [
            {**_ABSENT, "NOPOLICY": "on"}
        ],
        "test_an_offline_install_redeploys_removed_files_from_the_cache": [
            {**_ABSENT, "NET": "offline"}
        ],
        **{
            f"test_a_target_change_redeploys_removed_files[{change}]": [{**_ABSENT, "TGT": change}]
            for change in _TARGETS
        },
        "test_uninstall_forgets_a_package_whose_files_are_gone": [{**_ABSENT, "OP": "uninstall"}],
        "test_a_marketplace_add_leaves_a_parked_package_undeployed": [
            {**_ABSENT, "OP": "marketplace"}
        ],
        **{
            f"test_an_approved_local_bundle_deploys_from_nothing[{scope}]": [
                {
                    **_ABSENT,
                    "OP": "bundle-install",
                    "SRC": "local-bundle",
                    "SCOPE": scope,
                    "GATE": "allowed",
                }
            ]
            for scope in ("project", "user")
        },
    },
)
