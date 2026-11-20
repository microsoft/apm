"""Credentials stay out of persisted artifacts and command output (#4109).

PROMISE: P-LAW-auth.credentials_isolated -- credentials remain scoped to
their intended host and never enter persisted URLs or output (source:
tests/fixtures/lifecycle_bug_ledger.json). The docs promise the same per
artifact: secrets are written as references, never values
(docs/src/content/docs/consumer/install-mcp-servers.md "Keep secrets out of
config files"); a packed plugin carries no MCP credentials
(docs/src/content/docs/reference/cli/pack.md "Credential stripping");
tokens are resolved per host at run time
(docs/src/content/docs/getting-started/authentication.md).

FRAMEWORK. One parametrized test runs every row of ``CREDENTIAL_ROWS``. A
row id is the exact generated obligation ``C|<artifact>|<credential>``
(``tests/lifecycle/obligation_space.py``). A row names:

- the credential arrangement (``CREDENTIALS``): the env, shims and package
  spec that put one credential kind in play with recognizable sentinels;
- ``steps``: replay steps before the decisive command (I1-I9 per step);
- ``command``: the decisive command, also run through the replay;
- ``roots``: the snapshot roots whose every file the oracle scans.

``check_obligation`` then checks ``secrets_absent`` over every file under
the after roots and the decisive command's output. ``ORIGIN_ROWS``
(marketplace origin, the M space) shares this module.
"""

from __future__ import annotations

import contextlib
import stat
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
import yaml

from tests.lifecycle import oracles
from tests.lifecycle.obligation_space import security_obligations
from tests.lifecycle.rows import Row, RowReplay
from tests.lifecycle.world import World, use_policy
from tests.lifecycle.world.builder import TAG
from tests.lifecycle.world.contract import PackageRef
from tests.lifecycle.world.services import HOST_ALIASES
from tests.utils.artifact_snapshot import ArtifactSnapshotSet

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.promise(
        "P-LAW-auth.credentials_isolated",
        "P-OWN-host-credential-resolution",
        "P-INV-I7",
        "P-INV-I8",
    ),
]

CREDENTIAL_STRENGTH = "e2e-evidence"
LOCK = "apm.lock.yaml"
MANIFEST = "apm.yml"
PACKAGE = "p-skill"
MCP_NAME = "p-header"
MCP_URL = "https://mcp.lifecycle.test/p-header"
MCP_ENV = "LIFECYCLE_MCP_TOKEN"

# Recognizable sentinel values, shaped like the real credential of each kind.
GITHUB_APM_PAT = "ghp_" + "LifecycleSentinelApmPat".ljust(36, "0")
GITHUB_TOKEN = "ghp_" + "LifecycleSentinelGhToken".ljust(36, "1")
ADO_PAT = "lifecyclesentineladopat".ljust(52, "q")
AZ_BEARER = "eyJhbGciOiJub25lIn0.eyJzZW50aW5lbCI6ImxpZmVjeWNsZS1hei1iZWFyZXIifQ.bGlmZWN5Y2xlc2ln"
MCP_SECRET = "lifecycle-mcp-header-sentinel-4b9e1c"


def _pending(phrase: str) -> str:
    """Reason of a strict expected failure whose tracking issue is maintainer-routed."""
    return f"S-pending (neutral issue to follow): {phrase}"


# -- credential arrangements -------------------------------------------------


@dataclass(frozen=True)
class Credential:
    """One credential kind in play in the world.

    ``arrange`` returns the world view to run in (env, package spec) and may
    register cleanups on the stack; ``secrets`` are the sentinel values.
    """

    arrange: Callable[[World, contextlib.ExitStack], World]
    secrets: tuple[str, ...]
    # Host that must receive a sentinel during the row: proof the credential is in play.
    presented_to: str | None = None


def _with_env(world: World, **values: str) -> World:
    env = dict(world.env)
    env.update(values)
    return replace(world, env=env)


def _bind_package(world: World, spec: str) -> World:
    packages = dict(world.packages)
    base = world.packages[f"{PACKAGE}:git"]
    packages[PACKAGE] = PackageRef(PACKAGE, spec, base.kinds, "git")
    return replace(world, packages=packages)


def _ado_spec() -> str:
    host, prefix = HOST_ALIASES["ado"]
    return f"{host}/{prefix}/{PACKAGE}#{TAG}"


def _github_pat(world: World, stack: contextlib.ExitStack) -> World:
    del stack
    use_policy(world, "allow")
    world = _with_env(world, GITHUB_APM_PAT=GITHUB_APM_PAT, GITHUB_TOKEN=GITHUB_TOKEN)
    return _bind_package(world, world.packages[f"{PACKAGE}:git"].spec)


