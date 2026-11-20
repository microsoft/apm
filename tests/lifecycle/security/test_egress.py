"""Egress: a credential goes only to the host it belongs to (#4109).

PROMISE: P-LAW-auth.credentials_isolated -- credentials remain scoped to
their intended host and never enter persisted URLs or output (source:
tests/fixtures/lifecycle_bug_ledger.json); tokens are resolved per host at
run time (docs/src/content/docs/getting-started/authentication.md).

FRAMEWORK. One parametrized test runs every row of every
``egress_slices/<host>.py``. A row id is the exact generated obligation (E
space of ``tests/lifecycle/obligation_space.py``). The shared machinery here:

- ``CREDENTIALS``: how a credential kind is put in play with a sentinel
  value (PAT: every platform PAT variable, each with its own sentinel; gh: a
  ``gh`` shim on PATH; helper: a git credential helper that answers only for
  the row's host; none: nothing);
- ``OPERATIONS``: the egress operation (setup commands, the decisive
  command, and which requests of it belong to the operation).

Every command runs through the driver (global output oracle: I6, I8, I9)
behind the world's recording proxy. The row then checks, from the proxy's
request log of the decisive command: the operation reached its host; the
row's own credential reached only the host's own origins (and reached one of
them when ``presented``); no other platform's credential reached any host;
and ``check_obligation`` credits the id with the credential-carrying egress,
its allowed and forbidden hosts and a scan of every file under the project,
HOME and cache for the sentinels.

Unregistered, missing world support: every ``az-bearer`` id and the ADO
``helper`` ids (the world's Azure DevOps host answers anonymous requests, so
APM never reaches those chain steps); ``helper`` on the generic host (no
generic git host is served); ``registry`` egress (no APM package registry
service); the E1 and EP rows with REDIRECT=same-origin or cross-origin and
PROXY_ONLY=1 (the world proxy cannot yet script a redirect or an unproxied
route). A row whose id carries PROXY_ONLY=0 also proves the proxy refused no
connection; REDIRECT=none proves no response of the step redirected.
"""

from __future__ import annotations

import base64
import binascii
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from tests.lifecycle import driver
from tests.lifecycle.obligation_space import security_obligations
from tests.lifecycle.oracles import Evidence, check_obligation
from tests.lifecycle.security.egress_slices import EgressRow, HostSlice, slices
from tests.lifecycle.world import World
from tests.lifecycle.world.services import POLICY_REPO, REGISTRY_SERVER, policy_org
from tests.utils.apm_lifecycle_runner import CommandResult
from tests.utils.artifact_snapshot import ArtifactSnapshotSet
from tests.utils.local_ado_org_fixture import install_logging_credential_helper
from tests.utils.local_scripted_https_proxy import ScriptedRequest

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.promise("P-LAW-auth.credentials_isolated", "P-INV-I6", "P-INV-I8"),
]

# Recognizable sentinels, shaped like the real credential of each kind.
PATS: Mapping[str, str] = {
    "GITHUB_APM_PAT": "ghp_" + "LifecycleEgressGithubPat".ljust(36, "0"),
    "GITLAB_APM_PAT": "glpat-" + "LifecycleEgressGitlabPat".ljust(20, "1"),
    "ADO_APM_PAT": "lifecycleegressadopat".ljust(52, "q"),
}
GH_TOKEN = "gho_" + "LifecycleEgressGhCli".ljust(36, "2")
HELPER_PASSWORD = "lifecycle-egress-helper-sentinel-7c2d"


# -- credential arrangements -------------------------------------------------


@dataclass(frozen=True)
class Arranged:
    world: World
    own: tuple[str, ...]  # sentinels that may reach the row host's family
    foreign: tuple[str, ...]  # sentinels that must reach no host


def _with_env(world: World, **values: str) -> World:
    return replace(world, env={**world.env, **values})


def _pat(world: World, host: HostSlice) -> Arranged:
    world = _with_env(world, **PATS)
    own = tuple(PATS[name] for name in (host.pat_env,) if name)
    return Arranged(world, own, tuple(v for k, v in PATS.items() if k != host.pat_env))


