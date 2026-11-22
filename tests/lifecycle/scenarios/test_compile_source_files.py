"""``compile`` keeps a remote package's file primitive (other-file) (#4096).

PROMISE: P-INV-I1 -- after ``apm compile`` the lock still matches what is
deployed: a git or git-marketplace package's instruction stays under the
target's instruction root with its text and stays recorded in the lock; a
native Claude plugin's agent stays in its registered plugin with the lock's
fingerprint unchanged.
PROMISE: P-INV-I5 -- compile writes only its compiled context, never a
deployed primitive or the lock.

T-2 triples OP=compile x SRC={git, marketplace-git, native-plugin} x
PRIM=other-file. The world's remote packages ship skills only, so each row
serves its own package from a scenario-local git repository.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apm_cli.integration.targets import KNOWN_TARGETS
from tests.lifecycle.scenarios._kit import Scenario, credits, serve_repository
from tests.lifecycle.scenarios._sources import source_fields
from tests.lifecycle.world import use_policy
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
    pytest.mark.promise("P-INV-I1", "P-INV-I5"),
]

MARKER = "c-doc-marker-4096"
_DOC = "c-doc"
_NATIVE = "c-ndoc"
_MARKET = "c-doc-market"
_INSTRUCTION = f'---\napplyTo: "**"\n---\n\nFollow the {_DOC} conventions ({MARKER}).\n'
_AGENT = f"---\nname: {_NATIVE}\ndescription: Lifecycle agent.\n---\n\nAgent body ({MARKER}).\n"
_SOURCES = ("git", "marketplace-git", "native-plugin")


def _publish(world, name: str, tree: Path) -> str:
    """Serve *tree* as ``lifecycle/<name>`` tagged v1.0.0; return its shorthand."""
    repos = LocalGitRepositoryFactory(world.root / "repos", env=world.env)
    repository = repos.create(name, source_tree=tree)
    commit = repos.commit(repository, message=f"{name} 1.0.0")
    repos.tag(repository, "v1.0.0", commit)
    serve_repository(world, f"lifecycle/{name}", repository.origin)
    return f"lifecycle/{name}#v1.0.0"


def _doc_package(world) -> str:
    factory = LocalPackageFactory(world.root / "authored")
    package = factory.create(_DOC, version="1.0.0")
    factory.add_instruction(package, _DOC, _INSTRUCTION)
    return _publish(world, _DOC, package.root)


def _native_plugin(world) -> str:
    root = world.root / "authored" / _NATIVE
    (root / ".claude-plugin").mkdir(parents=True)
    plugin = {"name": _NATIVE, "version": "1.0.0", "description": "Lifecycle native agent."}
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps(plugin), encoding="ascii")
    (root / "agents").mkdir()
    (root / "agents" / f"{_NATIVE}.md").write_text(_AGENT, encoding="ascii")
    return _publish(world, _NATIVE, root)


def _catalog(world) -> str:
    """A git catalog listing the c-doc package; returns the URL ``marketplace add`` takes."""
    plugin = {
        "name": _DOC,
        "description": "Lifecycle instruction package.",
        "source": {"source": "github", "repo": f"lifecycle/{_DOC}", "ref": "v1.0.0"},
    }
    document = {"name": _MARKET, "owner": {"name": "APM Lifecycle"}, "plugins": [plugin]}
    tree = world.root / _MARKET
    (tree / ".claude-plugin").mkdir(parents=True)
    (tree / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps(document, indent=2) + "\n", encoding="ascii"
    )
    _publish(world, _MARKET, tree)
    return f"https://github.com/lifecycle/{_MARKET}"


def _scenario(world, src: str) -> tuple[Scenario, str]:
    use_policy(world, "allow")
    if src == "native-plugin":
        spec = _native_plugin(world)
        return Scenario(
            world,
            {_NATIVE: spec},
            kinds={_NATIVE: ("native",)},
            policy="allow",
            targets=("claude",),
        ), _NATIVE
    spec = _doc_package(world)
    if src == "marketplace-git":
        spec = f"{_DOC}@{_MARKET}"
    scenario = Scenario(world, {_DOC: spec}, kinds={_DOC: ("other-file",)}, policy="allow")
    if src == "marketplace-git":
        scenario.run(
            f"marketplace add {_catalog(world)}", forbidden={"project": ("*",)}, allowed={}
        )
    return scenario, _DOC


def _marked(world) -> dict[str, bytes]:
    """Every file under the project or HOME carrying MARKER (git metadata excluded)."""
    found = {}
    for label, root in (("project", world.project), ("home", world.home)):
        for path in root.rglob("*"):
            if not path.is_file() or ".git" in path.relative_to(root).parts:
                continue
            data = path.read_bytes()
            if MARKER.encode("ascii") in data:
                found[f"{label}:{path.relative_to(root).as_posix()}"] = data
    return found


def _assert_instruction(scenario: Scenario) -> str:
    """The instruction sits under copilot's instruction root, with its text, in the lock."""
    mapping = KNOWN_TARGETS["copilot"].primitives["instructions"]
    relative = f"{KNOWN_TARGETS['copilot'].root_dir}/{mapping.subdir}/{_DOC}{mapping.extension}"
    path = scenario.deploy_root / relative
    assert path.is_file(), f"{relative} is not deployed"
    assert MARKER in path.read_text(encoding="utf-8"), relative
    recorded = {p for dep in scenario.lock().get_package_dependencies() for p in dep.deployed_files}
    assert relative in recorded, f"{relative} is not recorded in the lock: {sorted(recorded)}"
    return relative


def _assert_native(scenario: Scenario) -> None:
    rows = [d for d in scenario.lock().get_package_dependencies() if d.native_plugin is not None]
    assert [d.native_plugin.name for d in rows] == [_NATIVE], rows


@pytest.mark.parametrize("src", _SOURCES)
def test_compile_keeps_a_remote_package_file_primitive(lifecycle_world, src) -> None:
    scenario, name = _scenario(lifecycle_world, src)
    scenario.run(f"install {name}", **source_fields(src))
    if src == "native-plugin":
        _assert_native(scenario)
    else:
        _assert_instruction(scenario)
    before = _marked(lifecycle_world)
    assert before, "the package's file primitive is deployed nowhere"
    lock_before = scenario.lock_path.read_bytes()

    step = scenario.run("compile", credit=[{"OP": "compile", "SRC": src, "PRIM": "other-file"}])

    assert step.result.returncode == 0, step.output
    assert scenario.lock_path.read_bytes() == lock_before, "compile never rewrites the lock"
    after = _marked(lifecycle_world)
    kept = {key: after.get(key) == data for key, data in before.items()}
    assert all(kept.values()), f"compile changed a deployed file primitive: {kept}"
    if src == "native-plugin":
        _assert_native(scenario)
    else:
        _assert_instruction(scenario)
    scenario.finish()


CREDITS = credits(
    __file__,
    {
        f"test_compile_keeps_a_remote_package_file_primitive[{src}]": [
            {"OP": "compile", "SRC": src, "PRIM": "other-file"}
        ]
        for src in _SOURCES
    },
)
