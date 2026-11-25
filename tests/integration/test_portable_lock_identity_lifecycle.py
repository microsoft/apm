"""A monorepo lockfile does not depend on the checkout location (#5418).

PROMISE: P-LAW-portability.byte_deterministic -- two clones of one repository
at different paths write a byte-identical apm.lock.yaml: a local package
inside the repository but outside the project root (a monorepo sibling) is
recorded relative to the project root with ``../``, in ``anchored_local_path``,
``declaring_parent: local:`` and the ledger owners (source: #5418 scope
record). A package outside the repository keeps its absolute identity.
CELL: OP=install SRC=local PRIM=skill SCOPE=project LOCK=absent|legacy-absolute
GRAPH=transitive-sibling REPO=git|none FROZEN=on|off

A lock written with the absolute form is accepted read-only by ``install
--frozen`` and ``audit --ci``; one plain install re-keys it in place (same
owners, no file deleted, the apm_modules slot moved) and the next install
is a byte no-op.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import load_yaml
from tests.integration._install_audit_replay_kit import (
    ReplayWorld,
    lock_bytes,
    tree_digest,
    write,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-LAW-portability.byte_deterministic", "P-INV-I2"),
]

_APP = Path("apps") / "app"


_MCP = (
    "  mcp:\n    - name: echo-srv\n      registry: false\n"
    "      transport: stdio\n      command: echo\n"
)


def _skill_package(root: Path, name: str, deps: tuple[str, ...] = (), *, mcp: bool = False) -> None:
    manifest = f"name: {name}\nversion: 1.0.0\n"
    if deps or mcp:
        manifest += "dependencies:\n"
    if deps:
        manifest += "  apm:\n" + "".join(f"    - {dep}\n" for dep in deps)
    if mcp:
        manifest += _MCP
    write(root / "apm.yml", manifest)
    write(
        root / f".apm/skills/{name}-skill/SKILL.md",
        f"---\nname: {name}-skill\ndescription: Skill of {name}\n---\n# {name}\n",
    )


def _monorepo(
    world: ReplayWorld, root: Path, *, outside: Path | None = None, mcp: bool = False
) -> Path:
    """The issue's layout: app -> ../../packages/lib-a -> ../lib-b -> ../lib-c (+ outside).

    With *mcp*, lib-b (a transitive sibling) declares a self-defined MCP server.
    """
    _skill_package(root / "packages" / "lib-c", "lib-c")
    _skill_package(root / "packages" / "lib-b", "lib-b", ("../lib-c",), mcp=mcp)
    lib_a_deps = ("../lib-b",) if outside is None else ("../lib-b", outside.as_posix())
    _skill_package(root / "packages" / "lib-a", "lib-a", lib_a_deps)
    app = root / _APP
    write(
        app / "apm.yml",
        "name: app\nversion: 1.0.0\ntargets: [claude]\n"
        "dependencies:\n  apm:\n    - ../../packages/lib-a\n",
    )
    (app / ".claude").mkdir(parents=True)
    return app


def _install_before_5418(world: ReplayWorld, repo: Path, app: Path, *args: str) -> None:
    """Write the pre-#5418 absolute form: with HOME at the repository root no bound applies."""
    env = {**world.env, "HOME": str(repo), "USERPROFILE": str(repo)}
    result = world.runner.run(("install", *args), cwd=app, env=env)
    assert result.returncode == 0, result.stdout + result.stderr


def _transitive_rows(app: Path) -> dict[str, dict]:
    lock = load_yaml(app / "apm.lock.yaml")
    return {row["name"]: row for row in lock["dependencies"] if row.get("anchored_local_path")}


def _owners(app: Path) -> list[tuple[str, tuple[str, ...], str]]:
    lock = load_yaml(app / "apm.lock.yaml")
    return [
        (row["value"], tuple(row["owners"]), row["active_owner"]) for row in lock["deployments"]
    ]


def _deployed(app: Path) -> dict[str, bytes]:
    """Every file outside apm_modules, .git and the lockfile, with its bytes."""
    files = tree_digest(app)
    files.pop("apm.lock.yaml")
    return files


def _slots(app: Path) -> set[str]:
    return {path.name for path in (app / "apm_modules" / "_local").iterdir() if path.is_dir()}


