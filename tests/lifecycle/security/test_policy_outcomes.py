"""Policy discovery outcomes: the owner table and one routing E2E per decision site (#4108).

PROMISE: P-OWN-trust-policy-outcome -- every discovery outcome routes through
one owner that announces it and fails closed per ``policy.fetch_failure_default``
(source: .apm/architecture/owners/install-deployment.json, id
``trust-policy-outcome``; owner ``src/apm_cli/policy/outcome_routing.py``).

This module is a framework of two tables; a row is data, never code:

* ``OWNER_ROWS`` -- one row per ``P|owner|<class>`` obligation. The owner
  test feeds real ``PolicyFetchResult`` / ``ApmPolicy`` inputs and a real
  project ``apm.yml`` to ``route_discovery_outcome`` and
  ``resolve_trust_policy`` (never patched), across enforcement off / warn /
  block and the dry-run preview, and discharges the row at ``unit`` strength.
* ``SITE_SLICES`` -- one slice per decision site (a caller of the owner):
  how to arrange the world, the command line, and per-class expectations.
  ``CLASS_SETUPS`` turns a class into a world input (policy variant, project
  knob, git remote, cache). The site test runs the command through
  ``RowReplay`` (driver + ``check_cross_cutting`` I1-I9 per step) and
  discharges ``P|<site>|<class>`` at ``e2e`` strength.

Add a site: add one ``SiteSlice`` to ``SITE_SLICES``. Add a class: add its
setup to ``CLASS_SETUPS`` and an ``Expect`` to each slice that covers it.
A row whose CLI behavior is wrong today carries ``xfail="#<issue> <neutral text>"``
and becomes a strict expected failure.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import yaml

from apm_cli.install.errors import PolicyViolationError
from apm_cli.policy.discovery import PolicyFetchResult
from apm_cli.policy.outcome_routing import resolve_trust_policy, route_discovery_outcome
from apm_cli.policy.schema import ApmPolicy
from tests.lifecycle import driver, oracles, rows
from tests.lifecycle.obligation_space import security_obligations
from tests.lifecycle.rows import Row, RowReplay
from tests.lifecycle.world import World, use_policy
from tests.lifecycle.world.packages import INERT_COMMAND as INERT_SERVER
from tests.lifecycle.world.services import API_HOST, ORG, REGISTRY_SERVER

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

# == Tier 1: the owner table ======================================================

ENFORCEMENTS = ("off", "warn", "block")
SOURCE = "org:lifecycle-org/.github"
MISS, RESOLVED = "miss", "resolved"


@dataclass(frozen=True)
class OwnerRow:
    """One owner class: the inputs and the decision the owner must make.

    ``knob`` is the project ``fetch_failure_default`` and ``cached`` the
    cached policy's own ``fetch_failure``; the knob that must not decide a
    class is set to the value that would flip it if the owner consulted it.
    """

    outcome: str
    knob: str
    cached: str
    blocks: bool
    returns_policy: bool
    announces: tuple[str, ...]  # logger calls in order
    carries_policy: bool = False  # discovery attaches the (merged) policy


OWNER_ROWS: Mapping[str, OwnerRow] = {
    "P|owner|found": OwnerRow("found", "block", "block", False, True, (RESOLVED,), True),
    "P|owner|disabled": OwnerRow("disabled", "block", "block", False, False, ()),
    "P|owner|absent/block": OwnerRow("absent", "block", "warn", True, False, (MISS,)),
    "P|owner|absent/warn": OwnerRow("absent", "warn", "block", False, False, (MISS,)),
    "P|owner|empty/block": OwnerRow("empty", "block", "warn", True, False, (MISS,), True),
    "P|owner|empty/warn": OwnerRow("empty", "warn", "block", False, False, (MISS,), True),
    "P|owner|no_git_remote/block": OwnerRow("no_git_remote", "block", "warn", True, False, (MISS,)),
    "P|owner|no_git_remote/warn": OwnerRow("no_git_remote", "warn", "block", False, False, (MISS,)),
    "P|owner|malformed/block": OwnerRow("malformed", "block", "warn", True, False, (MISS,)),
    "P|owner|malformed/warn": OwnerRow("malformed", "warn", "block", False, False, (MISS,)),
    "P|owner|cache_miss_fetch_fail/block": OwnerRow(
        "cache_miss_fetch_fail", "block", "warn", True, False, (MISS,)
    ),
    "P|owner|cache_miss_fetch_fail/warn": OwnerRow(
        "cache_miss_fetch_fail", "warn", "block", False, False, (MISS,)
    ),
    "P|owner|garbage_response/block": OwnerRow(
        "garbage_response", "block", "warn", True, False, (MISS,)
    ),
    "P|owner|garbage_response/warn": OwnerRow(
        "garbage_response", "warn", "block", False, False, (MISS,)
    ),
    "P|owner|incomplete_chain": OwnerRow(
        "incomplete_chain", "warn", "warn", True, False, (MISS,), True
    ),
    "P|owner|hash_mismatch": OwnerRow("hash_mismatch", "warn", "warn", True, False, (MISS,)),
    "P|owner|cached_stale/org-block": OwnerRow(
        "cached_stale", "warn", "block", True, True, (RESOLVED, MISS), True
    ),
    "P|owner|cached_stale/warn": OwnerRow(
        "cached_stale", "block", "warn", False, True, (RESOLVED, MISS), True
    ),
}


class RecordingLogger:
    """Records the two announcement calls the owner makes."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def policy_resolved(self, **kwargs) -> None:
        self.calls.append((RESOLVED, kwargs))

    def policy_discovery_miss(self, **kwargs) -> None:
        self.calls.append((MISS, kwargs))


def _fetch_result(row: OwnerRow, enforcement: str) -> PolicyFetchResult:
    policy = None
    if row.carries_policy:
        policy = ApmPolicy(name="table", enforcement=enforcement, fetch_failure=row.cached)
    return PolicyFetchResult(
        policy=policy,
        source=SOURCE,
        cached=row.outcome == "cached_stale",
        cache_stale=row.outcome == "cached_stale",
        outcome=row.outcome,
        error=f"{row.outcome} detail" if row.announces == (MISS,) else None,
    )


def _route(result: PolicyFetchResult, row: OwnerRow, *, preview: bool) -> tuple:
    """What ``route_discovery_outcome`` decided and announced, as plain values."""
    log = RecordingLogger()
    try:
        returned = route_discovery_outcome(
            result, logger=log, fetch_failure_default=row.knob, raise_blocking_errors=not preview
        )
        decision = (
            "policy" if returned is not None and returned is result.policy else repr(returned)
        )
    except PolicyViolationError as exc:
        decision = "blocked:" + str(exc).split(":", 1)[0]
    announced = tuple(
        (kind, kwargs.get("outcome") if kind == MISS else kwargs.get("enforcement"))
        for kind, kwargs in log.calls
    )
    return decision, announced


