"""Installed-CLI regression: every command's orphan removal comes from one owner (#4011).

Each offline preview (``apm install --dry-run``, ``apm prune --dry-run``,
``apm uninstall --dry-run``) must name exactly what the real run then removes.
The same owner keeps a targeted install from touching unrelated orphans,
removes unlocked ``apm_modules`` directories in prune, rescues a transitive
dependency a surviving package still declares in uninstall, and gives
``apm audit --ci`` the direct-entry projection of the install decision.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_package import LocalPackage, LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
]

_INSTALL_FLAGS = ("--target", "claude", "--no-policy", "--parallel-downloads", "0")


@dataclass
class _Project:
    """One isolated consumer project driven through the installed CLI."""

    runner: ApmLifecycleRunner
    environment: dict[str, str]
    root: Path
    packages: dict[str, LocalPackage]

    def run(self, *arguments: str, expect: int = 0) -> CommandResult:
        result = self.runner.run(
            arguments, scenario_id="orphan-removal-owner", cwd=self.root, env=self.environment
        )
        assert result.returncode == expect, f"{result.stdout}\n{result.stderr}"
        return result

    def install(self, *arguments: str) -> CommandResult:
        return self.run("install", *arguments, *_INSTALL_FLAGS)

    def lock(self) -> dict[str, dict[str, object]]:
        lock = load_yaml(self.root / "apm.lock.yaml")
        return {entry["name"]: entry for entry in lock["dependencies"]}

    def declared(self) -> list[object]:
        return list(load_yaml(self.root / "apm.yml")["dependencies"]["apm"])

    def declaration(self, name: str) -> object:
        source = str(self.packages[name].root)
        matches = [entry for entry in self.declared() if str(entry).endswith(name)]
        assert len(matches) == 1, (source, self.declared())
        return matches[0]

    def undeclare(self, name: str) -> None:
        manifest = load_yaml(self.root / "apm.yml")
        manifest["dependencies"]["apm"] = [
            entry for entry in manifest["dependencies"]["apm"] if entry != self.declaration(name)
        ]
        dump_yaml(manifest, self.root / "apm.yml")

    def skills(self) -> set[str]:
        skills = self.root / ".claude" / "skills"
        return {path.name for path in skills.iterdir() if path.is_dir()}


_STATUS = re.compile(r"^\s*\[.\]\s*")


def _lines(output: str) -> list[str]:
    """Return output lines without their ``[i]``-style status symbol."""
    return [_STATUS.sub("", line).strip() for line in output.splitlines()]


def _listed(output: str, header: str) -> set[str]:
    """Return the ``- item`` lines that follow *header* in CLI output."""
    lines = _lines(output)
    starts = [index for index, line in enumerate(lines) if header in line]
    assert starts, f"{header!r} not in output:\n{output}"
    listed: set[str] = set()
    for line in lines[starts[0] + 1 :]:
        if not line.startswith("- "):
            break
        listed.add(line[2:].removesuffix(" (would be removed)").strip())
    return listed


def _setup(tmp_path: Path, runner: ApmLifecycleRunner) -> _Project:
    """A consumer with parent (-> dropped, shared), sibling (-> shared) and bystander."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "lifecycle", base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    environment["APM_NO_CACHE"] = "1"
    environment["COLUMNS"] = "500"
    environment.pop("CLAUDE_CONFIG_DIR", None)
    factory = LocalPackageFactory(isolated.package_root)

    def package(name: str, deps: tuple[LocalPackage, ...] = ()) -> LocalPackage:
        created = factory.create(name, dependencies=tuple(str(dep.root) for dep in deps))
        factory.add_skill(
            created, name, f"---\nname: {name}\ndescription: Skill {name}\n---\n# {name}\n"
        )
        return created

    dropped = package("leaf-dropped")
    shared = package("leaf-shared")
    parent = package("parent", (dropped, shared))
    sibling = package("sibling", (shared,))
    bystander = package("bystander")
    root = isolated.work_root / "consumer"
    root.mkdir(parents=True)
    (root / "apm.yml").write_text("name: consumer\nversion: 1.0.0\n", encoding="utf-8")
    consumer = _Project(
        runner,
        environment,
        root,
        {"parent": parent, "sibling": sibling, "bystander": bystander},
    )
    # Parent first, so the shared leaf is recorded under it.
    for name in ("parent", "sibling", "bystander"):
        consumer.install(str(consumer.packages[name].root))
    assert {"parent", "leaf-dropped", "leaf-shared", "sibling", "bystander"} <= consumer.skills()
    return consumer