@pytest.mark.parametrize("git", [True, False], ids=["git-repository", "no-repository"])
def test_two_clones_write_byte_identical_lockfiles(
    tmp_path: Path, apm_binary_path: Path, git: bool
) -> None:
    """Both a git checkout and a clean copy with no .git (the issue's cp -r repro)."""
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    repo_a = world.sources / "A"
    app_a = _monorepo(world, repo_a, mcp=True)
    if git:
        world.git(repo_a, "init", "-q", "--initial-branch=main")
    repo_b = world.sources / "elsewhere" / "deeper" / "B"
    shutil.copytree(repo_a, repo_b, symlinks=True)
    app_b = repo_b / _APP

    world.apm("install", cwd=app_a)
    world.apm("install", cwd=app_b)
    for app in (app_a, app_b):
        world.apm("install", "--only", "apm", cwd=app)

    text = (app_a / "apm.lock.yaml").read_text(encoding="utf-8")
    assert (app_b / "apm.lock.yaml").read_text(encoding="utf-8") == text
    assert "mcp_omissions:\n  echo-srv:\n    declaration: sha256:" in text
    assert str(world.sources) not in text
    assert str(world.sources.resolve()) not in text
    rows = _transitive_rows(app_a)
    assert rows["lib-b"]["anchored_local_path"] == "../../packages/lib-b"
    assert rows["lib-b"]["declaring_parent"] == "../../packages/lib-a"
    assert rows["lib-c"]["anchored_local_path"] == "../../packages/lib-c"
    assert rows["lib-c"]["declaring_parent"] == "local:../../packages/lib-b"

    before = lock_bytes(app_a)
    world.apm("install", "--only", "apm", cwd=app_a)
    assert lock_bytes(app_a) == before, "I2: a converged install rewrote the lockfile"
    world.apm("install", "--frozen", "--only", "apm", cwd=app_a)
    world.audit_clean(cwd=app_a)