def _trust(result: PolicyFetchResult, row: OwnerRow, project: Path) -> tuple:
    """What ``resolve_trust_policy`` decided and reported for a trust reader."""
    (project / "apm.yml").write_text(
        yaml.safe_dump({"name": "table", "policy": {"fetch_failure_default": row.knob}}),
        encoding="utf-8",
    )
    log = RecordingLogger()
    try:
        returned = resolve_trust_policy(project, logger=log, fetch_result=result)
        decision = (
            "policy" if returned is not None and returned is result.policy else repr(returned)
        )
    except PolicyViolationError as exc:
        decision = "blocked:" + str(exc).split(":", 1)[0]
    return decision, tuple(kind for kind, _ in log.calls)


def _expected_owner(row: OwnerRow, enforcement: str) -> dict:
    kept = "policy" if row.returns_policy else "None"
    announced = tuple(
        (kind, row.outcome if kind == MISS else enforcement) for kind in row.announces
    )
    misses = tuple(kind for kind in row.announces if kind == MISS)
    return {
        "route": ("blocked:Install blocked" if row.blocks else kept, announced),
        "preview": (kept, announced),
        # A trust reader never re-announces a resolved policy; a block is reported
        # only by its exception.
        "trust": ("blocked:Blocked by org policy", ()) if row.blocks else (kept, misses),
    }


@pytest.mark.parametrize("cell_id", list(OWNER_ROWS))
def test_owner_routes_each_outcome(tmp_path: Path, cell_id: str) -> None:
    row = OWNER_ROWS[cell_id]
    expected, observed = {}, {}
    for enforcement in ENFORCEMENTS:
        result = _fetch_result(row, enforcement)
        expected[enforcement] = _expected_owner(row, enforcement)
        observed[enforcement] = {
            "route": _route(result, row, preview=False),
            "preview": _route(result, row, preview=True),
            "trust": _trust(result, row, tmp_path),
        }
    oracles.check_obligation(
        cell_id, strength="unit", evidence=oracles.Evidence(expected=expected, observed=observed)
    )


# == Tier 2: one routing E2E per decision site =====================================

POLICY_CONTENTS = "/contents/apm-policy.yml"
SERVED_POLICY_CONTENTS = "/.github/contents/apm-policy.yml"
SCRIPT_MARKER = ".lifecycle-script-ran"
# Documented script log (reference/cli/lifecycle.md): $APM_HOME/logs/scripts.log.
SCRIPT_LOG = (".apm/logs",)
MCP_SERVER = "p-exec-mcp"


@dataclass(frozen=True)
class Expect:
    """What one site does for one class."""

    exit: int
    output: tuple[str, ...] = ()  # regexes the combined output must match
    forbids: tuple[str, ...] = ()  # regexes it must not match
    absent: tuple[str, ...] = ()  # project paths absent after the command
    present: tuple[str, ...] = ()  # project paths present after the command
    unchanged: tuple[str, ...] = ()  # project path prefixes byte-identical
    # True: the command requested the org policy file; False: it did not fetch the
    # served policy repository (a warm cache may still probe ``.github-private``).
    policy_fetch: bool | None = None
    xfail: str | None = None  # "#<issue> <neutral text>" -> strict expected failure
    smoke: bool = False
    # Overrides CLASS_SETUPS[cls] when the site needs another input for the class.
    setup: Callable[[World, RowReplay], None] | None = None
    # HOME paths the command may write besides receipts and bookkeeping.
    home_writes: tuple[str, ...] = ()
    # Overrides the slice command when the class needs another command line.
    command: Callable[[World], str] | None = None


@dataclass(frozen=True)
class SiteSlice:
    """One decision site: arrange the world, the command line, per-class expectations."""

    arrange: Callable[[World, RowReplay], None]
    command: Callable[[World], str]
    classes: Mapping[str, Expect] = field(default_factory=dict)


# -- class setups: turn a class into world inputs -----------------------------------


def _set_knob(world: World, knob: str) -> None:
    manifest = world.project / "apm.yml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["policy"] = {**(data.get("policy") or {}), "fetch_failure_default": knob}
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")


def _variant(variant: str, knob: str) -> Callable[[World, RowReplay], None]:
    def setup(world: World, replay: RowReplay) -> None:
        del replay
        use_policy(world, variant)
        _set_knob(world, knob)

    return setup


def _absent(knob: str) -> Callable[[World, RowReplay], None]:
    """The project remote names an org that publishes no policy repository."""

    def setup(world: World, replay: RowReplay) -> None:
        del replay
        use_policy(world, "allow")
        remote = f"https://github.com/{ORG}/consumer.git"
        git = ("git", "-C", str(world.project), "remote", "set-url", "origin", remote)
        subprocess.run(git, env=world.env, check=True)
        _set_knob(world, knob)

    return setup


def _no_git_remote(knob: str) -> Callable[[World, RowReplay], None]:
    def setup(world: World, replay: RowReplay) -> None:
        del replay
        shutil.rmtree(world.project / ".git", ignore_errors=True)
        _set_knob(world, knob)

    return setup


def _disabled(world: World, replay: RowReplay) -> None:
    """``APM_POLICY_DISABLE=1`` over a policy that would deny."""
    del replay
    use_policy(world, "deny-pkg")
    _set_knob(world, "block")
    world.env["APM_POLICY_DISABLE"] = "1"


def _warmed(variant: str) -> Callable[[World, RowReplay], None]:
    """A found policy already in the cache, fetched moments ago."""

    def setup(world: World, replay: RowReplay) -> None:
        del replay
        use_policy(world, variant)
        result = driver.run(world, ["policy", "status"])
        assert result.returncode == 0, result.stdout + result.stderr

    return setup


_warm = _warmed("deny-pkg")


def _aged(seconds: int, variant: str = "deny-pkg") -> Callable[[World, RowReplay], None]:
    """A found policy cached *seconds* ago, and every refresh now fails to connect."""

    def setup(world: World, replay: RowReplay) -> None:
        _warmed(variant)(world, replay)
        _set_knob(world, "warn")
        _age_cache(world, seconds)
        for key in [key for key in world.env if key.lower() in _PROXY_KEYS]:
            world.env[key] = _DEAD_PROXY

    return setup


