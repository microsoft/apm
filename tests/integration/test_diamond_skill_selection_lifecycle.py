"""Real-binary lifecycle: diamond dependencies merge their selections (#3735, #3763).

Two parents that declare one skill bundle with different ``skills:`` or
``targets:`` must deploy and lock the sorted union, whatever the manifest
order, depth or scope. An unrestricted declaration absorbs narrowing, and
each skill reaches only the targets its own declaration paired it with
(OpenAPM req-rs-018). Uninstalling a parent shrinks the union, audit
recomputes it, and a frozen install refuses an older lockfile that recorded
only one parent's selection or a cross-product of two.

Packages are served as remote-shaped GitHub identities from local Git
repositories, so every edge lands on one dependency identity.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepository, LocalGitRepositoryFactory
from tests.utils.local_github_https_fixture import (
    local_github_https_fixture,
    openssl_executable,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.skipif(openssl_executable() is None, reason="openssl CLI not available"),
]

_OWNER = "fixture-diamond"
_REF = "v1.0.0"
_SKILLS = ("alpha", "beta", "delta", "gamma")
_BUNDLE = f"{_OWNER}/bundle"


def _git(name: str, ref: str = _REF, **fields: object) -> dict[str, object]:
    return {"git": f"https://github.com/{_OWNER}/{name}.git", "ref": ref, **fields}


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _manifest(name: str, deps: list | None = None, **extra: object) -> str:
    data: dict[str, object] = {"name": name, "version": "1.0.0", **extra}
    if deps:
        data["dependencies"] = {"apm": deps}
    return yaml.safe_dump(data, sort_keys=False)


def _bundle_tree(root: Path) -> None:
    _write(root / "apm.yml", _manifest("bundle"))
    for name in _SKILLS:
        _write(
            root / ".apm" / "skills" / name / "SKILL.md",
            f"---\nname: {name}\ndescription: {name} skill.\n---\n# {name}\n",
        )


def _evidence(result: CommandResult) -> str:
    return (
        f"command={result.command!r}\nreturncode={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


@dataclass
class _Scenario:
    isolated: IsolatedApmEnvironment
    factory: LocalGitRepositoryFactory
    repositories: dict[str, LocalGitRepository]
    runner: ApmLifecycleRunner
    env: dict[str, str]
    name: str

    def publish(self, repo: str, *, tag: str, manifest: str) -> None:
        repository = self.repositories[repo]
        _write(repository.worktree / "apm.yml", manifest)
        self.factory.tag(repository, tag, self.factory.commit(repository, message=tag))

    def project(self, name: str, deps: list, *, targets: tuple[str, ...] = ("copilot",)) -> Path:
        root = self.isolated.work_root / name
        _write(root / "apm.yml", _manifest(name, deps, targets=list(targets)))
        return root

    def run(
        self, args: tuple[str, ...], cwd: Path, step: str, expected: int | None = 0
    ) -> CommandResult:
        result = self.runner.run(args, scenario_id=f"{self.name}-{step}", cwd=cwd, env=self.env)
        if expected is not None:
            assert result.returncode == expected, _evidence(result)
        return result


@contextmanager
def _scenario(
    tmp_path: Path, apm_binary_path: Path, name: str, parents: dict[str, list]
) -> Iterator[_Scenario]:
    """Publish the bundle and parents (each declaring *parents[name]*) at v1.0.0."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    factory = LocalGitRepositoryFactory(isolated.repository_root, env=isolated.subprocess_env())
    repositories: dict[str, LocalGitRepository] = {}
    bundle_source = isolated.package_root / "bundle"
    _bundle_tree(bundle_source)
    repositories["bundle"] = factory.create("bundle", source_tree=bundle_source)
    factory.tag(
        repositories["bundle"], _REF, factory.commit(repositories["bundle"], message="bundle")
    )
    for parent, deps in parents.items():
        source = isolated.package_root / parent
        _write(source / "apm.yml", _manifest(parent, deps))
        repositories[parent] = factory.create(parent, source_tree=source)
        factory.tag(
            repositories[parent], _REF, factory.commit(repositories[parent], message=parent)
        )
    slugs = {f"{_OWNER}/{repo}": repository for repo, repository in repositories.items()}
    with local_github_https_fixture(tmp_path / "certs", slugs) as fixture:
        env = factory.url_rewrite_subprocess_env_many(
            [(repository, f"https://github.com/{slug}") for slug, repository in slugs.items()]
        )
        env["APM_POLICY_DISABLE"] = "1"
        yield _Scenario(
            isolated=isolated,
            factory=factory,
            repositories=repositories,
            runner=ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=240),
            env=fixture.child_environment(env),
            name=name,
        )


