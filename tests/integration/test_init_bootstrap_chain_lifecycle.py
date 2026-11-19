"""An auto-bootstrapped apm.yml (#3630) meets org policy and ``apm init`` (#2231, #3649).

Chain in a fresh repo whose git ``user.name`` is set:

1. ``apm install <pkg>`` auto-creates ``apm.yml`` -- no ``author`` (#3630);
   a repeat install is a no-op (no lockfile churn).
2. ``apm audit --ci`` under an org policy that requires ``author`` reports
   the missing field (the bootstrap no longer fills it).
3. ``apm init --yes --name/--description/--version/--author`` writes all
   four fields exactly; ``apm install`` + ``apm pack`` then succeed.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import load_yaml
from tests.integration._policy_scope_kit import (
    PolicyScopeWorld,
    file_digest,
    lock_repo_urls,
    output,
    run_step,
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

_POLICY = {
    "name": "contoso-policy",
    "version": "1.0.0",
    "enforcement": "block",
    "manifest": {"required_fields": ["author"]},
}


def _fresh_repo(world: PolicyScopeWorld, name: str) -> Path:
    project = world.isolated.work_root / name
    project.mkdir()
    env = world.isolated.subprocess_env()
    for args in (
        ["git", "init", "-q", "--initial-branch=main"],
        ["git", "remote", "add", "origin", f"https://github.com/contoso/{name}.git"],
        ["git", "config", "user.name", "Git Configured User"],
    ):
        subprocess.run(args, cwd=project, env=env, check=True)
    return project


def test_bootstrap_without_author_then_init_metadata_chain(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    world = PolicyScopeWorld(tmp_path, dict(os.environ))
    pkg = world.publish_package("pkg-a")
    world.publish_policy(_POLICY)
    boot = _fresh_repo(world, "boot")
    fresh = _fresh_repo(world, "fresh")
    runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=600)

    with local_github_https_fixture(tmp_path / "certs", world.served_repositories()) as fx:
        env = fx.child_environment(world.git_env())
        install = ("install", f"{pkg.slug}#v1.0.0", "--target", "copilot")

        run_step(runner, install, cwd=boot, env=env, step="1-bootstrap")
        manifest = load_yaml(boot / "apm.yml")
        assert "author" not in manifest, manifest
        assert manifest["dependencies"]["apm"] == [f"{pkg.slug}#v1.0.0"]
        assert lock_repo_urls(boot) == {pkg.slug}
        lock = file_digest(boot / "apm.lock.yaml")
        yml = file_digest(boot / "apm.yml")

        run_step(runner, install, cwd=boot, env=env, step="2-repeat")
        assert file_digest(boot / "apm.lock.yaml") == lock, "repeat install churned lock"
        assert file_digest(boot / "apm.yml") == yml, "repeat install rewrote apm.yml"

        r = run_step(runner, ("audit", "--ci"), cwd=boot, env=env, step="3-audit", expected=(0, 1))
        audit_rc, audit_text = r.returncode, output(r)

        run_step(
            runner,
            (
                "init",
                "--yes",
                "--name",
                "fresh-app",
                "--description",
                "Fresh description",
                "--version",
                "0.4.2",
                "--author",
                "Jane Dev",
                "--target",
                "copilot",
            ),
            cwd=fresh,
            env=env,
            step="4-init",
        )
        manifest = load_yaml(fresh / "apm.yml")
        assert (
            manifest["name"],
            manifest["description"],
            manifest["version"],
            manifest["author"],
        ) == ("fresh-app", "Fresh description", "0.4.2", "Jane Dev")
        run_step(runner, install, cwd=fresh, env=env, step="5-install")
        assert load_yaml(fresh / "apm.yml")["author"] == "Jane Dev"
        run_step(runner, ("audit", "--ci"), cwd=fresh, env=env, step="6-audit")
        run_step(runner, ("pack",), cwd=fresh, env=env, step="7-pack")
        assert any((fresh / "build").glob("fresh-app-0.4.2*")), list((fresh / "build").iterdir())

    # Documented consequence of #3630: the bootstrap leaves author unset, so a
    # policy that requires it flags the fresh manifest.
    assert audit_rc == 1, audit_text
    assert "author" in audit_text, audit_text