def _serve_policy(world: World, org: str, document: bytes) -> None:
    """Serve *document* as ``<org>/.github`` for this test (``WorldServices.serve``)."""
    root = world.root / "t7b-policies" / org
    tree = root / f"src{len(list(root.glob('src*')))}" if root.exists() else root / "src0"
    tree.mkdir(parents=True)
    (tree / "apm-policy.yml").write_bytes(document)
    bare = tree.with_suffix(".git")
    git = ("git", "-c", "user.name=lifecycle", "-c", "user.email=lifecycle@example.invalid")
    for args in (
        ("-C", str(tree), "init", "-q", "--initial-branch=main"),
        ("-C", str(tree), "add", "-A"),
        ("-C", str(tree), "commit", "-qm", "policy"),
        ("clone", "-q", "--bare", str(tree), str(bare)),
    ):
        subprocess.run((*git, *args), env=world.env, check=True, capture_output=True)
    world.services.serve(f"{org}/.github", bare)


def _point_remote(world: World, org: str) -> None:
    git = ("git", "-C", str(world.project))
    if not (world.project / ".git").exists():
        subprocess.run((*git, "init", "-q", "--initial-branch=main"), env=world.env, check=True)
    remote = f"https://github.com/{org}/consumer.git"
    existing = subprocess.run(
        (*git, "remote"), env=world.env, capture_output=True, text=True, check=True
    ).stdout.split()
    verb = "set-url" if "origin" in existing else "add"
    subprocess.run((*git, "remote", verb, "origin", remote), env=world.env, check=True)


def _document(**fields: object) -> bytes:
    return yaml.safe_dump(
        {"name": "t7b", "version": "1.0", "enforcement": "block", **fields}
    ).encode()


# Test-local policy documents (the world serves none of these outcomes).
MALFORMED_POLICY = b"name: t7b\nversion: '1.0'\nenforcement: sometimes\n"
GARBAGE_POLICY = b"- not\n- a mapping\n"
DENY_EXEC = {"dependencies": {"deny": ["**/p-exec", f"{ORG}/p-exec"]}}
MISSING_PARENT = "policy-t7b-missing/.github"


def _local(org: str, document: bytes, knob: str) -> Callable[[World, RowReplay], None]:
    def setup(world: World, replay: RowReplay) -> None:
        del replay
        _serve_policy(world, org, document)
        _point_remote(world, org)
        _set_knob(world, knob)

    return setup


def _cached_stale(fetch_failure: str, knob: str) -> Callable[[World, RowReplay], None]:
    """A cached deny policy past its TTL whose refresh now returns a non-mapping body."""
    org = f"policy-t7b-stale-{fetch_failure}"

    def setup(world: World, replay: RowReplay) -> None:
        del replay
        _serve_policy(world, org, _document(fetch_failure=fetch_failure, **DENY_EXEC))
        _point_remote(world, org)
        warmed = driver.run(world, ["policy", "status"])
        assert warmed.returncode == 0, warmed.stdout + warmed.stderr
        _age_cache(world, 2 * _HOUR)
        _serve_policy(world, org, GARBAGE_POLICY)
        _set_knob(world, knob)

    return setup


def _age_cache(world: World, seconds: int) -> None:
    metas = [path for path in world.root.rglob("*.meta.json") if "cached_at" in path.read_text()]
    assert metas, "no policy cache entry to age"
    for meta in metas:
        data = json.loads(meta.read_text(encoding="utf-8"))
        data["cached_at"] = time.time() - seconds
        meta.write_text(json.dumps(data), encoding="utf-8")


_PROXY_KEYS = ("https_proxy", "http_proxy", "all_proxy")
_DEAD_PROXY = "http://127.0.0.1:9"
_HOUR, _DAY = 3600, 24 * 3600

CLASS_SETUPS: Mapping[str, Callable[[World, RowReplay], None]] = {
    "malformed/block": _local("policy-t7b-malformed", MALFORMED_POLICY, "block"),
    "malformed/warn": _local("policy-t7b-malformed", MALFORMED_POLICY, "warn"),
    "garbage_response/block": _local("policy-t7b-garbage", GARBAGE_POLICY, "block"),
    "garbage_response/warn": _local("policy-t7b-garbage", GARBAGE_POLICY, "warn"),
    "incomplete_chain": _local("policy-t7b-chain", _document(extends=MISSING_PARENT), "warn"),
    "cached_stale/warn": _cached_stale("warn", "block"),
    "cached_stale/org-block": _cached_stale("block", "warn"),
    "cache=stale<7d": _aged(2 * _HOUR),
    "cache=stale>7d-or-expired": _aged(8 * _DAY),
    "found": _variant("deny-pkg", "block"),
    "cache=cold": _variant("deny-pkg", "block"),
    "cache=warm-fresh": _warm,
    "disabled": _disabled,
    "hash_mismatch": _variant("hash-mismatch", "warn"),
    "cache_miss_fetch_fail/block": _variant("unreachable", "block"),
    "cache_miss_fetch_fail/warn": _variant("unreachable", "warn"),
    "empty/block": _variant("allow", "block"),
    "empty/warn": _variant("allow", "warn"),
    "absent/block": _absent("block"),
    "absent/warn": _absent("warn"),
    "no_git_remote/block": _no_git_remote("block"),
    "no_git_remote/warn": _no_git_remote("warn"),
}


# -- arrangements shared by slices ----------------------------------------------------


def _nothing(world: World, replay: RowReplay) -> None:
    del world, replay


def _installed_parked_exec(world: World, replay: RowReplay) -> None:
    """p-skill deployed and p-exec declared with its executables parked, under ``allow``."""
    use_policy(world, "allow")
    replay.step("apm install p-skill p-exec")


def _trusted_script(world: World, replay: RowReplay) -> None:
    """p-skill deployed and a trusted pre-compile script that writes a marker."""
    use_policy(world, "allow")
    replay.step("apm install p-skill")
    manifest = world.project / "apm.yml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["lifecycle"] = {"pre-compile": [{"run": f"touch {world.project / SCRIPT_MARKER}"}]}
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")
    trusted = driver.run(world, ["lifecycle", "trust"])
    assert trusted.returncode == 0, trusted.stdout + trusted.stderr


def _spec(key: str) -> Callable[[World], str]:
    return lambda world: world.packages[key].spec


