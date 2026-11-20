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

Unregistered, missing world support: ``helper`` on the generic host (no generic git host is
served); the E1 and EP rows with REDIRECT=same-origin or cross-origin and
PROXY_ONLY=1 (the world proxy cannot yet script a redirect or an unproxied
route). A row whose id carries PROXY_ONLY=0 also proves the proxy refused no
connection; REDIRECT=none proves no response of the step redirected. Rows
with ``auth`` make Azure DevOps refuse anonymous requests first.
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
from tests.lifecycle.world import (
    World,
    login_azure,
    routes,
    use_policy,
    use_registry,
    use_registry_proxy,
)
from tests.lifecycle.world import registry as world_registry
from tests.lifecycle.world.services import (
    ADO_BEARER,
    ADO_PAT,
    POLICY_REPO,
    REGISTRY_HOST,
    REGISTRY_SERVER,
    policy_org,
)
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
    # The world's fake ADO PAT: dev.azure.com accepts it when it requires auth.
    "ADO_APM_PAT": ADO_PAT,
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
    # A host whose PAT is not a platform PAT (the package registry token) keeps the world's.
    own = tuple(PATS.get(name) or world.env.get(name, "") for name in (host.pat_env,) if name)
    return Arranged(
        world, tuple(v for v in own if v), tuple(v for k, v in PATS.items() if k != host.pat_env)
    )


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
    password = host.helper_password or HELPER_PASSWORD
    install_logging_credential_helper(
        Path(world.env["GIT_CONFIG_GLOBAL"]),
        world.root / "credential-helper",
        username="lifecycle-helper",
        password=password,
        hosts=(host.git_host,),
    )
    return Arranged(world, (password,), ())


def _none(world: World, host: HostSlice) -> Arranged:
    """No credential: also withhold one the world itself provides (the registry token)."""
    if host.pat_env and host.pat_env in world.env:
        world = replace(world, env={k: v for k, v in world.env.items() if k != host.pat_env})
    return Arranged(world, (), ())


def _az_bearer(world: World, host: HostSlice) -> Arranged:
    """``az`` logged in: APM's Azure DevOps chain falls back to its bearer (no PAT set).

    The Azure CLI's own token store lives under the world root, outside the
    scanned roots: only a copy APM itself writes counts as persisting it.
    """
    world = _with_env(world, AZURE_CONFIG_DIR=str(world.root / "azure-cli"))
    login_azure(world)
    return Arranged(world, (ADO_BEARER,), tuple(PATS.values()))


CREDENTIALS: Mapping[str, Callable[[World, HostSlice], Arranged]] = {
    "PAT": _pat,
    "gh": _gh,
    "helper": _helper,
    "none": _none,
    "az-bearer": _az_bearer,
}


# -- egress operations -------------------------------------------------------


def _set_remote(world: World, host: HostSlice, org: str) -> None:
    """Point the project's origin at *org* on the row's host (policy discovery)."""
    git = ("git", "-C", str(world.project))
    subprocess.run((*git, "init", "-q", "--initial-branch=main"), env=world.env, check=True)
    remote = f"https://{host.git_host}/{org}/consumer.git"
    subprocess.run((*git, "remote", "add", "origin", remote), env=world.env, check=True)


def _policy_remote(world: World, host: HostSlice) -> None:
    if host.host == "ADO":
        use_policy(world, "allow", host="ado")
    else:
        _set_remote(world, host, policy_org("allow"))


def _is_policy(request: ScriptedRequest) -> bool:
    path = request.path
    return (
        path.endswith(f"/{POLICY_REPO}/contents/apm-policy.yml")
        or "/contents/apm-policy.yml" in path
        # Azure DevOps Items API: the file path travels in the query string.
        or ("/_apis/git/repositories/" in path and path.endswith("/items"))
    )


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
    "api-probe": Operation(
        lambda w, h: ["install", h.api_spec or h.spec],
        _is_api,
        lambda w, h: h.api_setup(w) if h.api_setup else None,
    ),
    "marketplace-fetch": Operation(lambda w, h: ["marketplace", "add", f"{h.base}/marketplace"]),
    "policy-fetch": Operation(lambda w, h: ["policy", "status"], _is_policy, _policy_remote),
    "mcp-registry": Operation(lambda w, h: ["install", "--mcp", REGISTRY_SERVER]),
    "registry": Operation(
        lambda w, h: ["install", w.packages["p-skill:registry"].spec],
        setup=lambda w, h: use_registry(w, "project"),
    ),
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


# Under PROXY_REGISTRY_ONLY, a registry the operator configured is the sanctioned
# route, not a bypass (registry/client.py _refuse_direct_egress_when_proxy_only;
# docs enterprise/registry-proxy.md): the world's MCP registry (MCP_REGISTRY_URL)
# and its package registry (bound in the user config).
_SANCTIONED: Mapping[str, tuple[str, ...]] = {
    "mcp-registry": (REGISTRY_HOST,),
    "registry": (world_registry.HOST,),
}

