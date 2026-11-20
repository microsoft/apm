"""Lifecycle scenarios that depend on the platform (#4102).

PROMISE: P-INV-I1..I9 -- every lifecycle step keeps the cross-cutting laws on
each filesystem and OS lane (source: tests/lifecycle/oracles.py).
PROMISE: P-OWN-dependency-identity-materialization -- a casing-only rename of a
dependency keeps one identity and one materialization path, so prune keeps it
(source: .apm/architecture/owners/contracts-tooling.json).
PROMISE: P-OWN-url-path-containment-decoding -- install never writes through a
symlinked deploy directory (source: .apm/architecture/owners/transport-auth-platform.json).
PROMISE: P-LAW-ownership.preserve_unowned -- a case-variant user file survives.

Rows run on the lanes that can express them. A row that needs a capability
the lane lacks (a case-insensitive or case-sensitive filesystem, symbolic
links, long paths, a Windows standard user) is skipped with the reason named
in its ``skipif``; the probes run once at collection on the temp filesystem,
where pytest places every lifecycle world.

CELL: OP={install,prune,uninstall} SRC={git,local-path} PRIM=skill SCOPE={project,user}
"""

from __future__ import annotations

import ctypes
import os
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

from tests.lifecycle.scenarios._kit import Scenario, Tree, assert_changes, known_defect
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.lifecycle_smoke,
    pytest.mark.platform_sensitive,
    pytest.mark.promise(
        "P-INV-I1",
        "P-INV-I2",
        "P-INV-I3",
        "P-INV-I5",
        "P-INV-I7",
        "P-OWN-dependency-identity-materialization",
        "P-OWN-url-path-containment-decoding",
        "P-LAW-ownership.preserve_unowned",
    ),
]


# -- capability probes (collection time, on the filesystem of pytest's temp root) --


def _case_insensitive_fs() -> bool:
    """Same probe as tests/integration/test_prune_casing_cli.py, on the temp root."""
    with tempfile.TemporaryDirectory() as directory:
        (Path(directory) / "CaseProbe").mkdir()
        return (Path(directory) / "caseprobe").exists()


def _symlinks_supported() -> bool:
    with tempfile.TemporaryDirectory() as directory:
        try:
            (Path(directory) / "link").symlink_to(Path(directory))
        except (OSError, NotImplementedError):
            return False
        return True


def _long_paths_supported() -> bool:
    with tempfile.TemporaryDirectory() as directory:
        deep = Path(directory).joinpath(*(["d" * 60] * 6))
        try:
            deep.mkdir(parents=True)
            (deep / "probe.md").write_text("probe\n", encoding="ascii")
        except OSError:
            return False
        return True


def _windows_standard_user() -> bool:
    if sys.platform != "win32":
        return False
    return not ctypes.windll.shell32.IsUserAnAdmin()


CASE_INSENSITIVE = _case_insensitive_fs()
SYMLINKS = _symlinks_supported()
LONG_PATHS = _long_paths_supported()
WINDOWS_STANDARD_USER = _windows_standard_user()

needs_symlinks = pytest.mark.skipif(
    not SYMLINKS,
    reason="lane cannot create symbolic links (Windows standard user without Developer Mode)",
)

# Paths every first install in a fresh world creates besides the package's own.
_FIRST_INSTALL = (
    "project:apm.yml",
    "project:apm.lock.yaml",
    "project:.gitignore",
    "project:.github",
    "project:.apm-local*",
    "project:apm_modules*",
)
_SKILL_ROOT = "project:.agents/skills"


def _skill_package(world, name: str) -> Path:
    factory = LocalPackageFactory(world.root / "platform-packages")
    package = factory.create(name, version="1.0.0")
    factory.add_skill(package, name, f"---\nname: {name}\ndescription: {name}.\n---\n\nBody.\n")
    return package.root


def _deployed_tree(scenario: Scenario, name: str) -> dict[str, bytes]:
    root = scenario.world.project / ".agents" / "skills" / name
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _rename_dependency(manifest: Path, old: str, new: str) -> None:
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["dependencies"]["apm"] = [new if item == old else item for item in data["dependencies"]["apm"]]
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")


# -- case folding ---------------------------------------------------------------