# Output patterns of the owner's announcements and decisions.
KNOB_BLOCK = r"policy\.fetch_failure_default=block"
UNREACHABLE = r"Could not fetch org policy"
HASH = r"(?i)policy hash mismatch"
EMPTY = r"present but empty"
DENIED = r"denied by pattern"
TRUST_BLOCK = r"Blocked by org policy: "
# A stale cached policy that declares fetch_failure=block blocks audit as it blocks install.
_STALE_BLOCK = TRUST_BLOCK + r"org policy refresh failed and the cached policy"
SKILL = ".agents/skills/p-skill/SKILL.md"
HOOK = ".github/hooks/p-exec-session.json"

_OFFLINE = "S-pending (neutral issue to follow): offline use of cached policy"
_PARTIAL_UNINSTALL = "#4185 uninstall exits 1 after it already changed the manifest, lock and disk"
_CHECK_FAILED = r"check\(s\) failed"
_UNAVAILABLE = r"Org policy unavailable \("
_CONSENT_OK = r"config-consistency\s*\|\s*MCP configs match lockfile baseline"
_POLICY_DENY_PKG = r"Policy: org:policy-deny-pkg/\.github -- enforcement=block"
_DENY_ALL_SUPPRESSED = r"Lifecycle scripts suppressed by org policy \(executables\.deny_all\)"


def _unavailable(outcome: str) -> str:
    return rf"Org policy unavailable \({outcome}\); consent was evaluated without org deny"


def _not_applied(outcome: str) -> str:
    return rf"Org policy not applied \(outcome={outcome},"


def _replay_blocked(reason: str) -> Expect:
    return Expect(
        0,
        (r"drift check could not run: drift replay blocked: Blocked by org policy: " + reason,),
        unchanged=("*",),
    )


def _replay_ran() -> Expect:
    return Expect(0, forbids=(r"drift replay blocked",), unchanged=("*",))


def _local_mcp(world: World) -> str:
    del world
    return f"apm install --mcp t7b-local -- {INERT_SERVER} --stdio"


# The decision sites, keyed by the obligation site name.
SITE_SLICES: dict[str, SiteSlice] = {}

# install/phases/policy_gate.py -- the install pipeline.
SITE_SLICES["install-pipeline"] = SiteSlice(
    arrange=_nothing,
    command=lambda world: "apm install p-skill p-exec",
    classes={
        "cache_miss_fetch_fail/block": Expect(
            1, (KNOB_BLOCK,), absent=(SKILL, "apm.lock.yaml"), policy_fetch=True, smoke=True
        ),
        "cache_miss_fetch_fail/warn": Expect(0, (UNREACHABLE,), present=(SKILL,)),
        "cache=cold": Expect(1, (DENIED,), absent=(SKILL, HOOK), policy_fetch=True),
        "cache=warm-fresh": Expect(1, (DENIED,), absent=(SKILL, HOOK), policy_fetch=False),
        "cache=stale<7d": Expect(
            1,
            (DENIED, r"(?i)stale"),
            absent=(SKILL, HOOK),
            xfail=_OFFLINE,
        ),
        "cache=stale>7d-or-expired": Expect(0, (UNREACHABLE,), present=(SKILL,)),
    },
)

# policy/install_preflight.py -- an MCP-only install.
SITE_SLICES["mcp-install-preflight"] = SiteSlice(
    arrange=_nothing,
    command=lambda world: f"apm install --mcp {REGISTRY_SERVER}",
    classes={
        "cache_miss_fetch_fail/block": Expect(
            1,
            (KNOB_BLOCK,),
            absent=(".mcp.json",),
            unchanged=("apm.yml",),
            xfail="#4179 the MCP-only install exits 1 without printing the reason",
        ),
        "cache_miss_fetch_fail/warn": Expect(0, (UNREACHABLE,)),
        "cache=cold": Expect(
            0, (r"Policy: org:policy-deny-pkg/\.github -- enforcement=block",), policy_fetch=True
        ),
        "cache=warm-fresh": Expect(
            0, (r"\(cached, fetched \d+s ago\) -- enforcement=block",), policy_fetch=False
        ),
        "cache=stale<7d": Expect(0, (r"(?i)stale",), command=_local_mcp, xfail=_OFFLINE),
        "cache=stale>7d-or-expired": Expect(0, (UNREACHABLE,), command=_local_mcp),
    },
)

# core/lifecycle_scripts.py -- a trusted project script runs only when the outcome allows.
SITE_SLICES["lifecycle-scripts"] = SiteSlice(
    arrange=_trusted_script,
    command=lambda world: "apm compile",
    classes={
        "cache_miss_fetch_fail/block": Expect(
            0, (r"Lifecycle scripts suppressed by org policy",), absent=(SCRIPT_MARKER,), smoke=True
        ),
        "cache_miss_fetch_fail/warn": Expect(
            0,
            forbids=(r"Lifecycle scripts suppressed",),
            present=(SCRIPT_MARKER,),
            home_writes=SCRIPT_LOG,
        ),
        "cache=cold": Expect(
            0,
            (r"Lifecycle scripts suppressed by org policy \(executables\.deny_all\)",),
            absent=(SCRIPT_MARKER,),
            policy_fetch=True,
            setup=_variant("exec-deny-all", "warn"),
        ),
        "cache=warm-fresh": Expect(
            0,
            (_DENY_ALL_SUPPRESSED,),
            absent=(SCRIPT_MARKER,),
            policy_fetch=False,
            setup=_warmed("exec-deny-all"),
        ),
        "cache=stale<7d": Expect(
            0,
            (_DENY_ALL_SUPPRESSED,),
            absent=(SCRIPT_MARKER,),
            setup=_aged(2 * _HOUR, "exec-deny-all"),
            home_writes=SCRIPT_LOG,
            xfail=_OFFLINE,
        ),
        "cache=stale>7d-or-expired": Expect(
            0,
            forbids=(r"Lifecycle scripts suppressed",),
            present=(SCRIPT_MARKER,),
            setup=_aged(8 * _DAY, "exec-deny-all"),
            home_writes=SCRIPT_LOG,
        ),
    },
)