def _gh(world: World, host: HostSlice) -> Arranged:
    """A ``gh`` on PATH that is logged in and prints its token for any host."""
    directory = world.root / "gh-shim"
    directory.mkdir()
    shim = directory / "gh"
    shim.write_text(
        f"#!{sys.executable}\nimport sys\n"
        f"if sys.argv[1:3] == ['auth', 'token']:\n    print({GH_TOKEN!r})\n"
        "    raise SystemExit(0)\nraise SystemExit(1)\n",
        encoding="ascii",
    )
    shim.chmod(0o755)
    world = _with_env(world, PATH=f"{directory}:{world.env['PATH']}")
    return Arranged(world, (GH_TOKEN,), ())


def _helper(world: World, host: HostSlice) -> Arranged:
    """A git credential helper that answers only for the row's host."""
    install_logging_credential_helper(
        Path(world.env["GIT_CONFIG_GLOBAL"]),
        world.root / "credential-helper",
        username="lifecycle-helper",
        password=HELPER_PASSWORD,
        hosts=(host.git_host,),
    )
    return Arranged(world, (HELPER_PASSWORD,), ())


def _none(world: World, host: HostSlice) -> Arranged:
    return Arranged(world, (), ())


CREDENTIALS: Mapping[str, Callable[[World, HostSlice], Arranged]] = {
    "PAT": _pat,
    "gh": _gh,
    "helper": _helper,
    "none": _none,
}


# -- egress operations -------------------------------------------------------


def _set_remote(world: World, host: HostSlice, org: str) -> None:
    """Point the project's origin at *org* on the row's host (policy discovery)."""
    git = ("git", "-C", str(world.project))
    subprocess.run((*git, "init", "-q", "--initial-branch=main"), env=world.env, check=True)
    remote = f"https://{host.git_host}/{org}/consumer.git"
    subprocess.run((*git, "remote", "add", "origin", remote), env=world.env, check=True)


def _is_git(request: ScriptedRequest) -> bool:
    return request.path.endswith(("/info/refs", "/git-upload-pack", "/HEAD"))


def _is_api(request: ScriptedRequest) -> bool:
    return not _is_git(request)


@dataclass(frozen=True)
class Operation:
    """One egress operation: setup commands, the decisive command, its requests."""

    command: Callable[[World, HostSlice], list[str]]
    belongs: Callable[[ScriptedRequest], bool] = lambda request: True
    setup: Callable[[World, HostSlice], None] = lambda world, host: None


OPERATIONS: Mapping[str, Operation] = {
    "clone": Operation(lambda w, h: ["install", h.spec], _is_git),
    "api-probe": Operation(lambda w, h: ["install", h.spec], _is_api),
    "marketplace-fetch": Operation(lambda w, h: ["marketplace", "add", f"{h.base}/marketplace"]),
    "policy-fetch": Operation(
        lambda w, h: ["policy", "status"],
        lambda request: (
            request.path.endswith(f"/{POLICY_REPO}/contents/apm-policy.yml")
            or "/contents/apm-policy.yml" in request.path
        ),
        lambda w, h: _set_remote(w, h, policy_org("allow")),
    ),
    "mcp-registry": Operation(lambda w, h: ["install", "--mcp", REGISTRY_SERVER]),
}


# -- rows ----------------------------------------------------------------------


def _carried(request: ScriptedRequest, secrets: tuple[str, ...]) -> bool:
    """True when any header of *request* carries a sentinel, raw or in Basic auth."""
    for value in request.header_values():
        if any(secret in value for secret in secrets):
            return True
        scheme, _, token = value.partition(" ")
        if scheme.lower() == "basic":
            try:
                decoded = base64.b64decode(token, validate=True).decode("utf-8", "replace")
            except (binascii.Error, ValueError):
                continue
            if any(secret in decoded for secret in secrets):
                return True
    return False


def _authorized(request: ScriptedRequest) -> bool:
    return any(name.lower() == "authorization" for name, _ in request.headers)


def _entry(request: ScriptedRequest) -> dict[str, object]:
    return {"host": request.host, "method": request.method, "path": request.path}