def test_casing_only_rename_keeps_package_through_prune_and_install(lifecycle_world) -> None:
    """A dependency renamed only by letter case is the same package: prune keeps
    it on every filesystem and the next install converges without a re-deploy."""
    spec = lifecycle_world.packages["p-skill:git"].spec
    scenario = Scenario(lifecycle_world, {"p-skill": spec})
    installed = scenario.run(["install", spec])
    assert_changes(
        installed.changed,
        (*_FIRST_INSTALL, "project:.agents*"),
        label="install",
    )
    deployed = _deployed_tree(scenario, "p-skill")

    renamed = spec.replace("lifecycle/p-skill", "Lifecycle/P-Skill")
    _rename_dependency(lifecycle_world.project / "apm.yml", spec, renamed)
    scenario.model = scenario.model.edit("apm.yml")
    scenario.specs["p-skill"] = renamed

    pruned = scenario.run("prune")
    assert_changes(pruned.changed, (), label="prune after a casing-only rename")
    assert "No orphaned packages found" in pruned.output

    reinstalled = scenario.run("install")
    assert _deployed_tree(scenario, "p-skill") == deployed
    assert len(scenario.lock_names()) == 1, scenario.lock_names()
    assert not any(path.startswith(_SKILL_ROOT) for path in reinstalled.changed), sorted(
        reinstalled.changed
    )
    scenario.run("install")  # converged: I2 byte no-op
    scenario.pair()
    scenario.assert_deployed()
    scenario.finish()


@pytest.mark.skipif(
    CASE_INSENSITIVE,
    reason="case-insensitive filesystem: the case-variant directory IS the deployed one",
)
def test_case_variant_user_skill_survives_on_case_sensitive_fs(lifecycle_world) -> None:
    scenario = Scenario(lifecycle_world, ["p-skill"])
    user = Tree(lifecycle_world.project)
    user.write(".agents/skills/P-Skill/SKILL.md", "user-owned skill with a case-variant name\n")

    installed = scenario.run("install p-skill")
    assert_changes(
        installed.changed,
        (*_FIRST_INSTALL, "project:.agents/skills/p-skill*"),
        label="install",
    )
    user.assert_intact("install")
    scenario.pair()
    removed = scenario.run("uninstall p-skill")
    assert_changes(
        removed.changed,
        (
            "project:apm.yml",
            "project:apm.lock.yaml",
            "project:.agents/skills/p-skill*",
            "project:apm_modules*",
        ),
        label="uninstall",
    )
    user.assert_intact("uninstall")
    scenario.assert_deployed()
    scenario.finish()


@pytest.mark.skipif(
    not CASE_INSENSITIVE,
    reason="needs a case-insensitive filesystem (macOS and Windows lanes)",
)
def test_case_variant_user_skill_blocks_install_on_case_insensitive_fs(lifecycle_world) -> None:
    scenario = Scenario(lifecycle_world, ["p-skill"])
    user = Tree(lifecycle_world.project)
    user.write(".agents/skills/P-Skill/SKILL.md", "user-owned skill with a case-variant name\n")

    refused = scenario.run("install p-skill", exit=1)
    assert_changes(refused.changed, (), label="refused install")
    assert "conflicts with unmanaged file" in refused.output, refused.output
    user.assert_intact("refused install")
    scenario.finish()


# -- long paths -----------------------------------------------------------------