# install/local_bundle_handler.py effective_bundle_allow_map -- a local bundle's trust reader.
SITE_SLICES["bundle-allow-map(getattr cache_only)"] = SiteSlice(
    arrange=_nothing,
    command=lambda world: f"apm install {world.packages['p-exec:bundle'].spec}",
    classes={
        "cache_miss_fetch_fail/block": Expect(
            1, (TRUST_BLOCK + "no enforceable", KNOB_BLOCK), absent=(HOOK, ".mcp.json"), smoke=True
        ),
        "hash_mismatch": Expect(1, (TRUST_BLOCK + "policy hash mismatch",), absent=(HOOK,)),
        "absent/block": Expect(
            1,
            (TRUST_BLOCK + r"no enforceable org policy was resolved \(outcome=absent\)",),
            absent=(HOOK, ".mcp.json"),
        ),
        "empty/block": Expect(
            1,
            (TRUST_BLOCK + r"no enforceable org policy was resolved \(outcome=empty\)",),
            absent=(HOOK, ".mcp.json"),
        ),
        "no_git_remote/block": Expect(
            1,
            (TRUST_BLOCK + r"no enforceable org policy was resolved \(outcome=no_git_remote\)",),
            absent=(HOOK, ".mcp.json"),
        ),
        "malformed/block": Expect(
            1,
            (TRUST_BLOCK + r"no enforceable org policy was resolved \(outcome=malformed\)",),
            absent=(HOOK, ".mcp.json"),
        ),
        "garbage_response/block": Expect(
            1,
            (TRUST_BLOCK + r"no enforceable org policy was resolved \(outcome=garbage_response\)",),
            absent=(HOOK, ".mcp.json"),
        ),
        "incomplete_chain": Expect(
            1,
            (TRUST_BLOCK + r"org policy inheritance chain is incomplete",),
            absent=(HOOK, ".mcp.json"),
        ),
        "cached_stale/org-block": Expect(
            1,
            (TRUST_BLOCK + r"org policy refresh failed and the cached policy",),
            absent=(HOOK, ".mcp.json"),
        ),
        "found": Expect(0, (_POLICY_DENY_PKG,), absent=(HOOK,)),
        "cache=cold": Expect(0, (_POLICY_DENY_PKG,), absent=(HOOK,)),
        "disabled": Expect(
            0, (r"Policy enforcement disabled by APM_POLICY_DISABLE=1",), absent=(HOOK,)
        ),
        "absent/warn": Expect(0, (), absent=(HOOK,)),
        "empty/warn": Expect(0, (EMPTY,), absent=(HOOK,)),
        "no_git_remote/warn": Expect(0, (), absent=(HOOK,)),
        "malformed/warn": Expect(0, (r"is malformed",), absent=(HOOK,)),
        "cache_miss_fetch_fail/warn": Expect(0, (UNREACHABLE,), absent=(HOOK,)),
        "garbage_response/warn": Expect(0, (r"is not valid YAML",), absent=(HOOK,)),
        "cached_stale/warn": Expect(0, (r"\(cached, fetched 2h ago\)",), absent=(HOOK,)),
        "cache=warm-fresh": Expect(0, (r"\(cached, fetched \d+s ago\)",), absent=(HOOK,)),
        "cache=stale>7d-or-expired": Expect(0, (UNREACHABLE,), absent=(HOOK,)),
        "cache=stale<7d": Expect(0, (r"(?i)stale",), absent=(HOOK,), xfail=_OFFLINE),
    },
)

# install/exec_gate.py ReplayExecAllowMap -- audit's drift replay.
SITE_SLICES["audit-exec-replay(getattr)"] = SiteSlice(
    arrange=_installed_parked_exec,
    command=lambda world: "apm audit",
    classes={
        "hash_mismatch": Expect(
            0,
            (r"drift replay blocked: Blocked by org policy: policy hash mismatch",),
            unchanged=("*",),
            smoke=True,
        ),
        "absent/block": _replay_blocked("no enforceable"),
        "empty/block": _replay_blocked("no enforceable"),
        "no_git_remote/block": _replay_blocked("no enforceable"),
        "malformed/block": _replay_blocked("no enforceable"),
        "cache_miss_fetch_fail/block": _replay_blocked("no enforceable"),
        "garbage_response/block": _replay_blocked("no enforceable"),
        "incomplete_chain": _replay_blocked("org policy inheritance chain is incomplete"),
        "cached_stale/org-block": _replay_blocked("org policy refresh failed"),
        "found": _replay_ran(),
        "disabled": _replay_ran(),
        "absent/warn": _replay_ran(),
        "empty/warn": _replay_ran(),
        "no_git_remote/warn": _replay_ran(),
        "malformed/warn": _replay_ran(),
        "cache_miss_fetch_fail/warn": _replay_ran(),
        "garbage_response/warn": _replay_ran(),
        "cached_stale/warn": _replay_ran(),
        "cache=stale>7d-or-expired": _replay_ran(),
        "cache=cold": dataclasses.replace(_replay_ran(), policy_fetch=True),
        "cache=warm-fresh": dataclasses.replace(_replay_ran(), policy_fetch=False),
        "cache=stale<7d": _replay_ran(),
    },
)

# security/executables.py resolve_project_exec_allow_map -- uninstall's survivor rebuild.
SITE_SLICES["exec-map(getattr: uninstall/hook rebuild/lsp)"] = SiteSlice(
    arrange=_installed_parked_exec,
    command=lambda world: "apm uninstall p-skill",
    classes={
        "hash_mismatch": Expect(
            1,
            (TRUST_BLOCK + "policy hash mismatch",),
            absent=(HOOK,),
            xfail=_PARTIAL_UNINSTALL,
        ),
        "absent/block": Expect(1, (TRUST_BLOCK,), absent=(HOOK,), xfail=_PARTIAL_UNINSTALL),
        "empty/block": Expect(1, (TRUST_BLOCK,), absent=(HOOK,), xfail=_PARTIAL_UNINSTALL),
        "no_git_remote/block": Expect(1, (TRUST_BLOCK,), absent=(HOOK,), xfail=_PARTIAL_UNINSTALL),
        "malformed/block": Expect(1, (TRUST_BLOCK,), absent=(HOOK,), xfail=_PARTIAL_UNINSTALL),
        "cache_miss_fetch_fail/block": Expect(
            1, (TRUST_BLOCK,), absent=(HOOK,), xfail=_PARTIAL_UNINSTALL
        ),
        "garbage_response/block": Expect(
            1, (TRUST_BLOCK,), absent=(HOOK,), xfail=_PARTIAL_UNINSTALL
        ),
        "incomplete_chain": Expect(1, (TRUST_BLOCK,), absent=(HOOK,), xfail=_PARTIAL_UNINSTALL),
        "cached_stale/org-block": Expect(
            1, (TRUST_BLOCK,), absent=(HOOK,), xfail=_PARTIAL_UNINSTALL
        ),
        "found": Expect(0, (), absent=(HOOK, SKILL)),
        "disabled": Expect(0, (), absent=(HOOK, SKILL)),
        "absent/warn": Expect(0, (), absent=(HOOK, SKILL)),
        "no_git_remote/warn": Expect(0, (), absent=(HOOK, SKILL)),
        "cache=warm-fresh": Expect(0, (), absent=(HOOK, SKILL)),
        "cache=cold": Expect(0, (), absent=(HOOK, SKILL)),
        "empty/warn": Expect(0, (_not_applied("empty"),), absent=(HOOK, SKILL)),
        "malformed/warn": Expect(0, (_not_applied("malformed"),), absent=(HOOK, SKILL)),
        "cache_miss_fetch_fail/warn": Expect(
            0, (_not_applied("cache_miss_fetch_fail"),), absent=(HOOK, SKILL)
        ),
        "garbage_response/warn": Expect(
            0, (_not_applied("garbage_response"),), absent=(HOOK, SKILL)
        ),
        "cached_stale/warn": Expect(0, (r"Using stale cached org policy",), absent=(HOOK, SKILL)),
        "cache=stale>7d-or-expired": Expect(
            0, (_not_applied("cache_miss_fetch_fail"),), absent=(HOOK, SKILL)
        ),
        "cache=stale<7d": Expect(0, (r"Using stale cached org policy",), xfail=_OFFLINE),
    },
)


