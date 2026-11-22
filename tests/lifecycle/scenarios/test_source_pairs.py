"""Package sources crossed with the gate, primitives and commands (#4096).

PROMISE: P-INV-I4 -- the executables gate decides what deploys the same way
for every source: a package from a marketplace, the package registry or a
native plugin deploys its executables only once approved, never while denied,
and loses them once the approval is revoked (source:
docs/src/content/docs/enterprise/executables.md, install/exec_gate.py).
PROMISE: P-INV-I5 -- a catalog command (``marketplace add``) writes nothing in
the project at any gate level; ``compile`` and ``lock`` over a local bundle
keep the deployed files and the bundle's lock row.

T-2 pairs: SRC x GATE, SRC x PRIM and OP x SRC / OP x GATE. Credits name only
the two factors of the pair (plus context that implies no T-2 triple), so the
OP x SRC x PRIM triples stay with the source spine.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.lifecycle.scenarios._kit import Scenario, assert_changes, credits, serve_repository
from tests.lifecycle.scenarios._sources import source_fields
from tests.lifecycle.world import registry, use_policy, use_registry
from tests.lifecycle.world.packages import INERT_COMMAND
from tests.utils.local_git_repository import LocalGitRepositoryFactory

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("P-INV-I4", "P-INV-I5"),
]

# -- a primitive from each source (scenario-local publications) ---------------------

_CANVAS_FILE = ".github/extensions/p-canvas/extension.mjs"


def _approved_install(scenario: Scenario, name: str, *credit: dict[str, str], **fields) -> object:
    scenario.run(f"install {name}", **fields)
    scenario.run(f"approve {name}")
    step = scenario.run("install", credit=list(credit), **fields)
    scenario.assert_deployed()
    return step


def _serve_git(world, name: str) -> str:
    """Serve world package *name* from git as ``lifecycle/<name>`` at v1.0.0; return the spec."""
    repos = LocalGitRepositoryFactory(world.root / "repos", env=world.env)
    repository = repos.create(name, source_tree=Path(world.packages[name].spec))
    repos.tag(repository, "v1.0.0", repos.commit(repository, message=f"{name} v1.0.0"))
    serve_repository(world, f"lifecycle/{name}", repository.origin)
    return f"lifecycle/{name}#v1.0.0"


def test_a_canvas_from_git_deploys_once_approved(lifecycle_world) -> None:
    use_policy(lifecycle_world, "allow")
    spec = _serve_git(lifecycle_world, "p-canvas")
    scenario = Scenario(lifecycle_world, {"p-canvas": spec}, policy="allow")
    _approved_install(scenario, "p-canvas", {"SRC": "git", "PRIM": "canvas"})
    assert (lifecycle_world.project / _CANVAS_FILE).is_file()
    scenario.finish()


_CANVAS_MARKET = "canvas-market"


def test_a_canvas_from_a_git_marketplace_deploys_once_approved(lifecycle_world) -> None:
    use_policy(lifecycle_world, "allow")
    _serve_git(lifecycle_world, "p-canvas")
    catalog = lifecycle_world.root / _CANVAS_MARKET
    plugin = {
        "name": "p-canvas",
        "description": "Lifecycle world package p-canvas",
        "source": {"source": "github", "repo": "lifecycle/p-canvas", "ref": "v1.0.0"},
    }
    document = {"name": _CANVAS_MARKET, "owner": {"name": "APM Lifecycle"}, "plugins": [plugin]}
    (catalog / ".claude-plugin").mkdir(parents=True)
    (catalog / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps(document, indent=2) + "\n", encoding="ascii"
    )
    repos = LocalGitRepositoryFactory(lifecycle_world.root / "repos", env=lifecycle_world.env)
    repository = repos.create(_CANVAS_MARKET, source_tree=catalog)
    repos.commit(repository, message="canvas catalog")
    serve_repository(lifecycle_world, f"lifecycle/{_CANVAS_MARKET}", repository.origin)
    scenario = Scenario(lifecycle_world, {"p-canvas": f"p-canvas@{_CANVAS_MARKET}"}, policy="allow")
    scenario.run(
        f"marketplace add https://github.com/lifecycle/{_CANVAS_MARKET}",
        forbidden={"project": ("*",)},
        allowed={},
    )
    fields = source_fields("marketplace-git")
    _approved_install(scenario, "p-canvas", {"SRC": "marketplace-git", "PRIM": "canvas"}, **fields)
    assert (lifecycle_world.project / _CANVAS_FILE).is_file()
    scenario.finish()


_REGISTRY_PRIMS = {"p-canvas": "canvas", "p-lsp": "lsp"}


@pytest.mark.parametrize("name", sorted(_REGISTRY_PRIMS))
def test_a_registry_package_deploys_its_executable_once_approved(lifecycle_world, name) -> None:
    use_policy(lifecycle_world, "allow")
    root = Path(lifecycle_world.packages[name].spec)
    lifecycle_world.services.package_registry.publish(
        f"lifecycle/{name}", "1.0.0", registry.zip_tree(root)
    )
    use_registry(lifecycle_world)
    scenario = Scenario(lifecycle_world, {name: f"lifecycle/{name}#1.0.0"}, policy="allow")
    _approved_install(scenario, name, {"SRC": "pkg-registry", "PRIM": _REGISTRY_PRIMS[name]})
    fetched = [
        entry["path"]
        for entry in lifecycle_world.services.egress_log()
        if entry["host"] == registry.HOST
    ]
    assert any(name in path for path in fetched), fetched
    scenario.finish()


_NATIVE = "p-native-hooks"
_NATIVE_HOOK = "echo p-native-hooks"
_NATIVE_LSP = "p-native-hooks-server"


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="ascii")


def _native_plugin(world) -> Path:
    """A Claude plugin that carries one hook and one LSP server."""
    root = world.root / _NATIVE
    plugin = {"name": _NATIVE, "version": "1.0.0", "description": "Hooks and an LSP server."}
    _write_json(root / ".claude-plugin" / "plugin.json", plugin)
    hook = {"type": "command", "command": _NATIVE_HOOK}
    _write_json(root / "hooks" / "hooks.json", {"hooks": {"SessionStart": [{"hooks": [hook]}]}})
    server = {"command": INERT_COMMAND, "args": ["--stdio"], "extensionToLanguage": {".x": "x"}}
    _write_json(root / ".lsp.json", {_NATIVE_LSP: server})
    return root


def test_a_native_plugins_hook_and_lsp_deploy_once_approved(lifecycle_world) -> None:
    use_policy(lifecycle_world, "allow")
    root = _native_plugin(lifecycle_world)
    scenario = Scenario(
        lifecycle_world,
        {_NATIVE: str(root)},
        kinds={_NATIVE: ("native",)},
        targets=("claude",),
        policy="allow",
    )
    credit = [{"SRC": "native-plugin", "PRIM": prim} for prim in ("hook", "lsp")]
    step = _approved_install(scenario, _NATIVE, *credit)
    settings = (lifecycle_world.project / ".claude/settings.json").read_text(encoding="utf-8")
    assert _NATIVE_HOOK in settings, settings
    lsp = lifecycle_world.project / ".claude/skills/apm-lsp/.claude-plugin/plugin.json"
    assert _NATIVE_LSP in lsp.read_text(encoding="utf-8"), step.output
    scenario.finish()


# The bundle packs p-exec, so its files carry that package name.
_BUNDLE_KINDS = {"p-exec": ("hook", "mcp", "bin")}
_BUNDLE_OPS = {"compile": "compile", "lock": "lock"}


@pytest.mark.parametrize("op", sorted(_BUNDLE_OPS))
def test_compile_and_lock_keep_an_installed_local_bundle(lifecycle_world, op) -> None:
    use_policy(lifecycle_world, "allow")
    specs = {"p-exec": lifecycle_world.packages["p-exec:bundle"].spec}
    scenario = Scenario(lifecycle_world, specs, policy="allow", kinds=_BUNDLE_KINDS)
    first = scenario.run("install p-exec")
    hint = re.search(r"apm approve '([^']+)'", first.output)
    assert hint, first.output
    approved = scenario.model.approve("p-exec", _BUNDLE_KINDS["p-exec"])
    scenario.run(("approve", hint.group(1)), requested=approved)
    scenario.run("install p-exec")
    lock_before = scenario.lock_path.read_bytes()
    step = scenario.run(_BUNDLE_OPS[op], credit=[{"OP": op, "SRC": "local-bundle"}])
    assert step.result.returncode == 0, step.output
    scenario.assert_deployed()
    assert_changes(
        [item for item in step.changed if not item.endswith("apm.lock.yaml")],
        (),
        label=op,
    )
    # A local bundle has no dependency row: its deployment and MCP provenance carry it.
    lock_text = scenario.lock_path.read_text(encoding="utf-8")
    assert "active_owner: local-bundle" in lock_text, lock_text
    assert "p-exec-mcp: bundle:p-exec" in lock_text, lock_text
    if op == "compile":
        assert scenario.lock_path.read_bytes() == lock_before, "compile never rewrites the lock"
    scenario.finish()


CREDITS = credits(
    __file__,
    {
        "test_a_canvas_from_git_deploys_once_approved": [{"SRC": "git", "PRIM": "canvas"}],
        "test_a_canvas_from_a_git_marketplace_deploys_once_approved": [
            {"SRC": "marketplace-git", "PRIM": "canvas"}
        ],
        **{
            f"test_a_registry_package_deploys_its_executable_once_approved[{name}]": [
                {"SRC": "pkg-registry", "PRIM": prim}
            ]
            for name, prim in _REGISTRY_PRIMS.items()
        },
        "test_a_native_plugins_hook_and_lsp_deploy_once_approved": [
            {"SRC": "native-plugin", "PRIM": prim} for prim in ("hook", "lsp")
        ],
        **{
            f"test_compile_and_lock_keep_an_installed_local_bundle[{op}]": [
                {"OP": op, "SRC": "local-bundle"}
            ]
            for op in _BUNDLE_OPS
        },
    },
)
