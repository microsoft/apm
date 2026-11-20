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

A row passes only if its credential was in play: a sentinel reached its
host (``Credential.presented_to``, ``CredentialRow.presented_path``) or the
artifact holds the env reference (``CredentialRow.references``).
``check_obligation`` then checks ``secrets_absent`` over every file under
the after roots and the decisive command's output. ``ORIGIN_ROWS``
(marketplace origin, the M space) shares this module.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest
import yaml

from tests.lifecycle import driver, oracles
from tests.lifecycle.obligation_space import security_obligations
from tests.lifecycle.rows import Row, RowReplay
from tests.lifecycle.security.test_integrity import _MARKETPLACE_HOME, TamperReplay
from tests.lifecycle.world import World, use_policy
from tests.lifecycle.world.builder import TAG
from tests.lifecycle.world.contract import PackageRef
from tests.lifecycle.world.services import API_HOST, GITHUB_HOST, HOST_ALIASES, POLICY_FILE
from tests.utils.apm_lifecycle_runner import CommandResult
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
MCP_REFERENCE = "${" + MCP_ENV + "}"

# Recognizable sentinel values, shaped like the real credential of each kind.
GITHUB_APM_PAT = "ghp_" + "LifecycleSentinelApmPat".ljust(36, "0")
GITHUB_TOKEN = "ghp_" + "LifecycleSentinelGhToken".ljust(36, "1")
ADO_PAT = "lifecyclesentineladopat".ljust(52, "q")
MCP_SECRET = "lifecycle-mcp-header-sentinel-4b9e1c"


def _pending(phrase: str) -> str:
    """Reason of a strict expected failure whose tracking issue is maintainer-routed."""
    return f"S-pending (neutral issue to follow): {phrase}"


# -- credential arrangements -------------------------------------------------


@dataclass(frozen=True)
class Credential:
    """One credential kind in play in the world.

    ``arrange`` returns the world view to run in (env, package spec);
    ``secrets`` are the sentinel values.
    """

    arrange: Callable[[World], World]
    secrets: tuple[str, ...]
    # Hosts of which one must receive a sentinel during the row: proof the
    # credential is in play (empty: the credential never leaves the machine).
    presented_to: tuple[str, ...] = ()


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


def _github_pat(world: World) -> World:
    use_policy(world, "allow")
    world = _with_env(world, GITHUB_APM_PAT=GITHUB_APM_PAT, GITHUB_TOKEN=GITHUB_TOKEN)
    return _bind_package(world, world.packages[f"{PACKAGE}:git"].spec)


def _ado_pat(world: World) -> World:
    use_policy(world, "allow")
    return _bind_package(_with_env(world, ADO_APM_PAT=ADO_PAT), _ado_spec())


def _mcp_header(world: World) -> World:
    use_policy(world, "allow")
    world = _with_env(world, **{MCP_ENV: MCP_SECRET})
    return _bind_package(world, world.packages[f"{PACKAGE}:git"].spec)


CREDENTIALS: Mapping[str, Credential] = {
    "github-PAT": Credential(_github_pat, (GITHUB_APM_PAT, GITHUB_TOKEN), (GITHUB_HOST, API_HOST)),
    "ADO-PAT-or-helper": Credential(_ado_pat, (ADO_PAT,), (HOST_ALIASES["ado"][0],)),
    "mcp-header-secret": Credential(_mcp_header, (MCP_SECRET,)),
}

# -- rows ----------------------------------------------------------------------

INSTALL = f"apm install {PACKAGE}"
MCP_INSTALL = (
    f"apm install --mcp {MCP_NAME} --url {MCP_URL} --header 'Authorization=Bearer {MCP_REFERENCE}'"
)
PACK = "apm pack -o {pack}"


