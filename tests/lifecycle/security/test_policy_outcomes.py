"""Policy discovery outcomes: the owner table and one routing E2E per decision site (#4108).

Owner: ``trust-policy-outcome`` in ``.apm/architecture/owners/install-deployment.json``
(``src/apm_cli/policy/outcome_routing.py``). ``route_discovery_outcome`` turns
a discovery outcome into an announcement and a fail-closed decision that
honors the project's ``policy.fetch_failure_default``; ``resolve_trust_policy``
routes the trust readers through it.

Part 1 is the owner table at the owner symbol, fed real ``PolicyFetchResult``
and ``ApmPolicy`` inputs and a real project ``apm.yml`` (the owner is never
patched): every outcome x enforcement (off / warn / block) x
``fetch_failure_default`` (warn / block) x the cached policy's own
``fetch_failure``.

Part 2 drives the CLI through ``driver.run`` (via ``RowReplay`` steps, so
``check_cross_cutting`` checks I1-I9 after every step) in the lifecycle world,
once per call site of the owner:

* ``install/phases/policy_gate.py``      -- ``apm install <pkg>``
* ``policy/install_preflight.py``        -- ``apm install --mcp <server>``
* ``core/lifecycle_scripts.py``          -- ``apm compile`` with a trusted script
* ``install/local_bundle_handler.py``    -- ``apm install <bundle>``
* ``install/exec_gate.py`` (replay)      -- ``apm audit`` drift replay
* ``security/executables.py``            -- ``apm uninstall`` survivor rebuild

Decisive assertions are what is deployed (snapshot), the exit code per
``fetch_failure_default`` and the announcement line.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from apm_cli.install.errors import PolicyViolationError
from apm_cli.policy.discovery import PolicyFetchResult
from apm_cli.policy.outcome_routing import resolve_trust_policy, route_discovery_outcome
from apm_cli.policy.schema import ApmPolicy
from tests.lifecycle import driver
from tests.lifecycle.rows import Row, RowReplay
from tests.lifecycle.world import World, use_policy
from tests.lifecycle.world.services import REGISTRY_SERVER

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.promise(
        "P-OWN-trust-policy-outcome",
        "P-INV-I4",
        "P-INV-I7",
        "P-INV-I8",
        "P-LAW-outcome.status_matches_state",
    ),
]

# -- Part 1: the owner table -----------------------------------------------------

ENFORCEMENTS = ("off", "warn", "block")
FETCH_FAILURE_DEFAULTS = ("warn", "block")
SOURCE = "org:lifecycle-org/.github"

# When an outcome fails closed: never, always, when the project's
# fetch_failure_default is block, or when the cached policy's fetch_failure is block.
NEVER, ALWAYS, PROJECT_KNOB, CACHED_KNOB = "never", "always", "project-knob", "cached-knob"


@dataclass(frozen=True)
class Route:
    blocks: str
    returns_policy: bool
    announces: tuple[str, ...]  # logger calls in order: "resolved" / "miss"
    carries_policy: bool = False  # discovery attaches the (merged) policy


ROUTES: dict[str, Route] = {
    "found": Route(NEVER, True, ("resolved",), carries_policy=True),
    "disabled": Route(NEVER, False, ()),
    "absent": Route(PROJECT_KNOB, False, ("miss",)),
    "empty": Route(PROJECT_KNOB, False, ("miss",), carries_policy=True),
    "no_git_remote": Route(PROJECT_KNOB, False, ("miss",)),
    "malformed": Route(PROJECT_KNOB, False, ("miss",)),
    "cache_miss_fetch_fail": Route(PROJECT_KNOB, False, ("miss",)),
    "garbage_response": Route(PROJECT_KNOB, False, ("miss",)),
    "incomplete_chain": Route(ALWAYS, False, ("miss",), carries_policy=True),
    "cached_stale": Route(CACHED_KNOB, True, ("resolved", "miss"), carries_policy=True),
    "hash_mismatch": Route(ALWAYS, False, ("miss",)),
}


class RecordingLogger:
    """A real logger stand-in: records the two announcement calls the owner makes."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def policy_resolved(self, **kwargs) -> None:
        self.calls.append(("resolved", kwargs))

    def policy_discovery_miss(self, **kwargs) -> None:
        self.calls.append(("miss", kwargs))