def _fields(cell_id: str) -> dict[str, str]:
    """The factor values of an E id: "EP|CRED=PAT|HOST=github" -> {CRED: PAT, HOST: github}."""
    return dict(part.split("=", 1) for part in cell_id.split("|")[1:])


def registered() -> list[tuple[HostSlice, EgressRow]]:
    return [(host, row) for host in slices() for row in host.rows]


def run_egress_row(world: World, host: HostSlice, row: EgressRow) -> None:
    arranged = CREDENTIALS[row.cred](_with_env(world, **host.env), host)
    world = arranged.world
    operation = OPERATIONS[row.egress]
    operation.setup(world, host)
    roots = {
        "project": world.project,
        "home": world.home,
        "cache": world.cache_root or world.root / "cache",
    }
    world.services.reset()
    before = ArtifactSnapshotSet.capture(roots)
    result: CommandResult = driver.run(world, operation.command(world, host))
    after = ArtifactSnapshotSet.capture(roots)
    assert result.returncode == 0, f"{row.egress} failed:\n{result.stdout}\n{result.stderr}"
    requests = [
        request
        for request in world.services.proxy.observations
        if request.method != "CONNECT" and operation.belongs(request)
    ]
    reached = {request.host for request in requests}
    assert host.git_host in reached or reached & set(host.family), (
        f"{row.egress} never reached {host.host}: {sorted(reached)}"
    )
    fields = _fields(row.id)
    everything = world.services.proxy.observations
    if fields.get("PROXY_ONLY") == "0":
        # Normal mode: every connection went to an origin the world's proxy serves.
        refused = sorted({r.host for r in everything if r.method == "CONNECT"})
        assert not refused, f"connections the proxy refused: {refused}"
    if fields.get("REDIRECT") == "none":
        redirected = sorted({(r.host, r.path) for r in everything if 300 <= r.status < 400})
        assert not redirected, f"redirects in a no-redirect row: {redirected}"
    foreign = sorted({r.host for r in requests if _carried(r, arranged.foreign)})
    assert not foreign, f"another platform's credential reached {foreign}"
    if arranged.own:
        carrying = [request for request in requests if _carried(request, arranged.own)]
    else:
        carrying = [request for request in requests if _authorized(request)]
    others = sorted(reached - set(host.family))
    # In play: the own credential reached every own origin the operation used.
    present = sorted(reached & set(host.family)) if row.presented and arranged.own else []
    secrets = (*arranged.own, *arranged.foreign)
    check_obligation(
        row.id,
        strength=security_obligations()["E"][row.id],
        before=before,
        after=after,
        result=result,
        evidence=Evidence(
            egress=tuple(_entry(request) for request in carrying),
            egress_present_hosts=present,
            egress_absent_hosts=(*others, *(() if arranged.own else host.family)),
            secrets_absent=secrets,
            exit=0,
        ),
    )


def _param(host: HostSlice, row: EgressRow):
    marks = []
    if row.smoke:
        marks.append(pytest.mark.lifecycle_smoke)
    if row.xfail:
        marks.append(pytest.mark.xfail(strict=True, raises=AssertionError, reason=row.xfail))
    return pytest.param(host, row, id=row.id, marks=marks)


def test_every_egress_row_is_an_e_obligation() -> None:
    space = security_obligations()["E"]
    ids = [row.id for _, row in registered()]
    assert len(ids) == len(set(ids)), "an E id is registered twice"
    assert set(ids) <= set(space), sorted(set(ids) - set(space))
    for host, row in registered():
        fields = _fields(row.id)
        assert fields.get("HOST", host.host) == host.host, row.id
        assert fields.get("CRED", row.cred) == row.cred, row.id
        assert fields.get("EGRESS", row.egress) == row.egress, row.id
        assert row.cred in CREDENTIALS and row.egress in OPERATIONS, row.id


@pytest.mark.parametrize(("host", "row"), [_param(*item) for item in registered()])
def test_credential_reaches_only_its_host(lifecycle_world: World, host, row) -> None:
    run_egress_row(lifecycle_world, host, row)