@pytest.mark.skipif(
    not LONG_PATHS,
    reason="lane rejects paths over 260 characters (Windows without LongPathsEnabled)",
)
def test_long_paths_install_reinstall_and_uninstall(lifecycle_world) -> None:
    name = "p-long-" + "x" * 57  # the 64-character skill name limit
    source = _skill_package(lifecycle_world, name)
    deep = source / "skills" / name / "references" / ("d" * 60) / ("e" * 60) / ("f" * 60)
    deep.mkdir(parents=True)
    (deep / ("g" * 60 + ".md")).write_text("deep reference\n", encoding="ascii")
    scenario = Scenario(lifecycle_world, {name: str(source)}, kinds={name: ["skill"]})

    installed = scenario.run(["install", str(source)])
    assert_changes(
        installed.changed,
        (*_FIRST_INSTALL, "project:.agents*"),
        label="install",
    )
    files = _deployed_tree(scenario, name)
    reference = next(path for path in files if path.endswith(".md") and path != "SKILL.md")
    longest = lifecycle_world.project / ".agents" / "skills" / name / reference
    assert len(str(longest)) > 260, len(str(longest))
    assert files[reference] == b"deep reference\n"

    scenario.run("install")  # converged: I2 byte no-op
    scenario.pair()
    # The I3 pair runs before the last uninstall: with nothing declared,
    # install --frozen and audit --ci disagree (#4050, covered elsewhere).
    removed = scenario.run(["uninstall", str(source)])
    assert not (lifecycle_world.project / ".agents" / "skills" / name).exists()
    assert_changes(
        removed.changed,
        ("project:apm.yml", "project:apm.lock.yaml", "project:.agents*", "project:apm_modules*"),
        label="uninstall",
    )
    scenario.assert_deployed()
    scenario.finish()


# -- symbolic links ---------------------------------------------------------------


@needs_symlinks
def test_in_package_symlink_is_deployed_as_a_regular_file(lifecycle_world) -> None:
    source = _skill_package(lifecycle_world, "p-link")
    (source / "shared").mkdir()
    (source / "shared" / "ref.md").write_text("shared reference\n", encoding="ascii")
    (source / "skills" / "p-link" / "ref.md").symlink_to(Path("..", "..", "shared", "ref.md"))
    scenario = Scenario(lifecycle_world, {"p-link": str(source)}, kinds={"p-link": ["skill"]})

    scenario.run(["install", str(source)])
    deployed = lifecycle_world.project / ".agents" / "skills" / "p-link" / "ref.md"
    assert not deployed.is_symlink()
    assert deployed.read_bytes() == b"shared reference\n"
    scenario.run("install")  # converged: I2 byte no-op
    scenario.pair()
    scenario.assert_deployed()
    scenario.finish()


@needs_symlinks
def test_symlink_escaping_the_package_fails_closed(lifecycle_world) -> None:
    source = _skill_package(lifecycle_world, "p-link")
    outside = Tree(lifecycle_world.root)
    outside.write("outside/secret.md", "not part of any package\n")
    (source / "skills" / "p-link" / "ref.md").symlink_to(lifecycle_world.root / "outside" / "secret.md")
    scenario = Scenario(lifecycle_world, {"p-link": str(source)}, kinds={"p-link": ["skill"]})

    refused = scenario.run(["install", str(source)], exit=1)
    with known_defect("#4159", r"unexpected: \['project:\.github'\]\n  missing: \[\]$"):
        assert_changes(refused.changed, (), label="refused install")
    assert "escapes the package root" in refused.output, refused.output
    outside.assert_intact("refused install")
    scenario.finish()


@needs_symlinks
def test_symlinked_deploy_directory_is_refused(lifecycle_world) -> None:
    scenario = Scenario(lifecycle_world, ["p-skill"])
    elsewhere = Tree(lifecycle_world.root / "elsewhere-skills")
    elsewhere.write("README.md", "a directory outside the project\n")
    (lifecycle_world.project / ".agents").mkdir()
    (lifecycle_world.project / ".agents" / "skills").symlink_to(elsewhere.root)

    refused = scenario.run("install p-skill", exit=1)
    assert_changes(refused.changed, (), label="refused install")
    assert "Refusing deployment through symlinked target directory" in refused.output
    elsewhere.assert_intact("refused install")
    assert sorted(path.name for path in elsewhere.root.iterdir()) == ["README.md"]
    scenario.finish()


# -- Windows standard user --------------------------------------------------------


@pytest.mark.skipif(
    not WINDOWS_STANDARD_USER,
    reason="needs a Windows standard (non-administrator) user",
)
def test_windows_standard_user_user_scope_lifecycle(lifecycle_world) -> None:
    scenario = Scenario(lifecycle_world, ["p-skill"], scope="user")
    scenario.run("install p-skill")
    scenario.assert_deployed()
    scenario.run("install")  # converged: I2 byte no-op
    scenario.run("uninstall p-skill")
    scenario.assert_deployed()
    scenario.finish()