def _fetch_result(outcome: str, enforcement: str, cached_fetch_failure: str) -> PolicyFetchResult:
    route = ROUTES[outcome]
    policy = None
    if route.carries_policy:
        policy = ApmPolicy(
            name="table", enforcement=enforcement, fetch_failure=cached_fetch_failure
        )
    return PolicyFetchResult(
        policy=policy,
        source=SOURCE,
        cached=outcome == "cached_stale",
        cache_stale=outcome == "cached_stale",
        outcome=outcome,
        error=None if route.blocks == NEVER else f"{outcome} detail",
    )


def _blocks(route: Route, fetch_failure_default: str, cached_fetch_failure: str) -> bool:
    return {
        NEVER: False,
        ALWAYS: True,
        PROJECT_KNOB: fetch_failure_default == "block",
        CACHED_KNOB: cached_fetch_failure == "block",
    }[route.blocks]


def _outcome_cases():
    for outcome in ROUTES:
        for enforcement in ENFORCEMENTS:
            for knob in FETCH_FAILURE_DEFAULTS:
                for cached in ("warn", "block"):
                    yield pytest.param(
                        outcome,
                        enforcement,
                        knob,
                        cached,
                        id=f"{outcome}-{enforcement}-{knob}-c{cached}",
                    )


@pytest.mark.parametrize(("outcome", "enforcement", "knob", "cached"), list(_outcome_cases()))
def test_route_discovery_outcome_table(
    outcome: str, enforcement: str, knob: str, cached: str
) -> None:
    route = ROUTES[outcome]
    result = _fetch_result(outcome, enforcement, cached)
    blocks = _blocks(route, knob, cached)

    log = RecordingLogger()
    if blocks:
        with pytest.raises(PolicyViolationError) as raised:
            route_discovery_outcome(result, logger=log, fetch_failure_default=knob)
        assert raised.value.policy_source == SOURCE
        assert str(raised.value).startswith("Install blocked: ")
        if route.blocks == PROJECT_KNOB:
            assert f"outcome={outcome}" in str(raised.value)
            assert "policy.fetch_failure_default=block" in str(raised.value)
    else:
        returned = route_discovery_outcome(result, logger=log, fetch_failure_default=knob)
        assert returned is (result.policy if route.returns_policy else None)
    # The announcement does not depend on the decision: it precedes it.
    assert tuple(kind for kind, _ in log.calls) == route.announces
    for kind, kwargs in log.calls:
        assert kwargs["source"] == SOURCE
        if kind == "miss":
            assert kwargs["outcome"] == outcome
        else:
            assert kwargs["enforcement"] == enforcement

    # A dry-run preview routes the same way and never fails closed.
    preview = RecordingLogger()
    returned = route_discovery_outcome(
        result, logger=preview, fetch_failure_default=knob, raise_blocking_errors=False
    )
    assert returned is (result.policy if route.returns_policy else None)
    assert preview.calls == log.calls


def _trust_cases():
    for outcome in ROUTES:
        for knob in FETCH_FAILURE_DEFAULTS:
            for cached in ("warn", "block"):
                yield pytest.param(outcome, knob, cached, id=f"{outcome}-{knob}-c{cached}")