@dataclass(frozen=True)
class CredentialRow:
    """One C obligation: run ``steps`` then ``command``; scan ``roots`` and its output.

    ``present`` proves the row's artifact exists after the command. Proof the
    credential is in play: ``presented_path`` names a request path that must
    carry a sentinel; ``references`` names project files that must hold the
    env reference (the value never leaves the machine).
    """

    steps: tuple[str, ...]
    command: str
    roots: tuple[str, ...] = ()  # "project", "cache", "home", "pack"
    present: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    presented_path: str = ""
    references: tuple[str, ...] = ()
    xfail: str | None = None
    smoke: bool = False


# File rows scan every root (project, APM_CACHE_DIR, HOME, pack output);
# ``present`` pins the row's own artifact.
_FILES = ("project", "cache", "home", "pack")
_BUNDLE = "lifecycle-consumer-1.0.0"
_REF = (MANIFEST,)

CREDENTIAL_ROWS: Mapping[str, CredentialRow] = {
    # GitHub token in GITHUB_APM_PAT and GITHUB_TOKEN.
    "C|apm.yml|github-PAT": CredentialRow(
        (), INSTALL, _FILES, {"project": (MANIFEST,)}, smoke=True
    ),
    "C|lock|github-PAT": CredentialRow(
        (INSTALL,), "apm update --yes", _FILES, {"project": (LOCK,)}
    ),
    "C|cache-git-config|github-PAT": CredentialRow((), INSTALL, _FILES, {"cache": ("git",)}),
    "C|policy-cache|github-PAT": CredentialRow(
        (INSTALL,),
        "apm audit --ci",
        _FILES,
        {"cache": ("policy_v1",)},
        presented_path=POLICY_FILE,
    ),
    "C|pack-output|github-PAT": CredentialRow(
        (INSTALL,), PACK, _FILES, {"pack": (f"{_BUNDLE}/skills/{PACKAGE}/SKILL.md",)}
    ),
    "C|stdout-stderr|github-PAT": CredentialRow((), f"{INSTALL} --verbose"),
    # Azure DevOps PAT in ADO_APM_PAT.
    "C|apm.yml|ADO-PAT-or-helper": CredentialRow((), INSTALL, _FILES, {"project": (MANIFEST,)}),
    "C|lock|ADO-PAT-or-helper": CredentialRow(
        (INSTALL,), "apm update --yes", _FILES, {"project": (LOCK,)}
    ),
    "C|cache-git-config|ADO-PAT-or-helper": CredentialRow(
        (), INSTALL, _FILES, {"cache": ("git",)}, smoke=True
    ),
    "C|stdout-stderr|ADO-PAT-or-helper": CredentialRow((), f"{INSTALL} --verbose"),
    # MCP header secret passed through an env reference in apm.yml.
    "C|apm.yml|mcp-header-secret": CredentialRow(
        (), MCP_INSTALL, _FILES, {"project": (MANIFEST,)}, references=_REF
    ),
    "C|lock|mcp-header-secret": CredentialRow(
        (MCP_INSTALL,), "apm install", _FILES, {"project": (LOCK,)}, references=(LOCK,)
    ),
    "C|rendered-mcp-config|mcp-header-secret": CredentialRow(
        (),
        MCP_INSTALL,
        _FILES,
        {"project": (".mcp.json",)},
        references=(".mcp.json",),
        smoke=True,
    ),
    "C|pack-output|mcp-header-secret": CredentialRow(
        (MCP_INSTALL,), PACK, _FILES, {"pack": (f"{_BUNDLE}/.mcp.json",)}, references=_REF
    ),
    "C|stdout-stderr|mcp-header-secret": CredentialRow(
        (), f"{MCP_INSTALL} --verbose", references=_REF
    ),
}

# Not registered, missing world support: C|*|registry-token (no APM package
# registry service); C|*|az-bearer and the git-credential-helper half of
# ADO-PAT-or-helper (the world's Azure DevOps host answers anonymous requests,
# so APM never reaches those chain steps); C|policy-cache|ADO-PAT-or-helper
# (no Azure DevOps policy repository is served).

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


def _pack_dir(world: World) -> Path:
    return world.root / "pack-out"


def _roots(world: World, names: tuple[str, ...]) -> dict[str, Path]:
    available = {
        "project": world.project,
        "cache": world.cache_root or world.root / "cache",
        "home": world.home,
        "pack": _pack_dir(world),
    }
    return {name: available[name] for name in names}