def _ado_pat(world: World, stack: contextlib.ExitStack) -> World:
    del stack
    use_policy(world, "allow")
    return _bind_package(_with_env(world, ADO_APM_PAT=ADO_PAT), _ado_spec())


_AZ_SHIM = """
import json
import sys

if sys.argv[1:3] == ["account", "get-access-token"]:
    print(json.dumps({"accessToken": TOKEN, "expiresOn": "2999-01-01 00:00:00.000000"}))
    raise SystemExit(0)
if sys.argv[1:3] == ["account", "show"]:
    print(json.dumps({"tenantId": "00000000-0000-0000-0000-000000000000"}))
    raise SystemExit(0)
raise SystemExit(1)
"""


def _az_bearer(world: World, stack: contextlib.ExitStack) -> World:
    del stack
    use_policy(world, "allow")
    directory = world.root / "az-cli"
    directory.mkdir()
    shim = directory / "az"
    shim.write_text(
        f"#!{sys.executable}\nTOKEN = {AZ_BEARER!r}\n" + _AZ_SHIM, encoding="ascii", newline="\n"
    )
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    path = f"{directory}:{world.env.get('PATH', '')}"
    return _bind_package(_with_env(world, PATH=path), _ado_spec())


def _mcp_header(world: World, stack: contextlib.ExitStack) -> World:
    del stack
    use_policy(world, "allow")
    world = _with_env(world, **{MCP_ENV: MCP_SECRET})
    return _bind_package(world, world.packages[f"{PACKAGE}:git"].spec)


CREDENTIALS: Mapping[str, Credential] = {
    "github-PAT": Credential(_github_pat, (GITHUB_APM_PAT, GITHUB_TOKEN), "github.com"),
    "ADO-PAT-or-helper": Credential(_ado_pat, (ADO_PAT,), HOST_ALIASES["ado"][0]),
    "az-bearer": Credential(_az_bearer, (AZ_BEARER,), HOST_ALIASES["ado"][0]),
    "mcp-header-secret": Credential(_mcp_header, (MCP_SECRET,)),
}

# -- rows ----------------------------------------------------------------------

INSTALL = f"apm install {PACKAGE}"
MCP_INSTALL = (
    f"apm install --mcp {MCP_NAME} --url {MCP_URL} "
    f"--header 'Authorization=Bearer ${{{MCP_ENV}}}'"
)
PACK = "apm pack -o {pack}"


@dataclass(frozen=True)
class CredentialRow:
    """One C obligation: run ``steps`` then ``command``; scan ``roots`` and its output."""

    steps: tuple[str, ...]
    command: str
    roots: tuple[str, ...] = ()  # "project", "cache", "home", "pack"
    present: Mapping[str, tuple[str, ...]] = None  # type: ignore[assignment]
    xfail: str | None = None
    smoke: bool = False


CREDENTIAL_ROWS: Mapping[str, CredentialRow] = {
    # GitHub token in GITHUB_APM_PAT and GITHUB_TOKEN.
    "C|apm.yml|github-PAT": CredentialRow(
        (), INSTALL, ("project",), {"project": (MANIFEST,)}, smoke=True
    ),
    "C|lock|github-PAT": CredentialRow(
        (INSTALL,), "apm update --yes", ("project",), {"project": (LOCK,)}
    ),
    "C|cache-git-config|github-PAT": CredentialRow((), INSTALL, ("cache",), {"cache": ("git",)}),
    "C|policy-cache|github-PAT": CredentialRow(
        (INSTALL,), "apm audit --ci", ("cache",), {"cache": ("policy_v1",)}
    ),
    "C|pack-output|github-PAT": CredentialRow((INSTALL,), PACK, ("pack",)),
    "C|stdout-stderr|github-PAT": CredentialRow((), f"{INSTALL} --verbose"),
    # Azure DevOps PAT in ADO_APM_PAT.
    "C|apm.yml|ADO-PAT-or-helper": CredentialRow(
        (), INSTALL, ("project",), {"project": (MANIFEST,)}
    ),
    "C|lock|ADO-PAT-or-helper": CredentialRow(
        (INSTALL,), "apm update --yes", ("project",), {"project": (LOCK,)}
    ),
    "C|cache-git-config|ADO-PAT-or-helper": CredentialRow(
        (), INSTALL, ("cache",), {"cache": ("git",)}
    ),
    "C|policy-cache|ADO-PAT-or-helper": CredentialRow(
        (INSTALL,), "apm audit --ci", ("cache",), {"cache": ("policy_v1",)}
    ),
    "C|stdout-stderr|ADO-PAT-or-helper": CredentialRow((), f"{INSTALL} --verbose"),
    # Azure CLI bearer from an ``az`` on PATH (no PAT).
    "C|lock|az-bearer": CredentialRow((), INSTALL, ("project",), {"project": (LOCK,)}),
    "C|cache-git-config|az-bearer": CredentialRow((), INSTALL, ("cache",), {"cache": ("git",)}),
    "C|stdout-stderr|az-bearer": CredentialRow((), f"{INSTALL} --verbose"),
    # MCP header secret passed through an env reference in apm.yml.
    "C|apm.yml|mcp-header-secret": CredentialRow(
        (), MCP_INSTALL, ("project",), {"project": (MANIFEST,)}
    ),
    "C|lock|mcp-header-secret": CredentialRow(
        (MCP_INSTALL,), "apm install", ("project",), {"project": (LOCK,)}
    ),
    "C|rendered-mcp-config|mcp-header-secret": CredentialRow(
        (), MCP_INSTALL, ("project",), {"project": (".mcp.json",)}
    ),
    "C|pack-output|mcp-header-secret": CredentialRow((MCP_INSTALL,), PACK, ("pack",)),
    "C|stdout-stderr|mcp-header-secret": CredentialRow((), f"{MCP_INSTALL} --verbose"),
}