@pytest.mark.parametrize(("outcome", "knob", "cached"), list(_trust_cases()))
def test_resolve_trust_policy_table(tmp_path: Path, outcome: str, knob: str, cached: str) -> None:
    """Trust readers route like installs, reading the knob from the project apm.yml."""
    (tmp_path / "apm.yml").write_text(
        yaml.safe_dump({"name": "table", "policy": {"fetch_failure_default": knob}}),
        encoding="utf-8",
    )
    route = ROUTES[outcome]
    result = _fetch_result(outcome, "block", cached)
    log = RecordingLogger()
    if _blocks(route, knob, cached):
        with pytest.raises(PolicyViolationError, match=r"^Blocked by org policy: "):
            resolve_trust_policy(tmp_path, logger=log, fetch_result=result)
        # The exception is the only report of a blocking outcome.
        assert log.calls == []
        return
    returned = resolve_trust_policy(tmp_path, logger=log, fetch_result=result)
    assert returned is (result.policy if route.returns_policy else None)
    # A trust reader never announces a resolved policy again; a miss is reported once.
    assert [kind for kind, _ in log.calls] == [kind for kind in route.announces if kind == "miss"]
    silent = RecordingLogger()
    resolve_trust_policy(tmp_path, logger=silent, fetch_result=result, announce=False)
    assert silent.calls == []


# -- Part 2: one routing E2E per decision site ------------------------------------

MCP_SERVER = "p-exec-mcp"


def _replay(world: World, then: dict | None = None, known: tuple[dict, ...] = ()) -> RowReplay:
    row = Row(
        id="T7b/policy-outcome",
        promises=("P-OWN-trust-policy-outcome",),
        packages=("p-skill", "p-exec"),
        targets=("copilot",),
        scope="project",
        given=(),
        when=(),
        then=then or {},
        known_cells=known,
    )
    return RowReplay(world, row)


def _set_fetch_failure_default(world: World, value: str) -> None:
    manifest = world.project / "apm.yml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    policy = data.get("policy") or {}
    policy["fetch_failure_default"] = value
    data["policy"] = policy
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")


def _govern(world: World, variant: str, knob: str) -> None:
    use_policy(world, variant)
    _set_fetch_failure_default(world, knob)


def _output(replay: RowReplay) -> str:
    assert replay.last is not None
    return " ".join(f"{replay.last.stdout}\n{replay.last.stderr}".split())


def _install_parked_exec(world: World) -> RowReplay:
    """p-skill deployed and p-exec declared with its executables parked, under ``allow``."""
    use_policy(world, "allow")
    replay = _replay(world)
    replay.step("apm install p-skill p-exec")
    assert replay.observe().deployed == {"p-skill/skill/copilot"}
    return replay


# policy_gate: the project knob decides an unreachable policy; a hash mismatch always blocks.
@pytest.mark.parametrize(
    ("variant", "knob", "code", "announce"),
    [
        pytest.param(
            "unreachable",
            "block",
            1,
            "policy.fetch_failure_default=block",
            id="unreachable-block",
            marks=pytest.mark.lifecycle_smoke,
        ),
        pytest.param("unreachable", "warn", 0, "Could not fetch org policy", id="unreachable-warn"),
        pytest.param("hash-mismatch", "warn", 1, "policy hash mismatch", id="hash-mismatch-warn"),
    ],
)
def test_install_gate_routes_the_outcome(
    lifecycle_world: World, variant: str, knob: str, code: int, announce: str
) -> None:
    _govern(lifecycle_world, variant, knob)
    replay = _replay(
        lifecycle_world, then={"exit": code, "deployed": "expected", "announce": announce}
    )
    replay.step({"run": "apm install p-skill", "exit": code})
    replay.check_then()
    assert ("p-skill" in (lifecycle_world.project / "apm.yml").read_text()) is (code == 0)


@pytest.mark.xfail(
    strict=True, reason="#0 a blocked install also prints that it proceeds without enforcement"
)
def test_blocked_install_never_claims_to_proceed(lifecycle_world: World) -> None:
    _govern(lifecycle_world, "unreachable", "block")
    replay = _replay(lifecycle_world)
    replay.step({"run": "apm install p-skill", "exit": 1})
    assert "proceeding without policy enforcement" not in _output(replay)


# install_preflight: an MCP-only install routes through the preflight helper.
def test_mcp_install_preflight_fails_closed_on_hash_mismatch(lifecycle_world: World) -> None:
    _govern(lifecycle_world, "hash-mismatch", "warn")
    replay = _replay(lifecycle_world, then={"exit": 1, "deployed": "expected"})
    replay.step({"run": f"apm install --mcp {REGISTRY_SERVER}", "exit": 1})
    replay.check_then()
    assert "Policy hash mismatch" in _output(replay)
    assert REGISTRY_SERVER not in (lifecycle_world.project / "apm.yml").read_text()
    assert not (lifecycle_world.project / ".mcp.json").exists()