def _check_in_play(world: World, credential: Credential, row: CredentialRow) -> None:
    """The row tests its cell only if the credential was actually used."""
    carried = [
        request
        for request in world.services.proxy.observations
        if any(
            secret in value for value in request.header_values() for secret in credential.secrets
        )
    ]
    if credential.presented_to:
        hosts = sorted({request.host for request in carried})
        assert set(credential.presented_to) & set(hosts), f"credential reached only {hosts}"
    if row.presented_path:
        paths = sorted({request.path for request in carried})
        assert any(row.presented_path in path for path in paths), f"credential sent to {paths}"
    for relative in row.references:
        text = (world.project / relative).read_text(encoding="utf-8")
        assert MCP_REFERENCE in text, f"{relative} does not reference {MCP_REFERENCE}"


def run_credential_row(world: World, cell_id: str) -> None:
    """Arrange the credential, replay the steps, scan the roots and output for it."""
    _, _artifact, kind = cell_id.split("|")
    row = CREDENTIAL_ROWS[cell_id]
    credential = CREDENTIALS[kind]
    replay = RowReplay(
        credential.arrange(world),
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
    roots = _roots(world, row.roots)
    for step in row.steps:
        replay.step(step.replace("{pack}", str(_pack_dir(world))))
    before = ArtifactSnapshotSet.capture(roots) if roots else None
    replay.step(row.command.replace("{pack}", str(_pack_dir(world))))
    after = ArtifactSnapshotSet.capture(roots) if roots else None
    _check_in_play(world, credential, row)
    oracles.check_obligation(
        cell_id,
        strength=CREDENTIAL_STRENGTH,
        before=before,
        after=after,
        result=replay.last,
        evidence=oracles.Evidence(
            secrets_absent=credential.secrets,
            present=row.present,
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
        assert cell_id.isascii()


# == ORIGIN_ROWS: marketplace origin (the M space, lane T-7c) ====================
#
# PROMISE: a marketplace declaration resolves only from the origin the lock
# recorded; a changed origin, an unreachable catalog or an edited lock row is
# reported, never silently followed (source:
# docs/src/content/docs/consumer/installing-from-marketplaces.md, "The name
# ... is local to your machine"; docs/src/content/docs/reference/cli/marketplace.md
# "One catalog, one name"). A row id is the exact obligation ``M|<op>|<event>``.
#
# Each row arranges a test-local catalog (served with ``WorldServices.serve``),
# registers it, declares p-skill from it (object form, or through a local
# wrapper package for the transitive op), installs, applies the event, runs the
# op through the replay (I1-I9) and checks the obligation evidence.
#
# Unregistered: every ``confusion-sentinel(cross-repo bare name)`` id (8). The
# sentinel needs a ``*.ghe.com`` marketplace; the CLI reads its catalog from
# ``https://<host>/api/v3/repos/...`` and the world serves that API path only
# for the GHES alias, so registration fails before any row can arrange.

_ORIGIN_STRENGTH = "e2e"
_ORIGIN_MARKET = "origin-market"
_ORIGIN_SLUG = "lifecycle-origin/catalog"
_ORIGIN_OTHER_SLUG = "lifecycle-origin/other"
_ORIGIN_LOCK = "apm.lock.yaml"
_ORIGIN_MANIFEST = "apm.yml"
_ORIGIN_SKILL = ".agents/skills/p-skill/SKILL.md"
_ORIGIN_STATE = (_ORIGIN_LOCK, _ORIGIN_MANIFEST, ".agents/skills/p-skill")
_ORIGIN_GIT = ("git", "-c", "user.name=lifecycle", "-c", "user.email=lifecycle@example.invalid")


def _origin_pending(phrase: str) -> str:
    return f"S-pending (neutral issue to follow): {phrase}"


def _origin_plugin(name: str, repo: str) -> dict:
    return {
        "name": name,
        "description": name,
        "source": {"source": "github", "repo": repo, "ref": TAG},
    }


def _origin_publish(world: World, slug: str, plugins: list[dict], name: str) -> None:
    """Serve a test-authored catalog repository *slug* named *name*."""
    root = world.root / "origin-catalogs"
    index = len(list(root.glob("*.git"))) if root.is_dir() else 0
    tree = root / f"tree{index}"
    (tree / ".claude-plugin").mkdir(parents=True)
    document = {"name": name, "owner": {"name": "Lifecycle"}, "plugins": plugins}
    (tree / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps(document, indent=2) + "\n", encoding="ascii"
    )
    bare = root / f"catalog{index}.git"
    for args in (
        ("-C", str(tree), "init", "-q", "--initial-branch=main"),
        ("-C", str(tree), "add", "-A"),
        ("-C", str(tree), "commit", "-qm", "catalog"),
        ("clone", "-q", "--bare", str(tree), str(bare)),
    ):
        subprocess.run((*_ORIGIN_GIT, *args), env=world.env, check=True, capture_output=True)
    world.services.serve(slug, bare)


def _origin_register(world: World, slug: str, *extra: str) -> CommandResult:
    result = driver.run(world, ["marketplace", "add", slug, *extra])
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def _origin_declare(world: World, entry: object) -> None:
    manifest = world.project / _ORIGIN_MANIFEST
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["dependencies"]["apm"] = [entry]
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")


def _origin_wrapper(world: World) -> Path:
    """A local package whose own apm.yml declares p-skill from the marketplace."""
    root = world.root / "origin-wrap"
    root.mkdir()
    manifest = {
        "name": "origin-wrap",
        "version": "1.0.0",
        "dependencies": {"apm": [{"name": "p-skill", "marketplace": _ORIGIN_MARKET}]},
    }
    (root / _ORIGIN_MANIFEST).write_text(yaml.safe_dump(manifest), encoding="utf-8")
    return root


def _origin_arrange(world: World, *, transitive: bool) -> TamperReplay:
    """Publish and register the catalog, declare p-skill from it and converge."""
    _origin_publish(
        world,
        _ORIGIN_SLUG,
        [
            _origin_plugin("p-skill", "lifecycle/p-skill"),
            _origin_plugin("p-exec", "lifecycle/p-exec"),
        ],
        _ORIGIN_MARKET,
    )
    _origin_register(world, _ORIGIN_SLUG)
    entry = (
        str(_origin_wrapper(world))
        if transitive
        else {"name": "p-skill", "marketplace": _ORIGIN_MARKET}
    )
    _origin_declare(world, entry)
    row = Row(
        id="T7c/origin",
        promises=("P-OWN-marketplace-catalog-origin",),
        packages=("p-skill",),
        targets=("copilot",),
        scope="project",
        given=(),
        when=(),
    )
    replay = TamperReplay(world, row)
    replay.home_allowed = _MARKETPLACE_HOME
    replay._set_targets()
    replay.model = replay.model.declare("p-skill")
    replay.step("apm install")
    locked = _origin_row(world)
    assert locked["discovered_via"] == _ORIGIN_MARKET, locked
    return replay


def _origin_row(world: World) -> dict:
    data = yaml.safe_load((world.project / _ORIGIN_LOCK).read_text(encoding="utf-8"))
    return next(dep for dep in data["dependencies"] if dep["repo_url"] == "lifecycle/p-skill")


# -- events -------------------------------------------------------------------


def _origin_rebind(world: World) -> None:
    """The local name now points at another catalog that offers p-skill from p-exec."""
    _origin_publish(
        world, _ORIGIN_OTHER_SLUG, [_origin_plugin("p-skill", "lifecycle/p-exec")], _ORIGIN_MARKET
    )
    _origin_register(world, _ORIGIN_OTHER_SLUG, "--name", _ORIGIN_MARKET)


def _origin_unreachable(world: World) -> None:
    """The catalog repository stops answering (no repository behind the slug)."""
    world.services.serve(_ORIGIN_SLUG, world.root / "origin-missing.git")


def _origin_duplicate(world: World) -> None:
    result = _origin_register(world, _ORIGIN_SLUG, "--name", "origin-copy")
    assert "points at the same catalog" in result.stdout + result.stderr


def _origin_edit_row(world: World) -> None:
    """The lock's marketplace row names another marketplace."""
    path = world.project / _ORIGIN_LOCK
    text = path.read_text(encoding="utf-8")
    old = f"discovered_via: {_ORIGIN_MARKET}"
    assert old in text, text
    path.write_text(text.replace(old, "discovered_via: origin-elsewhere"), encoding="utf-8")


_ORIGIN_EVENTS: Mapping[str, Callable[[World], None]] = {
    "alias-rebind": _origin_rebind,
    "catalog-unreachable": _origin_unreachable,
    "duplicate-origin-registration": _origin_duplicate,
    "lock-row-edited": _origin_edit_row,
}
_ORIGIN_OPS: Mapping[str, str] = {
    "audit-ci": "apm audit --ci",
    "dry-run": "apm install --dry-run",
    "frozen": "apm install --frozen",
    "install object-form": "apm install",
    "install plugin@mkt": f"apm install p-skill@{_ORIGIN_MARKET}",
    "outdated/view": "apm outdated",
    "transitive marketplace dep": "apm install",
    "update": "apm update --yes",
}


@dataclass(frozen=True)
class _OriginExpect:
    exit: int
    output: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    present: tuple[str, ...] = ()
    # The lock row still records the declared origin after the op.
    keeps_origin: bool = True
    xfail: str | None = None


_ORIGIN_BOTH = (
    r"now points at https://github\.com/lifecycle-origin/other, but apm\.lock\.yaml recorded"
)
_ORIGIN_REPLAY = _OriginExpect(0, unchanged=_ORIGIN_STATE)
_ORIGIN_SAME = r"points at the same catalog|also found in marketplace"

ORIGIN_ROWS: dict[str, _OriginExpect] = {
    # A rebound name: re-resolution fails and names both origins; the locked
    # entry keeps replaying (installing-from-marketplaces.md).
    "M|audit-ci|alias-rebind": _ORIGIN_REPLAY,
    "M|dry-run|alias-rebind": _OriginExpect(0, unchanged=_ORIGIN_STATE),
    "M|install object-form|alias-rebind": _ORIGIN_REPLAY,
    "M|install plugin@mkt|alias-rebind": _OriginExpect(
        1, (_ORIGIN_BOTH, r"Nothing was changed"), unchanged=_ORIGIN_STATE
    ),
    "M|outdated/view|alias-rebind": _OriginExpect(1, (_ORIGIN_BOTH,), unchanged=_ORIGIN_STATE),
    "M|transitive marketplace dep|alias-rebind": _ORIGIN_REPLAY,
    "M|update|alias-rebind": _OriginExpect(1, (_ORIGIN_BOTH,), unchanged=_ORIGIN_STATE),
    # An unreachable catalog: install and --frozen replay the lock; update
    # fails rather than use stale data.
    "M|audit-ci|catalog-unreachable": _ORIGIN_REPLAY,
    "M|dry-run|catalog-unreachable": _ORIGIN_REPLAY,
    "M|frozen|catalog-unreachable": _ORIGIN_REPLAY,
    "M|install object-form|catalog-unreachable": _ORIGIN_REPLAY,
    "M|install plugin@mkt|catalog-unreachable": _OriginExpect(0, present=(_ORIGIN_SKILL,)),
    "M|outdated/view|catalog-unreachable": _ORIGIN_REPLAY,
    "M|transitive marketplace dep|catalog-unreachable": _ORIGIN_REPLAY,
    "M|update|catalog-unreachable": _OriginExpect(
        1,
        (r"Could not verify that marketplace 'origin-market' is current",),
        unchanged=_ORIGIN_STATE,
    ),
    # A second name for the same catalog: a warning names the existing alias;
    # the declared origin stays the one resolved.
    "M|audit-ci|duplicate-origin-registration": _ORIGIN_REPLAY,
    "M|dry-run|duplicate-origin-registration": _ORIGIN_REPLAY,
    "M|install object-form|duplicate-origin-registration": _ORIGIN_REPLAY,
    "M|install plugin@mkt|duplicate-origin-registration": _OriginExpect(
        0, (_ORIGIN_SAME,), present=(_ORIGIN_SKILL,)
    ),
    "M|outdated/view|duplicate-origin-registration": _ORIGIN_REPLAY,
    "M|transitive marketplace dep|duplicate-origin-registration": _ORIGIN_REPLAY,
    "M|update|duplicate-origin-registration": _OriginExpect(
        0, (_ORIGIN_SAME,), unchanged=_ORIGIN_STATE
    ),
    # An edited lock row no longer matches the declaration: audit and --frozen
    # report it (baseline-checks.md ref-consistency, install.md frozen mode);
    # install and update resolve the declared origin again.
    "M|audit-ci|lock-row-edited": _OriginExpect(
        1, (r"ref-consistency",), unchanged=_ORIGIN_STATE, keeps_origin=False
    ),
    "M|frozen|lock-row-edited": _OriginExpect(1, unchanged=_ORIGIN_STATE, keeps_origin=False),
    "M|dry-run|lock-row-edited": _OriginExpect(0, unchanged=_ORIGIN_STATE, keeps_origin=False),
    "M|install object-form|lock-row-edited": _OriginExpect(0, present=(_ORIGIN_SKILL,)),
    "M|install plugin@mkt|lock-row-edited": _OriginExpect(0, present=(_ORIGIN_SKILL,)),
    "M|outdated/view|lock-row-edited": _OriginExpect(
        0, unchanged=_ORIGIN_STATE, keeps_origin=False
    ),
    "M|transitive marketplace dep|lock-row-edited": _OriginExpect(0, present=(_ORIGIN_SKILL,)),
    "M|update|lock-row-edited": _OriginExpect(0, present=(_ORIGIN_SKILL,)),
}


def _origin_rows():
    for cell_id, expect in ORIGIN_ROWS.items():
        marks = [pytest.mark.xfail(strict=True, reason=expect.xfail)] if expect.xfail else []
        yield pytest.param(cell_id, id=cell_id, marks=marks)


def run_origin_row(world: World, cell_id: str) -> None:
    _, op, event = cell_id.split("|")
    expect = ORIGIN_ROWS[cell_id]
    replay = _origin_arrange(world, transitive=op == "transitive marketplace dep")
    _ORIGIN_EVENTS[event](world)
    replay.tampered = True
    before = replay.observe()
    replay._run(_ORIGIN_OPS[op], read_only=op == "audit-ci", pinned_exit=expect.exit)
    after = replay.observe()
    if expect.keeps_origin:
        assert _origin_row(world)["discovered_via"] == _ORIGIN_MARKET, _origin_row(world)
    oracles.check_obligation(
        cell_id,
        strength=_ORIGIN_STRENGTH,
        before=before,
        after=after,
        result=replay.last,
        evidence=oracles.Evidence(
            exit=expect.exit,
            output_matches=expect.output,
            unchanged={"project": expect.unchanged} if expect.unchanged else {},
            present={"project": expect.present} if expect.present else {},
        ),
    )


@pytest.mark.parametrize("cell_id", list(_origin_rows()))
def test_origin_row(lifecycle_world: World, cell_id: str) -> None:
    run_origin_row(lifecycle_world, cell_id)


def test_every_origin_row_is_a_generated_obligation() -> None:
    space = security_obligations()["M"]
    unknown = sorted(set(ORIGIN_ROWS) - set(space))
    assert not unknown, f"rows with no M obligation id: {unknown}"
    assert {space[cell_id] for cell_id in ORIGIN_ROWS} <= {_ORIGIN_STRENGTH}
    for cell_id in ORIGIN_ROWS:
        _, op, event = cell_id.split("|")
        assert op in _ORIGIN_OPS and event in _ORIGIN_EVENTS, cell_id