# policy/ci_checks.py -- ``apm audit --ci`` policy checks.
_CI_FAILED = r"\(policy\.fetch_failure_default=block\)"
SITE_SLICES["audit-ci"] = SiteSlice(
    arrange=_installed_parked_exec,
    command=lambda world: "apm audit --ci",
    classes={
        "cache_miss_fetch_fail/block": Expect(1, (r"Policy fetch failed: HTTP 503", _CI_FAILED)),
        "cache_miss_fetch_fail/warn": Expect(0, (r"enforcement skipped",)),
        "hash_mismatch": Expect(1, (HASH,)),
        "absent/block": Expect(1, (r"No org policy found", _CI_FAILED)),
        "absent/warn": Expect(0),
        "no_git_remote/block": Expect(1, (r"Could not determine org from git remote", _CI_FAILED)),
        "no_git_remote/warn": Expect(0),
        "empty/block": Expect(1, (EMPTY, _CI_FAILED)),
        "empty/warn": Expect(0),
        "found": Expect(1, (_CHECK_FAILED,)),
        "disabled": Expect(0),
        "malformed/block": Expect(1, (r"Policy fetch failed: Invalid policy", _CI_FAILED)),
        "malformed/warn": Expect(0, (_unavailable("malformed"),)),
        "garbage_response/block": Expect(1, (r"is not a YAML mapping", _CI_FAILED)),
        "garbage_response/warn": Expect(0, (_unavailable("garbage_response"),)),
        "incomplete_chain": Expect(1, (r"Policy fetch failed: Policy chain incomplete",)),
        "cached_stale/org-block": Expect(1, (_STALE_BLOCK,)),
        "cached_stale/warn": Expect(1, (_CHECK_FAILED,)),
        "cache=cold": Expect(1, (_CHECK_FAILED,), policy_fetch=True),
        "cache=warm-fresh": Expect(1, (_CHECK_FAILED,), policy_fetch=False),
        "cache=stale<7d": Expect(1, (_CHECK_FAILED,), xfail=_OFFLINE),
        "cache=stale>7d-or-expired": Expect(0, (r"enforcement skipped",)),
    },
)

# commands/approve.py -- approval loads the org policy best-effort.
_APPROVE_UNRESOLVED = r"Org policy could not be resolved; approval is proceeding"
_APPROVE_TWICE = "#4178 approve prints its org policy warning twice"
SITE_SLICES["approve"] = SiteSlice(
    arrange=_installed_parked_exec,
    command=lambda world: "apm approve p-exec",
    classes={
        "cache_miss_fetch_fail/block": Expect(0, (_APPROVE_UNRESOLVED,), xfail=_APPROVE_TWICE),
        "cache_miss_fetch_fail/warn": Expect(0, (_APPROVE_UNRESOLVED,), xfail=_APPROVE_TWICE),
        "hash_mismatch": Expect(0, (r"Policy hash verification failed",), xfail=_APPROVE_TWICE),
        "absent/block": Expect(0, forbids=(_APPROVE_UNRESOLVED,)),
        "absent/warn": Expect(0, forbids=(_APPROVE_UNRESOLVED,)),
        "no_git_remote/block": Expect(0, forbids=(_APPROVE_UNRESOLVED,)),
        "no_git_remote/warn": Expect(0, forbids=(_APPROVE_UNRESOLVED,)),
        "empty/block": Expect(0, forbids=(_APPROVE_UNRESOLVED,)),
        "empty/warn": Expect(0, forbids=(_APPROVE_UNRESOLVED,)),
        "found": Expect(0, forbids=(_APPROVE_UNRESOLVED,)),
        "disabled": Expect(0, forbids=(_APPROVE_UNRESOLVED,)),
        "malformed/block": Expect(0, (_APPROVE_UNRESOLVED,), xfail=_APPROVE_TWICE),
        "malformed/warn": Expect(0, (_APPROVE_UNRESOLVED,), xfail=_APPROVE_TWICE),
        "garbage_response/block": Expect(0, (_APPROVE_UNRESOLVED,), xfail=_APPROVE_TWICE),
        "garbage_response/warn": Expect(0, (_APPROVE_UNRESOLVED,), xfail=_APPROVE_TWICE),
        "incomplete_chain": Expect(0, (_APPROVE_UNRESOLVED,), xfail=_APPROVE_TWICE),
        "cached_stale/org-block": Expect(0, forbids=(_APPROVE_UNRESOLVED,)),
        "cached_stale/warn": Expect(0, forbids=(_APPROVE_UNRESOLVED,)),
        "cache=cold": Expect(0, forbids=(_APPROVE_UNRESOLVED,), policy_fetch=True),
        "cache=warm-fresh": Expect(0, forbids=(_APPROVE_UNRESOLVED,), policy_fetch=False),
        "cache=stale<7d": Expect(0, forbids=(_APPROVE_UNRESOLVED,), xfail=_OFFLINE),
        "cache=stale>7d-or-expired": Expect(0, (_APPROVE_UNRESOLVED,), xfail=_APPROVE_TWICE),
    },
)


def _status(outcome: str) -> Expect:
    return Expect(0, (rf"Outcome\s*\|\s*{outcome}\s",), unchanged=("*",))


