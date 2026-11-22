"""Org-policy field promises the generated corpus has no factor for yet (QL gap sweep).

PROMISE: P-INV-I4 -- an org policy restriction never widens what install deploys
(source: docs/src/content/docs/reference/policy-schema.md).
PROMISE: P-INV-I7 -- a refused or restricted install names its cause.

Each row authors its own packages and serves its own ``apm-policy.yml`` from a
unique org (``_serve_policy``), so the shared world variants stay untouched.
A promise row is paired with a control row that proves the fixture really
deploys what the promise restricts, so the promise row can never pass
vacuously. Rows whose promise is under private tracking are strict xfail.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from tests.lifecycle import driver
from tests.lifecycle.world import use_policy
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.lifecycle_smoke,
    pytest.mark.promise("P-INV-I4", "P-INV-I7"),
]


def _serve_policy(world, org: str, document: dict) -> None:
    """Serve *document* as ``<org>/.github/apm-policy.yml`` and point the project at *org*."""
    tree = world.root / f"policy-src-{org}"
    tree.mkdir()
    (tree / "apm-policy.yml").write_text(yaml.safe_dump(document), encoding="utf-8")
    world.services.publish(f"{org}/.github", tree, tag=None)
    git = ("git", "-C", str(world.project))
    if not (world.project / ".git").exists():
        subprocess.run((*git, "init", "-q", "--initial-branch=main"), env=world.env, check=True)
    remotes = subprocess.run(
        (*git, "remote"), env=world.env, capture_output=True, text=True, check=True
    ).stdout.split()
    verb = "set-url" if "origin" in remotes else "add"
    url = f"https://github.com/{org}/consumer.git"
    subprocess.run((*git, "remote", verb, "origin", url), env=world.env, check=True)


def _skill(factory: LocalPackageFactory, package, name: str) -> None:
    factory.add_skill(
        package, name, f"---\nname: {name}\ndescription: {name}.\n---\n\nUse {name}.\n"
    )


# A transitive MCP server reaches a project three ways: the one-run flag admits a
# self-defined one, an approval pins it, or a plain registry server needs neither.


def _text(result) -> str:
    return " ".join((result.stdout + result.stderr).split())


# -- G2: manifest.content_types (policy-schema.md, manifest section) ---------------


def _skill_and_instruction(world) -> str:
    factory = LocalPackageFactory(world.root / "g2")
    package = factory.create("t-typed", version="1.0.0")
    _skill(factory, package, "t-typed")
    factory.add_instruction(
        package, "t-typed", '---\napplyTo: "**"\n---\n\nKeep the typed instruction.\n'
    )
    return str(package.root)


def _instruction_deployed(world) -> bool:
    return any(
        path.is_file() and "apm_modules" not in path.parts
        for path in world.project.rglob("t-typed.instructions.md")
    )


def test_content_types_control_an_instruction_deploys(lifecycle_world) -> None:
    """Control: with no content-type restriction the package's instruction deploys."""
    use_policy(lifecycle_world, "allow")
    spec = _skill_and_instruction(lifecycle_world)
    result = driver.run(lifecycle_world, ["install", spec])
    assert result.returncode == 0, _text(result)
    assert _instruction_deployed(lifecycle_world), _text(result)


def test_content_types_allow_list_keeps_other_types_out(lifecycle_world) -> None:
    """manifest.content_types {allow: [skill]} -- an instruction is never deployed."""
    _serve_policy(
        lifecycle_world,
        "policy-g2-skills-only",
        {
            "name": "g2",
            "version": "1.0",
            "enforcement": "block",
            "manifest": {"content_types": {"allow": ["skill"]}},
        },
    )
    spec = _skill_and_instruction(lifecycle_world)
    project = lifecycle_world.project
    before = {
        name: (project / name).read_bytes() if (project / name).exists() else None
        for name in ("apm.yml", "apm.lock.yaml")
    }
    result = driver.run(lifecycle_world, ["install", spec])
    assert result.returncode == 1, _text(result)
    assert not _instruction_deployed(lifecycle_world), _text(result)
    # A refusal before any deploy write: no skill either, and apm.yml and the lock unchanged.
    assert not (lifecycle_world.project / ".agents/skills/t-typed").exists(), _text(result)
    after = {
        name: (project / name).read_bytes() if (project / name).exists() else None
        for name in before
    }
    assert after == before, "a refused install changed apm.yml or apm.lock.yaml"
    text = _text(result)
    assert "content_types" in text and "policy-g2-skills-only" in text, text
    assert "instruction" in text, text


_SKILLS_ONLY = {
    "name": "g2",
    "version": "1.0",
    "enforcement": "block",
    "manifest": {"content_types": {"allow": ["skill"]}},
}


def test_content_types_refuse_a_plugin_root_agent(lifecycle_world) -> None:
    """A plugin-format agents/ directory is an agent: allow [skill] refuses it before deploy."""
    _serve_policy(lifecycle_world, "policy-g2-root-agent", _SKILLS_ONLY)
    factory = LocalPackageFactory(lifecycle_world.root / "g2-agent")
    package = factory.create("t-agent", version="1.0.0")
    _skill(factory, package, "t-agent")
    agents = Path(package.root) / "agents"
    agents.mkdir()
    (agents / "reviewer.md").write_text("---\nname: reviewer\n---\n\nReview.\n", encoding="utf-8")
    result = driver.run(lifecycle_world, ["install", str(package.root)])
    assert result.returncode == 1, _text(result)
    deployed = [
        p for p in lifecycle_world.project.rglob("reviewer*") if "apm_modules" not in p.parts
    ]
    assert not deployed, deployed
    assert "content_types" in _text(result) and "agent" in _text(result), _text(result)


def test_audit_ci_fails_a_lock_that_ships_a_disallowed_content_type(lifecycle_world) -> None:
    """A package installed outside the policy still fails audit --ci under it."""
    _serve_policy(lifecycle_world, "policy-g2-audit", _SKILLS_ONLY)
    spec = _skill_and_instruction(lifecycle_world)
    installed = driver.run(lifecycle_world, ["install", "--no-policy", spec])
    assert installed.returncode == 0, _text(installed)
    assert _instruction_deployed(lifecycle_world), _text(installed)
    audit = driver.run(lifecycle_world, ["audit", "--ci"])
    assert audit.returncode != 0, _text(audit)
    assert "content-types" in _text(audit) and "instruction" in _text(audit), _text(audit)


def test_content_types_judge_a_local_source_not_its_stale_copy(lifecycle_world) -> None:
    """A local dependency that gains a disallowed kind is refused on a targeted install of
    another package, though its apm_modules copy predates the change."""
    _serve_policy(lifecycle_world, "policy-g2-stale-copy", _SKILLS_ONLY)
    factory = LocalPackageFactory(lifecycle_world.root / "g2-stale")
    first = factory.create("t-first", version="1.0.0")
    _skill(factory, first, "t-first")
    assert driver.run(lifecycle_world, ["install", str(first.root)]).returncode == 0
    hooks = Path(first.root) / ".apm" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "h.json").write_text('{"hooks": {}}\n', encoding="utf-8")
    second = factory.create("t-second", version="1.0.0")
    _skill(factory, second, "t-second")
    result = driver.run(lifecycle_world, ["install", str(second.root)])
    assert result.returncode == 1, _text(result)
    assert "content_types" in _text(result) and "hook" in _text(result), _text(result)
    assert not (lifecycle_world.project / ".agents/skills/t-second").exists(), _text(result)
