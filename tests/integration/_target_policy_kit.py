"""Shared plumbing for manifest-target and compilation-target policy chains (#3732).

Builds on :mod:`tests.integration._policy_scope_kit`: an org policy served from
``contoso/.github`` over the loopback GitHub HTTPS fixture, ``contoso/pkg-*``
packages from local bare repositories, and consumer projects whose ``origin``
points at ``contoso`` so policy auto-discovery needs no real network.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration._policy_scope_kit import (
    PolicyScopeWorld,
    output,
    run_step,
    tree_snapshot,
)
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.local_github_https_fixture import local_github_https_fixture

SELF_DEFINED_MCP = {
    "name": "srv",
    "registry": False,
    "transport": "http",
    "url": "https://mcp.example.com/mcp",
}

# The legacy deprecation text the MCP phase prints for ``all``. Under decision
# D1 a singular ``target: all`` expands for deployment, so this claim must not
# be printed for it.
TREATED_AS_OMITTED = "treating the field as omitted"


def block_policy(target_policy: Mapping[str, object] | None = None, **extra: object) -> dict:
    """Return a block-mode org policy with an optional ``compilation.target`` block."""
    policy: dict[str, object] = {
        "name": "contoso-policy",
        "version": "1.0.0",
        "enforcement": "block",
    }
    if target_policy is not None:
        policy["compilation"] = {"target": dict(target_policy)}
    policy.update(extra)
    return policy


def write_manifest(project: Path, declaration: Mapping[str, object], **extra: object) -> None:
    """Replace the consumer manifest's target declaration, keeping dependencies."""
    manifest = load_yaml(project / "apm.yml") or {}
    manifest.pop("targets", None)
    manifest.pop("target", None)
    manifest.update(declaration)
    manifest.update(extra)
    dump_yaml(manifest, project / "apm.yml")


def compilation_target_check(result: CommandResult) -> dict:
    """Return the ``compilation-target`` row from ``audit --ci -f json`` output."""
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError as exc:  # pragma: no cover - diagnostic path
        raise AssertionError(f"audit did not emit JSON:\n{output(result)}") from exc
    for check in report.get("checks") or []:
        if check.get("name") == "compilation-target":
            return check
    raise AssertionError(f"no compilation-target check in audit report:\n{result.stdout}")


def check_text(check: Mapping[str, object]) -> str:
    return " ".join([str(check.get("message", "")), *map(str, check.get("details") or [])])


def quoted_tokens(text: str) -> set[str]:
    """Return every single-quoted token in a policy message."""
    return set(re.findall(r"'([^']*)'", text))


def names_word(text: str, name: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", text) is not None


def violation_text(result: CommandResult, check: str = "compilation-target") -> str:
    """Return the whitespace-normalized install violation for ``check``."""
    flat = " ".join(output(result).split())
    match = re.search(rf"Policy violation: {re.escape(check)} -- (.*?)(?: Blocked by |$)", flat)
    return match.group(1) if match else ""


def changed_paths(before: Mapping[str, str], after: Mapping[str, str]) -> list[str]:
    keys = set(before) | set(after)
    return sorted(key for key in keys if before.get(key) != after.get(key))


def file_bytes(path: Path) -> bytes | None:
    return path.read_bytes() if path.exists() else None


@dataclass
class TargetPolicyWorld:
    """One policy world plus a runner and child environment inside the HTTPS fixture."""

    world: PolicyScopeWorld
    runner: ApmLifecycleRunner
    env: dict[str, str]

    def consumer(
        self,
        name: str,
        declaration: Mapping[str, object],
        *,
        dependencies: Sequence[object] | None = None,
        mcp: Sequence[Mapping[str, object]] = (),
        dirs: Sequence[str] = (),
    ) -> Path:
        deps = (
            [f"{pkg.slug}#v1.0.0" for pkg in self.world.packages.values()]
            if dependencies is None
            else list(dependencies)
        )
        project = self.world.consumer(name, dependencies=deps)
        extra: dict[str, object] = {}
        if mcp:
            extra["dependencies"] = {"apm": deps, "mcp": [dict(entry) for entry in mcp]}
        write_manifest(project, declaration, **extra)
        for directory in dirs:
            (project / directory).mkdir(parents=True, exist_ok=True)
        return project

    def run(
        self,
        project: Path,
        *args: str,
        step: str,
        expected: int | tuple[int, ...] = 0,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        return run_step(
            self.runner, args, cwd=project, env=env or self.env, step=step, expected=expected
        )

    def audit_read_only(self, project: Path, *, step: str) -> tuple[CommandResult, dict]:
        """Run ``audit --ci -f json`` and assert it changed nothing in the project."""
        before = tree_snapshot(project)
        result = self.run(project, "audit", "--ci", "-f", "json", step=step, expected=(0, 1))
        after = tree_snapshot(project)
        assert after == before, (
            f"[{step}] audit --ci changed project files: {changed_paths(before, after)}"
        )
        return result, compilation_target_check(result)


@contextmanager
def target_policy_world(
    tmp_path: Path,
    apm_binary_path: Path,
    policy: Mapping[str, object],
    *,
    packages: Sequence[str] = ("pkg-a",),
    extra_policy_repos: Mapping[str, Mapping[str, object]] | None = None,
) -> Iterator[TargetPolicyWorld]:
    """Yield a served policy world. ``extra_policy_repos`` adds ``contoso/<name>`` repos."""
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    for name in packages:
        world.publish_package(name)
    world.publish_policy(policy)
    served = world.served_repositories()
    for name, content in (extra_policy_repos or {}).items():
        source = world.isolated.package_root / name
        source.mkdir(parents=True)
        dump_yaml(dict(content), source / "apm-policy.yml")
        repository = world.repositories.create(name, source_tree=source)
        world.repositories.commit(repository, message=f"{name} policy")
        served[f"contoso/{name}"] = repository
    runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=900)
    with local_github_https_fixture(tmp_path / "certs", served) as fixture:
        env = fixture.child_environment(world.git_env())
        env.pop("CLAUDE_CONFIG_DIR", None)
        yield TargetPolicyWorld(world=world, runner=runner, env=env)