def _lock(lock_dir: Path) -> dict:
    return yaml.safe_load((lock_dir / "apm.lock.yaml").read_text(encoding="utf-8")) or {}


def _locked(lock_dir: Path, repo: str = _BUNDLE) -> dict:
    entries = [
        dep for dep in _lock(lock_dir).get("dependencies") or [] if dep.get("repo_url") == repo
    ]
    assert len(entries) == 1, _lock(lock_dir)
    return entries[0]


def _skills(deploy_root: Path, skills_dir: str = ".agents/skills") -> set[str]:
    root = deploy_root / skills_dir
    return {path.name for path in root.iterdir() if path.is_dir()} if root.is_dir() else set()


def _dependency_rows(lock_dir: Path) -> list[dict]:
    rows = [
        {key: value for key, value in dep.items() if key not in {"resolved_by", "depth"}}
        for dep in _lock(lock_dir).get("dependencies") or []
    ]
    return sorted(rows, key=lambda row: row["repo_url"])


_SIBLINGS = {
    "pkg-a": [_git("bundle", skills=["alpha", "beta"])],
    "pkg-b": [_git("bundle", skills=["gamma", "delta"])],
}


def test_sibling_diamond_unions_in_any_order_then_uninstall_shrinks(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """L1 + L6: siblings union order-independently; uninstall shrinks then cleans."""
    with _scenario(tmp_path, apm_binary_path, "siblings", _SIBLINGS) as scenario:
        ab = scenario.project("ab", [_git("pkg-a"), _git("pkg-b")])
        ba = scenario.project("ba", [_git("pkg-b"), _git("pkg-a")])
        for project in (ab, ba):
            scenario.run(("install",), project, f"install-{project.name}")
            assert _skills(project) == set(_SKILLS)
            assert _locked(project)["skill_subset"] == sorted(_SKILLS)
        assert _dependency_rows(ab) == _dependency_rows(ba)

        scenario.run(("uninstall", f"{_OWNER}/pkg-a"), ab, "uninstall-a")
        assert _skills(ab) == {"delta", "gamma"}
        locked = _locked(ab)
        assert locked["skill_subset"] == ["delta", "gamma"]
        assert {
            path.split("/")[2] for path in locked.get("deployed_files") or [] if "skills/" in path
        } == {"delta", "gamma"}
        scenario.run(("audit", "--ci"), ab, "audit-after-shrink")

        scenario.run(("uninstall", f"{_OWNER}/pkg-b"), ab, "uninstall-b")
        assert _skills(ab) == set()
        assert not (ab / "apm_modules" / _OWNER / "bundle").exists()


def test_user_scope_diamond_unions_and_shrinks(tmp_path: Path, apm_binary_path: Path) -> None:
    """L10: the same union and shrink hold for a global (-g) installation."""
    with _scenario(tmp_path, apm_binary_path, "global", _SIBLINGS) as scenario:
        home = scenario.isolated.home
        _write(home / ".apm" / "apm.yml", _manifest("global", [_git("pkg-b"), _git("pkg-a")]))
        cwd = scenario.isolated.work_root
        scenario.run(("install", "-g"), cwd, "install")
        assert _skills(home, ".copilot/skills") | _skills(home, ".agents/skills") >= set(_SKILLS)
        assert _locked(home / ".apm")["skill_subset"] == sorted(_SKILLS)

        scenario.run(("uninstall", "-g", f"{_OWNER}/pkg-a"), cwd, "uninstall-a")
        assert _locked(home / ".apm")["skill_subset"] == ["delta", "gamma"]
        deployed = _skills(home, ".copilot/skills") | _skills(home, ".agents/skills")
        assert deployed == {"delta", "gamma"}


def test_depth_three_chain_unions_and_update_recomputes(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """L2 + L11: consumer -> pkg-b -> pkg-a chain unions; an upstream change re-merges."""
    parents = {
        "pkg-a": [_git("bundle", skills=["alpha", "beta"])],
        "pkg-b": [_git("pkg-a"), _git("bundle", skills=["gamma", "delta"])],
    }
    with _scenario(tmp_path, apm_binary_path, "chain", parents) as scenario:
        project = scenario.project("chain", [_git("pkg-b", ref="main")])
        scenario.run(("install",), project, "install")
        assert _locked(project)["skill_subset"] == sorted(_SKILLS)
        assert _skills(project) == set(_SKILLS)

        scenario.publish(
            "pkg-b",
            tag="v1.1.0",
            manifest=_manifest("pkg-b", [_git("pkg-a"), _git("bundle", skills=["gamma"])]),
        )
        scenario.run(("update", "--yes"), project, "update")
        assert _locked(project)["skill_subset"] == ["alpha", "beta", "gamma"]
        assert _skills(project) == {"alpha", "beta", "gamma"}


def test_unrestricted_edge_absorbs_and_targets_union(tmp_path: Path, apm_binary_path: Path) -> None:
    """L3 + L5: an unrestricted parent absorbs narrowing only on its own harness."""
    parents = {
        "pkg-a": [_git("bundle", skills=["alpha"], targets=["claude"])],
        "pkg-b": [_git("bundle", targets=["copilot"])],
    }
    with _scenario(tmp_path, apm_binary_path, "absorb", parents) as scenario:
        project = scenario.project(
            "absorb", [_git("pkg-a"), _git("pkg-b")], targets=("claude", "copilot")
        )
        scenario.run(("install",), project, "install")
        locked = _locked(project)
        assert "skill_subset" not in locked
        assert locked["target_subset"] == ["claude", "copilot"]
        assert locked["subset_pairings"] == [
            {"skills": ["alpha"], "targets": ["claude"]},
            {"targets": ["copilot"]},
        ]
        assert _skills(project, ".claude/skills") == {"alpha"}
        assert _skills(project, ".agents/skills") == set(_SKILLS)
        scenario.run(("audit", "--ci"), project, "audit")


def test_root_and_transitive_union_audits_and_tamper_fails(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """L4 + L12: root plus transitive declarations audit as their union."""
    with _scenario(
        tmp_path, apm_binary_path, "audit", {"pkg-b": [_git("bundle", skills=["gamma"])]}
    ) as scenario:
        project = scenario.project("audit", [_git("bundle", skills=["alpha"]), _git("pkg-b")])
        scenario.run(("install",), project, "install")
        assert _skills(project) == {"alpha", "gamma"}
        assert _locked(project)["skill_subset"] == ["alpha", "gamma"]
        scenario.run(("audit", "--ci"), project, "audit")

        lock_path = project / "apm.lock.yaml"
        data = _lock(project)
        for dep in data["dependencies"]:
            if dep.get("repo_url") == _BUNDLE:
                dep["skill_subset"] = ["alpha", "beta", "gamma"]
        lock_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        tampered = scenario.run(("audit", "--ci"), project, "audit-tampered", expected=None)
        assert tampered.returncode != 0, _evidence(tampered)
        assert "skill-subset-consistency" in tampered.stdout + tampered.stderr


def test_frozen_refuses_single_parent_lock_then_converges(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """L8: an older one-parent selection fails --frozen with a named reason."""
    with _scenario(tmp_path, apm_binary_path, "frozen", _SIBLINGS) as scenario:
        project = scenario.project("frozen", [_git("pkg-a"), _git("pkg-b")])
        scenario.run(("install",), project, "install")
        scenario.run(("install", "--frozen"), project, "frozen-current")

        lock_path = project / "apm.lock.yaml"
        data = _lock(project)
        for dep in data["dependencies"]:
            if dep.get("repo_url") == _BUNDLE:
                dep["skill_subset"] = ["alpha", "beta"]
        lock_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        refused = scenario.run(("install", "--frozen"), project, "frozen-old", expected=None)
        output = refused.stdout + refused.stderr
        assert refused.returncode != 0, _evidence(refused)
        assert f"selection for {_BUNDLE} changed" in output, _evidence(refused)
        assert "apm install" in output

        scenario.run(("install",), project, "converge")
        assert _locked(project)["skill_subset"] == sorted(_SKILLS)
        scenario.run(("install", "--frozen"), project, "frozen-converged")


def test_transitive_typo_fails_naming_parent_and_preserves_prior_state(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """L9: a parent's unmatched selection fails (#3204) and names that parent."""
    parents = {
        "pkg-a": [_git("bundle", skills=["alpha"])],
        "pkg-b": [_git("bundle", skills=["gamma"])],
    }
    with _scenario(tmp_path, apm_binary_path, "typo", parents) as scenario:
        project = scenario.project("typo", [_git("pkg-a"), _git("pkg-b")])
        scenario.run(("install",), project, "install")
        assert _skills(project) == {"alpha", "gamma"}
        lock_before = (project / "apm.lock.yaml").read_bytes()
        skills_before = {
            path.relative_to(project): path.read_bytes()
            for path in (project / ".agents" / "skills").rglob("*")
            if path.is_file()
        }

        scenario.publish(
            "pkg-b", tag="v2.0.0", manifest=_manifest("pkg-b", [_git("bundle", skills=["gama"])])
        )
        _write(
            project / "apm.yml",
            _manifest("typo", [_git("pkg-a"), _git("pkg-b", ref="v2.0.0")], targets=["copilot"]),
        )
        failed = scenario.run(("install",), project, "install-typo", expected=None)
        output = failed.stdout + failed.stderr
        assert failed.returncode != 0, _evidence(failed)
        assert f"{_OWNER}/pkg-b" in output and "gama" in output, _evidence(failed)
        assert (project / "apm.lock.yaml").read_bytes() == lock_before
        assert {
            path.relative_to(project): path.read_bytes()
            for path in (project / ".agents" / "skills").rglob("*")
            if path.is_file()
        } == skills_before


_CROSS = {
    "pkg-a": [_git("bundle", skills=["alpha"], targets=["claude"])],
    "pkg-b": [_git("bundle", skills=["gamma"], targets=["copilot"])],
}


def test_divergent_skills_and_targets_pair_per_edge(tmp_path: Path, apm_binary_path: Path) -> None:
    """#3763: alpha deploys only for claude, gamma only for copilot, in either order.

    The lock records the exact pairing byte-identically across declaration
    orders; audit, frozen replay and uninstall shrink read the same owner.
    """
    with _scenario(tmp_path, apm_binary_path, "pairing", _CROSS) as scenario:
        harnesses = ("claude", "copilot")
        ab = scenario.project("ab", [_git("pkg-a"), _git("pkg-b")], targets=harnesses)
        ba = scenario.project("ba", [_git("pkg-b"), _git("pkg-a")], targets=harnesses)
        for project in (ab, ba):
            scenario.run(("install",), project, f"install-{project.name}")
            assert _skills(project, ".claude/skills") == {"alpha"}
            assert _skills(project, ".agents/skills") == {"gamma"}
            locked = _locked(project)
            assert locked["skill_subset"] == ["alpha", "gamma"]
            assert locked["target_subset"] == ["claude", "copilot"]
            assert locked["subset_pairings"] == [
                {"skills": ["alpha"], "targets": ["claude"]},
                {"skills": ["gamma"], "targets": ["copilot"]},
            ]
            scenario.run(("audit", "--ci"), project, f"audit-{project.name}")
            scenario.run(("install", "--frozen"), project, f"frozen-{project.name}")
        assert _dependency_rows(ab) == _dependency_rows(ba)

        scenario.run(("uninstall", f"{_OWNER}/pkg-a"), ab, "uninstall-a")
        assert _skills(ab, ".claude/skills") == set()
        assert _skills(ab, ".agents/skills") == {"gamma"}
        locked = _locked(ab)
        assert locked["skill_subset"] == ["gamma"]
        assert locked["target_subset"] == ["copilot"]
        assert "subset_pairings" not in locked
        scenario.run(("audit", "--ci"), ab, "audit-after-shrink")


def test_frozen_refuses_cross_product_lock_then_converges(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """D6: a lock from 0.36.1 (flat unions, no pairing) fails --frozen with a reason.

    That lock encodes the cross-product, a different plan from the exact
    pairing, so frozen refuses before deploying; a plain install converges.
    """
    with _scenario(tmp_path, apm_binary_path, "old-lock", _CROSS) as scenario:
        project = scenario.project(
            "old", [_git("pkg-a"), _git("pkg-b")], targets=("claude", "copilot")
        )
        scenario.run(("install",), project, "install")
        lock_path = project / "apm.lock.yaml"
        data = _lock(project)
        for dep in data["dependencies"]:
            if dep.get("repo_url") == _BUNDLE:
                dep.pop("subset_pairings")
        lock_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        skills_before = _skills(project, ".claude/skills")

        refused = scenario.run(("install", "--frozen"), project, "frozen-old", expected=None)
        output = refused.stdout + refused.stderr
        assert refused.returncode != 0, _evidence(refused)
        assert f"selection for {_BUNDLE} changed" in output, _evidence(refused)
        assert _skills(project, ".claude/skills") == skills_before

        audit = scenario.run(("audit", "--ci"), project, "audit-old", expected=None)
        assert audit.returncode != 0, _evidence(audit)
        assert "skill-subset-consistency" in audit.stdout + audit.stderr

        scenario.run(("install",), project, "converge")
        assert _locked(project)["subset_pairings"]
        scenario.run(("install", "--frozen"), project, "frozen-converged")