@pytest.mark.xfail(strict=True, reason="#0 the MCP install block reason is not printed")
def test_mcp_install_preflight_announces_the_project_knob(lifecycle_world: World) -> None:
    _govern(lifecycle_world, "unreachable", "block")
    replay = _replay(lifecycle_world, then={"exit": 1, "deployed": "expected"})
    replay.step({"run": f"apm install --mcp {REGISTRY_SERVER}", "exit": 1})
    replay.check_then()
    assert not (lifecycle_world.project / ".mcp.json").exists()
    assert "policy.fetch_failure_default=block" in _output(replay)


# lifecycle_scripts: a trusted project script runs only when the outcome does not block.
@pytest.mark.parametrize(
    ("knob", "runs"),
    [
        pytest.param("block", False, id="block", marks=pytest.mark.lifecycle_smoke),
        pytest.param("warn", True, id="warn"),
    ],
)
def test_lifecycle_scripts_follow_the_outcome(
    lifecycle_world: World, knob: str, runs: bool
) -> None:
    world = lifecycle_world
    marker = world.root / "pre-compile-ran"
    _govern(world, "unreachable", knob)
    manifest = world.project / "apm.yml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["lifecycle"] = {"pre-compile": [{"run": f"touch {marker}"}]}
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")
    replay = _replay(world)
    trusted = driver.run(world, ["lifecycle", "trust"])
    assert trusted.returncode == 0, trusted.stdout + trusted.stderr
    replay.step({"run": "apm compile", "exit": 0})
    assert marker.exists() is runs
    suppressed = "Lifecycle scripts suppressed by org policy" in _output(replay)
    assert suppressed is not runs


# local_bundle_handler: the bundle's trust reader fails closed under the project knob.
@pytest.mark.lifecycle_smoke
def test_local_bundle_trust_reader_fails_closed(lifecycle_world: World) -> None:
    world = lifecycle_world
    _govern(world, "unreachable", "block")
    bundle = world.packages["p-exec:bundle"].spec
    replay = _replay(world, then={"exit": 1, "deployed": "expected"})
    replay.step({"run": f"apm install {bundle}", "exit": 1})
    replay.check_then()
    output = _output(replay)
    assert "Blocked by org policy: no enforceable org policy was resolved" in output
    assert "outcome=cache_miss_fetch_fail" in output
    assert not (world.project / ".github" / "hooks").exists()
    assert not (world.project / ".mcp.json").exists()


# exec_gate.ReplayExecAllowMap: audit's drift replay reports the block and writes nothing.
@pytest.mark.lifecycle_smoke
def test_audit_replay_trust_reader_reports_the_block(lifecycle_world: World) -> None:
    world = lifecycle_world
    replay = _install_parked_exec(world)
    use_policy(world, "hash-mismatch")
    before = replay.observe()
    replay.step({"run": "apm audit", "exit": 0})
    after = replay.observe()
    assert after.state.files == before.state.files
    assert after.state.manifest_bytes == before.state.manifest_bytes
    assert after.state.lockfile_bytes == before.state.lockfile_bytes
    assert "drift replay blocked: Blocked by org policy: policy hash mismatch" in _output(replay)


# security/executables: uninstall's survivor rebuild fails closed and admits nothing.
def test_uninstall_trust_reader_fails_closed(lifecycle_world: World) -> None:
    world = lifecycle_world
    replay = _install_parked_exec(world)
    use_policy(world, "hash-mismatch")
    replay.step({"run": "apm uninstall p-skill", "exit": 1})
    assert "Blocked by org policy: policy hash mismatch" in _output(replay)
    deployed = replay.observe().deployed
    assert not any(surface.startswith("p-exec/") for surface in deployed), deployed
    assert MCP_SERVER not in (
        (world.project / ".mcp.json").read_text() if (world.project / ".mcp.json").exists() else ""
    )