def test_outside_repository_keeps_absolute_and_no_repository_is_relative(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    outside = world.sources / "outside" / "lib-x"
    _skill_package(outside, "lib-x")
    repo = world.sources / "R"
    app = _monorepo(world, repo, outside=outside)
    world.git(repo, "init", "-q", "--initial-branch=main")
    world.apm("install", cwd=app)
    text = (app / "apm.lock.yaml").read_text(encoding="utf-8")
    assert f"anchored_local_path: {outside.resolve().as_posix()}" in text
    assert "anchored_local_path: ../../packages/lib-c" in text
    anchors = {row["anchored_local_path"] for row in _transitive_rows(app).values()}
    assert anchors == {
        "../../packages/lib-b",
        "../../packages/lib-c",
        outside.resolve().as_posix(),
    }

    plain = world.sources / "plain"
    plain_app = _monorepo(world, plain)
    world.apm("install", cwd=plain_app)
    # No repository: the common ancestor (plain/) is below /, HOME and its ancestors.
    plain_text = (plain_app / "apm.lock.yaml").read_text(encoding="utf-8")
    assert str(plain.resolve().as_posix()) not in plain_text
    rows = _transitive_rows(plain_app)
    assert rows["lib-b"]["anchored_local_path"] == "../../packages/lib-b"


def test_absolute_lock_is_rekeyed_in_place_once(tmp_path: Path, apm_binary_path: Path) -> None:
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    repo = world.sources / "R"
    app = _monorepo(world, repo)
    _install_before_5418(world, repo, app)
    legacy, legacy_owners, legacy_slots = lock_bytes(app), _owners(app), _slots(app)
    absolute = {name: row["anchored_local_path"] for name, row in _transitive_rows(app).items()}
    assert all(Path(anchor).is_absolute() for anchor in absolute.values()), absolute
    deployed = _deployed(app)
    world.git(repo, "init", "-q", "--initial-branch=main")

    # Read path, on a second app in the same repository holding the same lock:
    # accepted as locked, nothing written.
    reader = repo / "apps" / "reader"
    shutil.copytree(app, reader, symlinks=True)
    world.apm("install", "--frozen", cwd=reader)
    world.audit_clean(cwd=reader)
    assert lock_bytes(reader) == legacy
    assert _deployed(reader) == deployed
    assert _slots(app) == legacy_slots

    # Write path: one install re-keys the rows in place.
    world.apm("install", cwd=app)
    rows = _transitive_rows(app)
    assert rows["lib-b"]["anchored_local_path"] == "../../packages/lib-b"
    assert rows["lib-c"]["anchored_local_path"] == "../../packages/lib-c"
    assert rows["lib-c"]["declaring_parent"] == "local:../../packages/lib-b"
    renames = {
        f"local:{absolute[name]}": f"local:{rows[name]['anchored_local_path']}" for name in rows
    }
    assert _owners(app) == [
        (value, tuple(renames.get(owner, owner) for owner in owners), renames.get(active, active))
        for value, owners, active in legacy_owners
    ]
    assert _deployed(app) == deployed, "a re-key must not redeploy or delete a file"
    slots = _slots(app)
    assert len(slots) == len(legacy_slots)
    assert not (legacy_slots - {"lib-a"}) & slots, "the legacy slot must move, not stay"

    rekeyed = (app / "apm.lock.yaml").read_bytes()
    world.apm("install", cwd=app)
    assert (app / "apm.lock.yaml").read_bytes() == rekeyed, "I2: the re-key must happen once"
    assert _slots(app) == slots
    world.apm("install", "--frozen", cwd=app)
    world.audit_clean(cwd=app)


def _legacy_mcp_lock(tmp_path: Path, apm_binary_path: Path) -> tuple[ReplayWorld, Path, dict]:
    """A lock written before #5418 with a skills-only (--only apm) MCP omission."""
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    repo = world.sources / "R"
    app = _monorepo(world, repo, mcp=True)
    _install_before_5418(world, repo, app, "--only", "apm")
    evidence = load_yaml(app / "apm.lock.yaml")["mcp_omissions"]["echo-srv"]
    assert evidence["reason"] == "only-apm"
    world.git(repo, "init", "-q", "--initial-branch=main")
    return world, app, evidence


@pytest.mark.parametrize("command", [("lock",), ("deps", "update")])
def test_rekey_keeps_mcp_omission_bound(
    tmp_path: Path, apm_binary_path: Path, command: tuple[str, ...]
) -> None:
    """C2: evidence that matches the legacy recompute moves to the new key."""
    world, app, legacy = _legacy_mcp_lock(tmp_path, apm_binary_path)

    world.apm(*command, cwd=app)

    text = (app / "apm.lock.yaml").read_text(encoding="utf-8")
    assert "anchored_local_path: ../../packages/lib-b" in text
    evidence = load_yaml(app / "apm.lock.yaml")["mcp_omissions"]["echo-srv"]
    assert evidence["reason"] == "only-apm"
    assert evidence["declaration"] != legacy["declaration"]
    world.audit_clean(cwd=app)
    # The rebound value is the one a fresh portable install records.
    world.apm("install", "--only", "apm", cwd=app)
    assert (app / "apm.lock.yaml").read_text(encoding="utf-8") == text


def test_rekey_leaves_mismatched_mcp_omission_unbound(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """C2: a stored value that is not the legacy recompute is never rebound."""
    world, app, legacy = _legacy_mcp_lock(tmp_path, apm_binary_path)
    lock_path = app / "apm.lock.yaml"
    forged = "sha256:" + "0" * 64
    lock_path.write_text(
        lock_path.read_text(encoding="utf-8").replace(legacy["declaration"], forged),
        encoding="utf-8",
    )

    world.apm("lock", cwd=app)

    text = lock_path.read_text(encoding="utf-8")
    assert "anchored_local_path: ../../packages/lib-b" in text
    assert f"declaration: {forged}" in text
    audit = world.apm("audit", "--ci", "--no-policy", "--no-fail-fast", cwd=app, expect=1)
    output = audit.stdout + audit.stderr
    assert "omission evidence is missing, damaged or stale" in " ".join(output.split())
    assert "--only apm" in output


def _modules_state(app: Path) -> dict[str, bytes | None]:
    """Every apm_modules entry: file bytes, or None for a directory."""
    modules = app / "apm_modules"
    return {
        path.relative_to(modules).as_posix(): None if path.is_dir() else path.read_bytes()
        for path in sorted(modules.rglob("*"))
    }


def _legacy_lock(tmp_path: Path, apm_binary_path: Path) -> tuple[ReplayWorld, Path]:
    world = ReplayWorld.create(tmp_path, apm_binary_path)
    repo = world.sources / "R"
    app = _monorepo(world, repo)
    _install_before_5418(world, repo, app)
    world.git(repo, "init", "-q", "--initial-branch=main")
    return world, app


def test_declined_update_restores_the_moved_slot(tmp_path: Path, apm_binary_path: Path) -> None:
    """I7: an update that applies nothing leaves apm_modules and the lock as they were."""
    world, app = _legacy_lock(tmp_path, apm_binary_path)
    lock, modules = (app / "apm.lock.yaml").read_bytes(), _modules_state(app)

    world.apm("update", cwd=app)

    assert (app / "apm.lock.yaml").read_bytes() == lock
    assert _modules_state(app) == modules


def test_failed_install_after_rekey_restores_the_moved_slot(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """I7: an install that fails after the re-key leaves apm_modules and the lock as they were."""
    world, app = _legacy_lock(tmp_path, apm_binary_path)
    lock, modules = (app / "apm.lock.yaml").read_bytes(), _modules_state(app)
    manifest = app / "apm.yml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8") + "    - ../../packages/missing\n", encoding="utf-8"
    )

    failed = world.apm("install", cwd=app, expect=1)

    assert "missing" in failed.stdout + failed.stderr
    assert (app / "apm.lock.yaml").read_bytes() == lock
    assert _modules_state(app) == modules
