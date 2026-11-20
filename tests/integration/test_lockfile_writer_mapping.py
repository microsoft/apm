"""Component proofs for the lockfile write authority's closed writer set (#4086).

Runs every real lock-writing command in process and records which writer it
declared, whether it opened a write scope (its first staged lockfile write),
and how many times it persisted ``apm.lock.yaml``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.deps import lockfile_write_authority as authority
from apm_cli.deps.lockfile_write_authority import LockfileWriter
from apm_cli.utils import atomic_io
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_package import LocalPackage, LocalPackageFactory

pytestmark = [pytest.mark.integration, pytest.mark.component, pytest.mark.promise("P-INV-I1")]

_LOCKFILE = "apm.lock.yaml"
_SERVER = {
    "name": "mapping-server",
    "registry": False,
    "transport": "stdio",
    "command": "python",
    "args": ["-m", "mapping_server"],
}
_LSP = {
    "name": "mapping-language",
    "command": "inert-language-server",
    "extensionToLanguage": {".fixture": "fixture"},
}
_SKILL = "---\nname: mapping-skill\ndescription: Writer mapping fixture\n---\n# Skill\n"
_HTTP_MCP = ("--transport", "http", "--url", "https://mcp.example.invalid/mcp")
_NO_POLICY = ("--no-policy", "--trust-transitive-mcp")


@dataclass
class _Recorder:
    declared: list[LockfileWriter] = field(default_factory=list)
    opened: list[LockfileWriter] = field(default_factory=list)
    persisted: int = 0

    def clear(self) -> None:
        self.declared.clear()
        self.opened.clear()
        self.persisted = 0


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    """Record declared writers, opened write scopes and lockfile persists."""
    record = _Recorder()
    original_scope = authority.lockfile_write_scope
    original_class = authority._WriteScope
    original_write = atomic_io.atomic_write_text

    @contextmanager
    def declaring(writer: LockfileWriter) -> Iterator[None]:
        if authority._active_writer.get() is None:
            record.declared.append(writer)
        with original_scope(writer):
            yield

    class _Opening(original_class):  # type: ignore[misc, valid-type]
        def __init__(self, writer: LockfileWriter) -> None:
            super().__init__(writer)
            record.opened.append(writer)

    def persisting(path: Path, *args: object, **kwargs: object) -> None:
        if Path(path).name == _LOCKFILE:
            record.persisted += 1
        original_write(path, *args, **kwargs)

    monkeypatch.setattr(authority, "lockfile_write_scope", declaring)
    monkeypatch.setattr(authority, "_WriteScope", _Opening)
    monkeypatch.setattr(atomic_io, "atomic_write_text", persisting)
    return record


@dataclass(frozen=True)
class _World:
    environment: dict[str, str]
    packages: LocalPackageFactory
    leaf: LocalPackage
    project: LocalPackage
    consumer: LocalPackage


def _world(root: Path) -> _World:
    isolated = IsolatedApmEnvironment.create(root, base_env=dict(os.environ))
    packages = LocalPackageFactory(isolated.work_root)
    leaf = packages.create("mapping-leaf", targets=("copilot",), mcp_dependencies=(_SERVER,))
    packages.add_skill(leaf, "mapping-skill", _SKILL)
    project = packages.create(
        "mapping-project", targets=("copilot", "claude"), lsp_dependencies=(_LSP,)
    )
    dependencies = LocalPackageFactory(project.root / "packages")
    dependency = dependencies.create("mapping-dep", mcp_dependencies=(_SERVER,))
    dependencies.add_skill(dependency, "mapping-skill", _SKILL)
    packages.replace_apm_dependencies(
        project, ({"path": "./packages/mapping-dep", "alias": dependency.name},)
    )
    consumer = packages.create("mapping-consumer", targets=("copilot",))
    return _World(isolated.subprocess_env(), packages, leaf, project, consumer)


def _invoke(root: Path, environment: dict[str, str], *args: str) -> int:
    previous = Path.cwd()
    os.chdir(root)
    try:
        result = CliRunner().invoke(cli, list(args), env=environment, catch_exceptions=False)
    finally:
        os.chdir(previous)
    return result.exit_code


def _ok(root: Path, environment: dict[str, str], *args: str) -> None:
    assert _invoke(root, environment, *args) == 0, args


def _bundle(world: _World) -> Path:
    _ok(world.leaf.root, world.environment, "install", *_NO_POLICY)
    _ok(world.leaf.root, world.environment, "pack", "-o", "dist")
    (bundle,) = (world.leaf.root / "dist").iterdir()
    return bundle


def _apm_bundle(world: _World) -> Path:
    """An APM-format bundle, the input ``apm unpack`` reads."""
    _ok(world.leaf.root, world.environment, "pack", "--format", "apm", "-o", "dist-apm")
    (bundle,) = (world.leaf.root / "dist-apm").iterdir()
    return bundle


def _writer_steps(
    world: _World, bundle: Path, apm_bundle: Path
) -> list[tuple[tuple[str, ...], Path, LockfileWriter]]:
    root = world.project.root
    return [
        (("install", *_NO_POLICY), root, LockfileWriter.INSTALL),
        (("lock", "--no-policy"), root, LockfileWriter.LOCK),
        (("install", "--mcp", "direct-api", *_HTTP_MCP), root, LockfileWriter.INSTALL_MCP),
        (("mcp", "install", "alias-api", *_HTTP_MCP), root, LockfileWriter.INSTALL_MCP),
        (("deps", "update"), root, LockfileWriter.UPDATE),
        (("update", "--yes"), root, LockfileWriter.UPDATE),
        (("compile",), root, LockfileWriter.COMPILE),
        (("uninstall", "./packages/mapping-dep"), root, LockfileWriter.UNINSTALL),
        (("prune",), root, LockfileWriter.PRUNE),
        (
            ("install", str(bundle), "--no-policy"),
            world.consumer.root,
            LockfileWriter.INSTALL_BUNDLE,
        ),
        (("unpack", str(apm_bundle), "--force"), world.consumer.root, LockfileWriter.UNPACK),
    ]


def test_each_lock_writing_command_maps_to_its_registered_writer(
    tmp_path: Path, recorder: _Recorder
) -> None:
    world = _world(tmp_path / "mapping")
    steps = _writer_steps(world, _bundle(world), _apm_bundle(world))

    for args, cwd, writer in steps:
        recorder.clear()
        _ok(cwd, world.environment, *args)
        assert recorder.declared == [writer], (args, recorder.declared)
        assert recorder.persisted <= 1, (args, recorder.persisted)

    assert set(LockfileWriter) == {writer for _args, _cwd, writer in steps}


def test_install_with_mcp_and_lsp_persists_the_lockfile_once(
    tmp_path: Path, recorder: _Recorder
) -> None:
    world = _world(tmp_path / "services")
    root = world.project.root

    _ok(root, world.environment, "install", *_NO_POLICY)

    lock = load_yaml(root / _LOCKFILE)
    assert lock.get("mcp_servers") and lock.get("lsp_servers"), lock
    assert recorder.opened == [LockfileWriter.INSTALL]
    assert recorder.persisted == 1


def test_newer_generator_lockfile_is_refused_before_any_writer_scope_opens(
    tmp_path: Path, recorder: _Recorder
) -> None:
    world = _world(tmp_path / "refusal")
    bundle = _bundle(world)
    apm_bundle = _apm_bundle(world)
    _ok(world.project.root, world.environment, "install", *_NO_POLICY)
    world.packages.set_targets(world.project, ("copilot",))
    for root in (world.project.root, world.consumer.root):
        path = root / _LOCKFILE
        lock = load_yaml(path) if path.exists() else {"lockfile_version": "1", "dependencies": []}
        lock["apm_version"] = "99.0.0"
        dump_yaml(lock, path)

    for args, cwd, writer in _writer_steps(world, bundle, apm_bundle):
        before = _tree(cwd)
        recorder.clear()
        assert _invoke(cwd, world.environment, *args) != 0, args
        assert recorder.opened == [], (writer, recorder.opened)
        assert recorder.persisted == 0, writer
        assert _tree(cwd) == before, writer


def _tree(root: Path) -> dict[str, bytes]:
    """Every file APM could change under *root*, excluding the module store."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and "apm_modules" not in path.relative_to(root).parts
    }
