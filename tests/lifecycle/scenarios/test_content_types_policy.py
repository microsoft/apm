"""Org policy ``manifest.content_types``: which primitive kinds a package may ship (#4838).

PROMISE: P-INV-I4 -- an org policy restriction never widens what install deploys
(source: docs/src/content/docs/reference/policy-schema.md, manifest section).
PROMISE: P-INV-I7 -- a refused or restricted install names its cause.

The rows complement ``test_policy_promises.py`` (instruction, plugin-root agent,
audit and stale-copy rows). Each serves its own ``apm-policy.yml`` from a unique
org. A refusal under ``enforcement: block`` deploys nothing of the install and
leaves apm.yml and apm.lock.yaml unchanged; ``warn`` deploys and names the kind.
Kinds are counted by where a package ships them: ``.apm/<dir>/`` (an unnamed
directory is its own kind) and the components a plugin manifest declares.
Allow lists inherited through ``extends:`` only narrow, by kind, and ``audit --ci``
judges locked content even when apm_modules is gone. Each refusal
row is paired with an admitting row on the same fixture, so no row passes
vacuously.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.lifecycle import driver
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.lifecycle_smoke,
    pytest.mark.promise("P-INV-I4", "P-INV-I7"),
]

_HOOK = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo t"}]}]}}


def _publish_policy(world, org: str, document: dict) -> None:
    tree = world.root / f"policy-src-{org}"
    tree.mkdir()
    (tree / "apm-policy.yml").write_text(yaml.safe_dump(document), encoding="utf-8")
    world.services.publish(f"{org}/.github", tree, tag=None)


def _serve_policy(world, org: str, document: dict) -> None:
    """Serve *document* as ``<org>/.github/apm-policy.yml`` and point the project at *org*."""
    _publish_policy(world, org, document)
    git = ("git", "-C", str(world.project))
    if not (world.project / ".git").exists():
        subprocess.run((*git, "init", "-q", "--initial-branch=main"), env=world.env, check=True)
    remotes = subprocess.run(
        (*git, "remote"), env=world.env, capture_output=True, text=True, check=True
    ).stdout.split()
    verb = "set-url" if "origin" in remotes else "add"
    url = f"https://github.com/{org}/consumer.git"
    subprocess.run((*git, "remote", verb, "origin", url), env=world.env, check=True)


def _policy(allow: list[str] | None, *, enforcement: str = "block", **extra) -> dict:
    document = {"name": "ct", "version": "1.0", "enforcement": enforcement, **extra}
    if allow is not None:
        document["manifest"] = {"content_types": {"allow": allow}}
    return document


def _text(result) -> str:
    return " ".join((result.stdout + result.stderr).split())


def _package(world, name: str) -> tuple[LocalPackageFactory, object]:
    factory = LocalPackageFactory(world.root / f"ct-{name}")
    package = factory.create(name, version="1.0.0")
    factory.add_skill(
        package, name, f"---\nname: {name}\ndescription: {name}.\n---\n\nUse {name}.\n"
    )
    return factory, package


def _with_instruction(world, name: str) -> str:
    factory, package = _package(world, name)
    factory.add_instruction(package, name, '---\napplyTo: "**"\n---\n\nKeep it typed.\n')
    return str(package.root)


def _deployed(world, pattern: str) -> list[Path]:
    return [
        path
        for path in world.project.rglob(pattern)
        if path.is_file() and "apm_modules" not in path.parts
    ]


def _inputs(world) -> dict[str, bytes | None]:
    return {
        name: (world.project / name).read_bytes() if (world.project / name).exists() else None
        for name in ("apm.yml", "apm.lock.yaml")
    }


def _assert_refused(world, spec: str, kind: str) -> str:
    """The install exits 1 naming the policy field and *kind*; nothing of it deploys."""
    before = _inputs(world)
    result = driver.run(world, ["install", spec])
    text = _text(result)
    assert result.returncode == 1, text
    assert "content_types" in text and kind in text, text
    name = Path(spec).name
    assert not (world.project / ".agents/skills" / name).exists(), text
    assert _inputs(world) == before, "a refused install changed apm.yml or apm.lock.yaml"
    return text


def _gate_off(world) -> None:
    """No ``executables`` block: the executables gate is off, so only the policy decides."""
    manifest = world.project / "apm.yml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data.pop("executables", None)
    manifest.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _hooks(world, name: str) -> list[Path]:
    return sorted((world.project / ".github" / "hooks").glob(f"{name}-*.json"))


@pytest.mark.parametrize("enforcement", ["block", "warn"])
def test_a_hook_package_meets_a_skill_only_allow_list(lifecycle_world, enforcement) -> None:
    """``.apm/hooks/`` is the hook kind: block refuses the package, warn deploys and names it."""
    world = lifecycle_world
    _gate_off(world)
    _serve_policy(
        world, f"policy-ct-hook-{enforcement}", _policy(["skill"], enforcement=enforcement)
    )
    factory, package = _package(world, "t-hooked")
    factory.add_hook(package, "session", _HOOK)
    spec = str(package.root)
    if enforcement == "block":
        text = _assert_refused(world, spec, "hook")
        assert not _hooks(world, "t-hooked"), text
        return
    result = driver.run(world, ["install", spec])
    text = _text(result)
    assert result.returncode == 0, text
    assert "content" in text and "hook" in text, text
    assert (world.project / ".agents/skills/t-hooked").is_dir(), text
    assert _hooks(world, "t-hooked"), f"warn must deploy the hook too: {text}"


@pytest.mark.parametrize(
    ("allow", "admitted"),
    [(["skill"], False), (["skills", "rules"], True)],
    ids=["unnamed-kind-refused", "unnamed-kind-allow-listed"],
)
def test_an_unnamed_source_directory_is_its_own_kind(lifecycle_world, allow, admitted) -> None:
    """``.apm/rules/`` has no content_types name: it is refused unless the allow list names it."""
    world = lifecycle_world
    org = "policy-ct-rules-" + ("in" if admitted else "out")
    _serve_policy(world, org, _policy(allow))
    _factory, package = _package(world, "t-rules")
    rules = Path(package.root) / ".apm" / "rules"
    rules.mkdir(parents=True)
    (rules / "style.md").write_text("Prefer small functions.\n", encoding="utf-8")
    spec = str(package.root)
    if not admitted:
        _assert_refused(world, spec, "rules")
        return
    result = driver.run(world, ["install", spec])
    assert result.returncode == 0, _text(result)
    assert (world.project / ".agents/skills/t-rules").is_dir(), _text(result)


def test_a_plugin_manifest_component_at_a_custom_path_counts(lifecycle_world) -> None:
    """Agents a plugin manifest declares outside ``agents/`` are still the agent kind."""
    world = lifecycle_world
    _serve_policy(world, "policy-ct-plugin-path", _policy(["skill"]))
    _factory, package = _package(world, "t-plugin")
    root = Path(package.root)
    (root / "custom").mkdir()
    (root / "custom" / "reviewer.md").write_text(
        "---\nname: reviewer\n---\n\nReview.\n", encoding="utf-8"
    )
    manifest = {"name": "t-plugin", "version": "1.0.0", "agents": ["./custom/reviewer.md"]}
    (root / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    _assert_refused(world, str(root), "agent")
    assert not _deployed(world, "reviewer*"), _deployed(world, "reviewer*")


@pytest.mark.parametrize(
    ("parent", "child", "refused"),
    [
        (["instruction", "skill"], ["skills"], True),
        (["skill"], None, True),
        (["instructions", "skill"], ["instruction", "skills"], False),
    ],
    ids=["child-narrows", "child-silent-keeps-parent", "aliases-agree"],
)
def test_an_inherited_allow_list_only_narrows_by_kind(
    lifecycle_world, parent, child, refused
) -> None:
    """``extends:`` intersects allow lists by kind; a child without one keeps the parent's."""
    world = lifecycle_world
    case = "-".join([*parent, "x", *(child or ["none"])])
    parent_org = f"policy-ct-parent-{case}"
    _publish_policy(world, parent_org, _policy(parent))
    leaf = _policy(child, extends=f"{parent_org}/.github")
    _serve_policy(world, f"policy-ct-leaf-{case}", leaf)
    spec = _with_instruction(world, "t-inherit")
    if refused:
        _assert_refused(world, spec, "instruction")
        assert not _deployed(world, "t-inherit.instructions.md")
        return
    result = driver.run(world, ["install", spec])
    assert result.returncode == 0, _text(result)
    assert _deployed(world, "t-inherit.instructions.md"), _text(result)


def test_deleting_apm_modules_does_not_hide_a_disallowed_kind_from_audit(lifecycle_world) -> None:
    """``audit --ci`` judges the locked packages' content even when apm_modules is gone:
    it rebuilds the locked package and names the kind it ships."""
    world = lifecycle_world
    _serve_policy(world, "policy-ct-no-modules", _policy(["skill"]))
    installed = driver.run(world, ["install", "--no-policy", world.packages["p-exec:git"].spec])
    assert installed.returncode == 0, _text(installed)
    shutil.rmtree(world.project / "apm_modules")
    audit = driver.run(world, ["audit", "--ci"])
    text = _text(audit)
    assert audit.returncode == 1, text
    assert "content-types" in text, text
    assert "lifecycle/p-exec: ships hook; org policy manifest.content_types allows only: skill" in (
        text
    ), text