def test_install_and_prune_previews_match_real_runs_and_targeted_install_is_isolated(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    project = _setup(tmp_path, ApmLifecycleRunner((str(apm_binary_path),)))
    bystander_files = set(project.lock()["bystander"]["deployed_files"])
    assert bystander_files
    project.undeclare("bystander")

    # A targeted install previews and removes nothing outside its subtree.
    preview = project.run("install", str(project.packages["sibling"].root), "--dry-run")
    assert "Files that would be removed" not in preview.stdout
    project.install(str(project.packages["sibling"].root))
    assert "bystander" in project.lock()
    assert all((project.root / path).exists() for path in bystander_files)

    # The full-install preview names exactly the files the real run removes.
    preview = project.run("install", "--dry-run", *_INSTALL_FLAGS)
    previewed = _listed(preview.stdout, "Files that would be removed")
    assert previewed == bystander_files
    project.install()
    assert "bystander" not in project.lock()
    assert not any((project.root / path).exists() for path in previewed)
    assert {"parent", "leaf-dropped", "leaf-shared", "sibling"} <= project.skills()

    # Prune: the uninstalled checkout plus an unlocked directory, nothing else.
    stray = project.root / "apm_modules" / "stray-org" / "stray-pkg"
    stray.mkdir(parents=True)
    (stray / "apm.yml").write_text("name: stray-pkg\nversion: 0.1.0\n", encoding="utf-8")
    preview = project.run("prune", "--dry-run")
    previewed = _listed(preview.stdout, "orphaned package(s)")
    assert "stray-org/stray-pkg" in previewed
    assert any(path.endswith("bystander") for path in previewed)
    real = project.run("prune")
    removed = {
        line.removeprefix("Removed ") for line in _lines(real.stdout) if line.startswith("Removed ")
    }
    assert removed == previewed
    assert not stray.exists()
    assert {"parent", "leaf-dropped", "leaf-shared", "sibling"} <= set(project.lock())


def test_uninstall_preview_matches_real_run_and_rescues_shared_dependency(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    project = _setup(tmp_path, ApmLifecycleRunner((str(apm_binary_path),)))
    target = str(project.declaration("parent"))

    preview = project.run("uninstall", target, "--dry-run")
    previewed = _listed(preview.stdout, "Transitive dependencies that would be removed")
    # Only the exclusive leaf; the shared one is rescued in the preview too.
    assert len(previewed) == 1
    assert next(iter(previewed)).endswith("/leaf-dropped")

    project.run("uninstall", target)
    after = project.lock()
    assert "parent" not in after
    assert "leaf-dropped" not in after
    # Rescued: the surviving sibling still declares the shared leaf.
    assert "leaf-shared" in after
    assert "leaf-shared" in project.skills()
    assert "leaf-dropped" not in project.skills()


def test_audit_reports_the_direct_entry_projection(tmp_path: Path, apm_binary_path: Path) -> None:
    project = _setup(tmp_path, ApmLifecycleRunner((str(apm_binary_path),)))
    project.undeclare("parent")

    audit = project.run("audit", "--ci", "--no-policy", "--format", "json", expect=1)
    payload = json.loads(audit.stdout)
    check = next(item for item in payload["checks"] if item["name"] == "no-orphaned-packages")
    # Only the undeclared direct entry; its transitive leaves belong to it.
    assert check["passed"] is False
    assert len(check["details"]) == 1
    assert check["details"][0].endswith("/parent")
