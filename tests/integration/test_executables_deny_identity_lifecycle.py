"""Org executables deny matches every spelling of a package (#3649).

An org ``executables.deny`` entry is the ceiling on trust: no project or user
allow can deploy the denied package's hooks. The deny entry and the allow key
may spell the package differently (host-qualified, URL, different case, or a
publisher glob); the decision uses the package's canonical identity, so the
hooks stay out and the lockfile records ``denied``. Without a deny, the same
allow deploys the hooks (control).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import load_yaml
from tests.integration._policy_scope_kit import (
    PolicyScopeWorld,
    PublishedPackage,
    output,
    run_step,
    write,
)
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.local_github_https_fixture import (
    local_github_https_fixture,
    openssl_executable,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.skipif(openssl_executable() is None, reason="openssl CLI not available"),
]

_POLICY = {"name": "contoso-policy", "version": "1.0.0", "enforcement": "block"}
_ALLOW_KEY = "github.com/contoso/pkg-hook"


def _publish_hook_package(world: PolicyScopeWorld) -> PublishedPackage:
    source = world.isolated.package_root / "pkg-hook"
    write(source / "apm.yml", "name: pkg-hook\nversion: 1.0.0\ndescription: d\nauthor: a\n")
    hook = {
        "hooks": {
            "PreToolUse": [
                {"matcher": "*", "hooks": [{"type": "command", "command": "echo guard"}]}
            ]
        }
    }
    write(source / ".apm" / "hooks" / "guard.json", json.dumps(hook))
    repository = world.repositories.create("pkg-hook", source_tree=source)
    commit = world.repositories.commit(repository, message="pkg-hook v1.0.0")
    world.repositories.tag(repository, "v1.0.0", commit)
    package = PublishedPackage(name="pkg-hook", repository=repository, commit=commit)
    world.packages["pkg-hook"] = package
    return package


def _hook_commands(project: Path) -> list[str]:
    settings = project / ".claude" / "settings.json"
    if not settings.exists():
        return []
    data = json.loads(settings.read_text(encoding="utf-8"))
    return [
        handler.get("command")
        for groups in data.get("hooks", {}).values()
        for group in groups
        for handler in group.get("hooks", [])
    ]


@pytest.mark.parametrize(
    "deny",
    [
        None,
        "contoso/pkg-hook",
        "Contoso/Pkg-Hook",
        "https://github.com/contoso/pkg-hook.git",
        "contoso/*",
    ],
    ids=["no-deny", "shorthand", "mixed-case", "url", "publisher-glob"],
)
def test_org_deny_holds_for_any_allow_spelling(
    tmp_path: Path, apm_binary_path: Path, deny: str | None
) -> None:
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    package = _publish_hook_package(world)
    executables = {"deny": [deny]} if deny else {"recommend": ["other/pkg"]}
    world.publish_policy({**_POLICY, "executables": executables})
    project = world.consumer(
        dependencies=[f"{package.slug}#v1.0.0"],
        targets=("claude",),
        extra={"executables": {"allow": {_ALLOW_KEY: {"hooks": True}}}},
    )
    (project / ".claude").mkdir()
    runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=900)

    with local_github_https_fixture(tmp_path / "certs", world.served_repositories()) as fx:
        env = fx.child_environment(world.git_env())
        result = run_step(runner, ("install",), cwd=project, env=env, step="install")

    lock = load_yaml(project / "apm.lock.yaml") or {}
    statuses = [entry.get("exec_status") for entry in lock.get("dependencies", [])]
    if deny is None:
        assert _hook_commands(project) == ["echo guard"], output(result)
        assert statuses == ["deployed"]
    else:
        assert _hook_commands(project) == [], output(result)
        assert statuses == ["denied"]