# commands/policy.py -- ``apm policy status`` reports the routed outcome.
SITE_SLICES["policy-status"] = SiteSlice(
    arrange=_nothing,
    command=lambda world: "apm policy status",
    classes={
        "cache_miss_fetch_fail/block": _status("cache_miss_fetch_fail"),
        "cache_miss_fetch_fail/warn": _status("cache_miss_fetch_fail"),
        "hash_mismatch": _status("hash_mismatch"),
        "absent/block": _status("absent"),
        "absent/warn": _status("absent"),
        "no_git_remote/block": _status("no_git_remote"),
        "no_git_remote/warn": _status("no_git_remote"),
        "empty/block": _status("empty"),
        "empty/warn": _status("empty"),
        "found": _status("found"),
        "disabled": _status("disabled"),
        "malformed/block": _status("malformed"),
        "malformed/warn": _status("malformed"),
        "garbage_response/block": _status("garbage_response"),
        "garbage_response/warn": _status("garbage_response"),
        "incomplete_chain": _status("incomplete_chain"),
        "cached_stale/org-block": _status("cached_stale"),
        "cached_stale/warn": _status("cached_stale"),
        "cache=cold": _status("found"),
        "cache=warm-fresh": _status("found"),
        "cache=stale<7d": Expect(
            0, (r"Outcome\s*\|\s*cached_stale\s",), unchanged=("*",), xfail=_OFFLINE
        ),
        "cache=stale>7d-or-expired": _status("cache_miss_fetch_fail"),
    },
)

# policy/install_preflight.py dry-run path -- a preview never fails closed.
SITE_SLICES["install-dry-run-preflight"] = SiteSlice(
    arrange=_nothing,
    command=lambda world: "apm install --dry-run p-skill p-exec",
    classes={
        "cache_miss_fetch_fail/block": Expect(
            0,
            (KNOB_BLOCK,),
            unchanged=("*",),
            xfail="S-pending (neutral issue to follow): dry-run preview of policy outcome",
        ),
        "cache_miss_fetch_fail/warn": Expect(0, (UNREACHABLE,), unchanged=("*",)),
        "cache=cold": Expect(0, (DENIED,), unchanged=("*",), policy_fetch=True),
        "cache=warm-fresh": Expect(0, (DENIED,), unchanged=("*",), policy_fetch=False),
        "cache=stale<7d": Expect(0, (DENIED,), unchanged=("*",), xfail=_OFFLINE),
        "cache=stale>7d-or-expired": Expect(0, (UNREACHABLE,), unchanged=("*",)),
    },
)

# commands/update.py -- the re-gate short circuit of an update with nothing to change.
_SHORTCIRCUIT = (
    "S-pending (neutral issue to follow): update and install exit agreement under policy"
)
SITE_SLICES["update-regate-shortcircuit"] = SiteSlice(
    arrange=_installed_parked_exec,
    command=lambda world: "apm update",
    classes={
        "cache_miss_fetch_fail/block": Expect(1, (KNOB_BLOCK,), present=(SKILL,)),
        "cache_miss_fetch_fail/warn": Expect(0, (UNREACHABLE,), present=(SKILL,)),
        "hash_mismatch": Expect(1, (HASH,), present=(SKILL,)),
        "absent/block": Expect(1, (KNOB_BLOCK,), xfail=_SHORTCIRCUIT),
        "absent/warn": Expect(0, present=(SKILL,)),
        "no_git_remote/block": Expect(1, (KNOB_BLOCK,), xfail=_SHORTCIRCUIT),
        "no_git_remote/warn": Expect(0, present=(SKILL,)),
        "empty/block": Expect(1, (KNOB_BLOCK,), xfail=_SHORTCIRCUIT),
        "empty/warn": Expect(0, present=(SKILL,)),
        "found": Expect(1, (DENIED,), xfail=_SHORTCIRCUIT),
        "disabled": Expect(0, present=(SKILL,)),
        "malformed/block": Expect(1, (KNOB_BLOCK,), present=(SKILL,)),
        "malformed/warn": Expect(0, (r"is malformed",), present=(SKILL,)),
        "garbage_response/block": Expect(1, (KNOB_BLOCK,), present=(SKILL,)),
        "garbage_response/warn": Expect(0, (r"is not valid YAML",), present=(SKILL,)),
        "incomplete_chain": Expect(1, (r"inheritance chain is incomplete",), present=(SKILL,)),
        "cached_stale/org-block": Expect(1, (r"fetch_failure=block",), xfail=_SHORTCIRCUIT),
        "cached_stale/warn": Expect(1, (DENIED,), xfail=_SHORTCIRCUIT),
        "cache=cold": Expect(1, (DENIED,), xfail=_SHORTCIRCUIT),
        "cache=warm-fresh": Expect(1, (DENIED,), xfail=_SHORTCIRCUIT),
        "cache=stale<7d": Expect(1, (DENIED,), xfail=_OFFLINE),
        "cache=stale>7d-or-expired": Expect(0, (UNREACHABLE,), present=(SKILL,)),
    },
)


def _installed_approved_exec(world: World, replay: RowReplay) -> None:
    """p-skill and an approved p-exec deployed (hook and MCP server), under ``allow``."""
    use_policy(world, "allow")
    replay.step("apm install p-skill p-exec")
    replay.step("apm approve p-exec")
    replay.step("apm install")


# security/trust.py McpAdmission -- audit's MCP consent check admits or refuses a
# deployed server against the org policy it resolves itself.
SITE_SLICES["mcp-admission(trust.py)"] = SiteSlice(
    arrange=_installed_approved_exec,
    command=lambda world: "apm audit --ci",
    classes={
        "absent/block": Expect(1, (r"No org policy found", _CI_FAILED)),
        "empty/block": Expect(1, (EMPTY, _CI_FAILED)),
        "no_git_remote/block": Expect(1, (r"Could not determine org from git remote", _CI_FAILED)),
        "malformed/block": Expect(1, (r"Invalid policy", _CI_FAILED)),
        "cache_miss_fetch_fail/block": Expect(1, (r"HTTP 503", _CI_FAILED)),
        "garbage_response/block": Expect(1, (r"is not a YAML mapping", _CI_FAILED)),
        "incomplete_chain": Expect(1, (r"Policy chain incomplete",)),
        "malformed/warn": Expect(0, (_unavailable("malformed"), _CONSENT_OK)),
        "cache_miss_fetch_fail/warn": Expect(
            0, (_unavailable("cache_miss_fetch_fail"), _CONSENT_OK)
        ),
        "garbage_response/warn": Expect(0, (_unavailable("garbage_response"), _CONSENT_OK)),
        "absent/warn": Expect(0, (_CONSENT_OK,), forbids=(_UNAVAILABLE,)),
        "empty/warn": Expect(0, (_CONSENT_OK,), forbids=(_UNAVAILABLE,)),
        "no_git_remote/warn": Expect(0, (_CONSENT_OK,), forbids=(_UNAVAILABLE,)),
        "disabled": Expect(0, (_CONSENT_OK,), forbids=(_UNAVAILABLE,)),
        "found": Expect(1, (_CONSENT_OK, _CHECK_FAILED), forbids=(_UNAVAILABLE,)),
        "cached_stale/org-block": Expect(1, (_STALE_BLOCK,)),
        "cached_stale/warn": Expect(1, (_CONSENT_OK, _CHECK_FAILED), forbids=(_UNAVAILABLE,)),
        "cache=cold": Expect(
            1, (_CONSENT_OK, _CHECK_FAILED), forbids=(_UNAVAILABLE,), policy_fetch=True
        ),
        "cache=warm-fresh": Expect(
            1, (_CONSENT_OK, _CHECK_FAILED), forbids=(_UNAVAILABLE,), policy_fetch=False
        ),
        "cache=stale<7d": Expect(1, (_CHECK_FAILED,), forbids=(_UNAVAILABLE,), xfail=_OFFLINE),
        "cache=stale>7d-or-expired": Expect(0, (_unavailable("cache_miss_fetch_fail"),)),
        "hash_mismatch": Expect(1, (HASH,)),
    },
)