# Where a REDIRECT row sends the host's requests: a path no route serves.
_MOVED = "/lifecycle-egress-moved"
# A redirect may break the operation; credential isolation is the subject.
_REDIRECT_EXITS = (0, 1)


def _redirect(world: World, host: HostSlice, kind: str) -> None:
    """Answer every request to the host's own origins with a 307.

    ``same-origin`` sends it to an unserved path on the same origin (a 404);
    ``cross-origin`` sends it to a spare host outside every platform.
    """
    for origin in dict.fromkeys((host.git_host, *host.family)):
        if kind == "same-origin":
            world.services.redirect(origin, "/", f"https://{origin}{_MOVED}")
            world.services.script(origin, _MOVED, 404, {"Content-Type": "text/plain"}, b"moved\n")
        else:
            world.services.redirect(origin, "/", f"https://{routes.MIRROR_HOSTS[0]}{_MOVED}")


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
    fields = _fields(row.id)
    redirect = fields.get("REDIRECT") in ("same-origin", "cross-origin")
    # PROXY_ONLY=1: downloads must go through the enterprise registry proxy and
    # every direct content host refuses (policy fetches stay direct by design).
    proxy_only = fields.get("PROXY_ONLY") == "1"
    if proxy_only:
        use_registry_proxy(world, only=True)
    world.services.reset()
    if row.auth:
        world.services.require_ado_auth()
    if redirect:
        _redirect(world, host, fields["REDIRECT"])
    sanctioned = _SANCTIONED.get(row.egress, ()) if proxy_only else ()
    if proxy_only and not sanctioned:
        world.services.forbid_direct()
    before = ArtifactSnapshotSet.capture(roots)
    result: CommandResult = driver.run(world, operation.command(world, host))
    after = ArtifactSnapshotSet.capture(roots)
    exits = _REDIRECT_EXITS if redirect or proxy_only else row.exit
    exits = (exits,) if isinstance(exits, int) else exits
    assert result.returncode in exits, (
        f"{row.egress} exited {result.returncode}, wanted {exits}:\n"
        f"{result.stdout}\n{result.stderr}"
    )
    requests = [
        request
        for request in world.services.proxy.observations
        if request.method != "CONNECT"
        # A redirected step's requests to the spare hosts all belong to it.
        and (operation.belongs(request) or (redirect and request.host in routes.MIRROR_HOSTS))
    ]
    reached = {request.host for request in requests}
    everything = world.services.proxy.observations
    # A redirect may end the step before the operation's own requests start.
    touched = {r.host for r in everything if r.method != "CONNECT"} if redirect else reached
    if proxy_only:
        direct = sorted(
            {
                (r.host, r.path)
                for r in everything
                if r.method != "CONNECT"
                and r.host != routes.PROXY_HOST
                and r.host not in sanctioned
                and "/policy-" not in r.path
            }
        )
        assert not direct, f"a proxy-only {row.egress} went direct: {direct}"
        carriers = sorted({r.host for r in everything if _carried(r, (routes.PROXY_TOKEN,))})
        assert set(carriers) <= {routes.PROXY_HOST}, f"the proxy token reached {carriers}"
        # The proxy itself is not one of the host's origins: no credential of it may go there.
        requests = [r for r in everything if r.method != "CONNECT"]
        reached = {request.host for request in requests}
    else:
        assert host.git_host in touched or touched & set(host.family), (
            f"{row.egress} never reached {host.host}: {sorted(reached)}"
        )
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
        # The proxy's own bearer is checked separately (it may reach only the proxy).
        carrying = [
            request
            for request in requests
            if _authorized(request) and not _carried(request, (routes.PROXY_TOKEN,))
        ]
    others = sorted(reached - set(host.family))
    # In play: the own credential reached every own origin the operation used.
    # A redirected first request may leave before any credential is presented.
    present = (
        sorted(reached & set(host.family))
        if row.presented and arranged.own and not redirect and not proxy_only
        else []
    )
    secrets = (
        *arranged.own,
        *arranged.foreign,
        *((routes.PROXY_TOKEN,) if proxy_only else ()),
    )
    check_obligation(
        row.id,
        strength=security_obligations()["E"][row.id],
        before=before,
        after=after,
        result=result,
        evidence=Evidence(
            egress=tuple(_entry(request) for request in carrying),
            egress_present_hosts=present,
            egress_absent_hosts=(
                *others,
                *(() if arranged.own else host.family),
                *(routes.MIRROR_HOSTS if redirect else ()),
            ),
            secrets_absent=secrets,
            exit=exits,
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
