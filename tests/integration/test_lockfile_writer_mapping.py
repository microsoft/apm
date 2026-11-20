"""Every lockfile-writing command opens its registered writer scope (#4086).

The lockfile write authority keeps a closed writer set; this proves the
mapping from each real lock-writing command to its ``LockfileWriter`` by
running the commands in process and recording which writer scope each one
opened, alongside whether it committed.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.deps import lockfile_write_authority as authority
from apm_cli.deps.lockfile_write_authority import LockfileWriter
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_package import LocalPackageFactory

pytestmark = pytest.mark.integration

_SERVER = {
    "name": "mapping-server",
    "registry": False,
    "transport": "stdio",
    "command": "python",
    "args": ["-m", "mapping_server"],
}
_SKILL = "---\nname: mapping-skill\ndescription: Writer mapping fixture\n---\n# Skill\n"
_HTTP_MCP = ("--transport", "http", "--url", "https://mcp.example.invalid/mcp")


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[LockfileWriter]:
    """Record the writer of every outermost scope a command opens."""
    writers: list[LockfileWriter] = []
    original = authority._WriteScope

    class _RecordingScope(original):  # type: ignore[misc, valid-type]
        def __init__(self, writer: LockfileWriter) -> None:
            super().__init__(writer)
            writers.append(writer)

    monkeypatch.setattr(authority, "_WriteScope", _RecordingScope)
    return writers


def _invoke(root: Path, environment: dict[str, str], *args: str) -> None:
    previous = Path.cwd()
    os.chdir(root)
    try:
        result = CliRunner().invoke(cli, list(args), env=environment, catch_exceptions=False)
    finally:
        os.chdir(previous)
    assert result.exit_code == 0, (args, result.output)


def test_each_lock_writing_command_maps_to_its_registered_writer(
    tmp_path: Path, opened: list[LockfileWriter]
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "mapping", base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    packages = LocalPackageFactory(isolated.work_root)

    leaf = packages.create("mapping-leaf", targets=("copilot",), mcp_dependencies=(_SERVER,))
    packages.add_skill(leaf, "mapping-skill", _SKILL)
    project = packages.create("mapping-project", targets=("copilot", "claude"))
    dependencies = LocalPackageFactory(project.root / "packages")
    dependency = dependencies.create("mapping-dep", mcp_dependencies=(_SERVER,))
    dependencies.add_skill(dependency, "mapping-skill", _SKILL)
    packages.replace_apm_dependencies(
        project, ({"path": "./packages/mapping-dep", "alias": dependency.name},)
    )
    consumer = packages.create("mapping-consumer", targets=("copilot",))
    root = project.root
    no_policy = ("--no-policy", "--trust-transitive-mcp")

    steps: list[tuple[tuple[str, ...], Path, LockfileWriter]] = [
        (("install", *no_policy), root, LockfileWriter.INSTALL),
        (("lock", "--no-policy"), root, LockfileWriter.LOCK),
        (("install", "--mcp", "direct-api", *_HTTP_MCP), root, LockfileWriter.INSTALL_MCP),
        (("mcp", "install", "alias-api", *_HTTP_MCP), root, LockfileWriter.INSTALL_MCP),
        (("deps", "update"), root, LockfileWriter.UPDATE),
        (("update", "--yes"), root, LockfileWriter.UPDATE),
        (("compile",), root, LockfileWriter.COMPILE),
        (("uninstall", "./packages/mapping-dep"), root, LockfileWriter.UNINSTALL),
        (("prune",), root, LockfileWriter.PRUNE),
    ]
    for args, cwd, writer in steps:
        opened.clear()
        _invoke(cwd, environment, *args)
        assert opened == [writer], (args, opened)

    _invoke(leaf.root, environment, "install", *no_policy)
    _invoke(leaf.root, environment, "pack", "-o", "dist")
    (bundle,) = (leaf.root / "dist").iterdir()
    opened.clear()
    _invoke(consumer.root, environment, "install", str(bundle), "--no-policy")
    assert opened == [LockfileWriter.INSTALL_BUNDLE]
    assert set(LockfileWriter) == {writer for _args, _cwd, writer in steps} | {
        LockfileWriter.INSTALL_BUNDLE
    }