# install/local_bundle_handler.py preflight (cache_only) -- a local bundle install.
SITE_SLICES["bundle-install-preflight(cache_only)"] = SiteSlice(
    arrange=_nothing,
    command=lambda world: f"apm install {world.packages['p-exec:bundle'].spec}",
    classes={
        "cache_miss_fetch_fail/block": Expect(
            1, (TRUST_BLOCK + "no enforceable", KNOB_BLOCK), absent=(HOOK, ".mcp.json")
        ),
        "cache_miss_fetch_fail/warn": Expect(0, (UNREACHABLE,), absent=(HOOK,)),
        "cache=cold": Expect(0, (_POLICY_DENY_PKG,), absent=(HOOK,), policy_fetch=True),
        "cache=warm-fresh": Expect(
            0, (r"\(cached, fetched \d+s ago\)",), absent=(HOOK,), policy_fetch=False
        ),
        "cache=stale<7d": Expect(0, (r"(?i)stale",), absent=(HOOK,), xfail=_OFFLINE),
        "cache=stale>7d-or-expired": Expect(0, (UNREACHABLE,), absent=(HOOK,)),
    },
)


def _site_rows():
    for site, slice_ in SITE_SLICES.items():
        for cls, expect in slice_.classes.items():
            cell_id = f"P|{site}|{cls}"
            marks = []
            if expect.smoke:
                marks.append(pytest.mark.lifecycle_smoke)
            if expect.xfail:
                marks.append(pytest.mark.xfail(strict=True, reason=expect.xfail))
            yield pytest.param(cell_id, id=cell_id, marks=marks)


def _replay(world: World) -> RowReplay:
    row = Row(
        id="T7b/policy-outcome",
        promises=("P-OWN-trust-policy-outcome",),
        packages=("p-skill", "p-exec"),
        targets=("copilot",),
        scope="project",
        given=(),
        when=(),
    )
    return RowReplay(world, row)


def run_site_row(world: World, cell_id: str) -> None:
    """Arrange, apply the class, run the site command, check I1-I9 and the obligation."""
    _, site, cls = cell_id.split("|", 2)
    slice_ = SITE_SLICES[site]
    expect = slice_.classes[cls]
    replay = _replay(world)
    slice_.arrange(world, replay)
    (expect.setup or CLASS_SETUPS[cls])(world, replay)
    # The org policy input changed, so the converged state no longer holds.
    replay.model = dataclasses.replace(replay.model, converged=False)
    before = replay.observe()
    world.services.reset()
    with pytest.MonkeyPatch.context() as patch:
        if expect.home_writes:
            allowed = (*rows._PROJECT_HOME_ALLOWED, *expect.home_writes)
            patch.setattr(rows, "_PROJECT_HOME_ALLOWED", allowed)
        command = (expect.command or slice_.command)(world)
        replay.step({"run": command, "exit": expect.exit})
    after = replay.observe()
    egress = tuple(world.services.egress_log())
    requested = any(entry["path"].endswith(POLICY_CONTENTS) for entry in egress)
    served = any(entry["path"].endswith(SERVED_POLICY_CONTENTS) for entry in egress)
    if expect.policy_fetch is True:
        assert requested, egress
    elif expect.policy_fetch is False:
        assert not served, egress
    oracles.check_obligation(
        cell_id,
        strength="e2e",
        before=before,
        after=after,
        result=replay.last,
        evidence=oracles.Evidence(
            exit=expect.exit,
            output_matches=expect.output,
            output_forbids=expect.forbids,
            absent={"project": expect.absent} if expect.absent else {},
            present={"project": expect.present} if expect.present else {},
            unchanged={"project": expect.unchanged} if expect.unchanged else {},
            egress=egress,
            egress_present_hosts=(API_HOST,) if expect.policy_fetch else (),
        ),
    )


@pytest.mark.parametrize("cell_id", list(_site_rows()))
def test_site_routes_the_outcome(lifecycle_world: World, cell_id: str) -> None:
    run_site_row(lifecycle_world, cell_id)


@pytest.mark.xfail(
    strict=True, reason="#4180 a blocked install also prints that it proceeds without enforcement"
)
def test_blocked_install_never_claims_to_proceed(lifecycle_world: World) -> None:
    replay = _replay(lifecycle_world)
    CLASS_SETUPS["cache_miss_fetch_fail/block"](lifecycle_world, replay)
    replay.step({"run": "apm install p-skill", "exit": 1})
    assert replay.last is not None
    assert "proceeding without policy enforcement" not in replay.last.stdout


def test_every_row_is_a_policy_obligation() -> None:
    known = security_obligations()["P"]
    rows = {cell: "unit" for cell in OWNER_ROWS}
    rows.update({f"P|{site}|{cls}": "e2e" for site, s in SITE_SLICES.items() for cls in s.classes})
    unknown = sorted(set(rows) - set(known))
    assert not unknown, f"rows with no P obligation id: {unknown}"
    wrong = sorted(cell for cell, strength in rows.items() if known[cell] != strength)
    assert not wrong, f"rows whose strength differs from the obligation: {wrong}"
    assert set(CLASS_SETUPS) >= {cls for s in SITE_SLICES.values() for cls in s.classes}