# Marketplace-origin rows (the M space) join this module in ORIGIN_ROWS.


def _credential_rows():
    for cell_id, row in CREDENTIAL_ROWS.items():
        marks = []
        if row.smoke:
            marks.append(pytest.mark.lifecycle_smoke)
        if row.xfail:
            marks.append(pytest.mark.xfail(strict=True, reason=row.xfail))
        yield pytest.param(cell_id, id=cell_id, marks=marks)


CREDENTIAL_ROW_IDS = [param.values[0] for param in _credential_rows()]


def _roots(world: World, names: tuple[str, ...]) -> dict[str, Path]:
    available = {
        "project": world.project,
        "cache": world.cache_root or world.root / "cache",
        "home": world.home,
        "pack": world.root / "pack-out",
    }
    return {name: available[name] for name in names}


def _hosts_presented(world: World, secrets: tuple[str, ...]) -> list[str]:
    """Hosts whose requests carried a sentinel (headers, decoded Basic credentials)."""
    return sorted(
        {
            request.host
            for request in world.services.proxy.observations
            if any(secret in value for value in request.header_values() for secret in secrets)
        }
    )


def run_credential_row(world: World, cell_id: str) -> None:
    """Arrange the credential, replay the steps, scan the roots and output for it."""
    _, _artifact, kind = cell_id.split("|")
    row = CREDENTIAL_ROWS[cell_id]
    credential = CREDENTIALS[kind]
    with contextlib.ExitStack() as stack:
        view = credential.arrange(world, stack)
        replay = RowReplay(
            view,
            Row(
                id=f"T7c/credentials/{cell_id}",
                promises=("P-LAW-auth.credentials_isolated",),
                packages=(PACKAGE,),
                targets=("copilot",),
                scope="project",
                given=(),
                when=(),
            ),
        )
        replay._set_targets()
        for step in row.steps:
            replay.step(step.format(pack=world.root / "pack-out"))
        roots = _roots(world, row.roots)
        before = ArtifactSnapshotSet.capture(roots) if roots else None
        replay.step(row.command.format(pack=world.root / "pack-out"))
        after = ArtifactSnapshotSet.capture(roots) if roots else None
    if credential.presented_to is not None:
        hosts = _hosts_presented(world, credential.secrets)
        assert credential.presented_to in hosts, f"credential not in play: presented to {hosts}"
    oracles.check_obligation(
        cell_id,
        strength=CREDENTIAL_STRENGTH,
        before=before,
        after=after,
        result=replay.last,
        evidence=oracles.Evidence(
            secrets_absent=credential.secrets,
            present=dict(row.present or {}),
            exit=0,
        ),
    )


@pytest.mark.parametrize("cell_id", list(_credential_rows()))
def test_credential_row(lifecycle_world: World, cell_id: str) -> None:
    run_credential_row(lifecycle_world, cell_id)


def test_every_credential_row_is_a_generated_obligation() -> None:
    space = security_obligations()["C"]
    unknown = sorted(set(CREDENTIAL_ROW_IDS) - set(space))
    assert not unknown, f"rows with no C obligation id: {unknown}"
    assert len(CREDENTIAL_ROW_IDS) == len(set(CREDENTIAL_ROW_IDS))
    assert {space[cell_id] for cell_id in CREDENTIAL_ROW_IDS} <= {CREDENTIAL_STRENGTH}
    for cell_id in CREDENTIAL_ROW_IDS:
        assert cell_id.split("|")[2] in CREDENTIALS, cell_id
        assert yaml.safe_dump(cell_id).isascii()
