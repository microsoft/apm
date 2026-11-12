"""Real installed-CLI Cursor rule delivery at user scope, with project parity.

APM writes ``~/.cursor/rules/*.mdc`` through the existing ``cursor_rules``
transform. These tests prove file creation, ownership and lifecycle only;
native consumption by Cursor 2.1+ rests on Cursor's documented home-folder
rules capability (https://cursor.com/changelog/2-1) and is not executed here.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.artifact_snapshot import ArtifactSnapshotSet
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_e2e_mode,
    pytest.mark.requires_apm_binary,
]

_STYLE = "---\ndescription: Style rules\napplyTo: '**/*.py'\n---\nCURSOR_STYLE_V1\n"
_GLOBAL = "---\ndescription: Always on\n---\nCURSOR_GLOBAL\n"
_PLAIN = "---\ndescription: {name}\n---\n{body}\n"
_USER_RULE = "---\ndescription: Mine\n---\nUSER_RULE\n"
_USER_COLLISION = "---\ndescription: Mine too\n---\nUSER_COLLISION\n"


@pytest.mark.parametrize("user_scope", [True, False], ids=["user", "project"])
def test_cursor_rules_installed_lifecycle(
    tmp_path: Path, apm_binary_path: Path, user_scope: bool
) -> None:
    """Install, repeat, update, frozen replay, audit and remove without losing user rules."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    environment["APM_NO_CACHE"] = "1"
    runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=300)
    factory = LocalPackageFactory(isolated.package_root / "repository")
    package = factory.create("rules-kit", targets=("cursor",))
    factory.add_instruction(package, "style", _STYLE)
    factory.add_instruction(package, "always", _GLOBAL)
    factory.add_instruction(package, "retired", _PLAIN.format(name="Retired", body="RETIRED"))
    factory.add_instruction(package, "edited", _PLAIN.format(name="Edited", body="MANAGED"))
    factory.add_instruction(package, "collision", _PLAIN.format(name="Pkg", body="PKG"))
    factory.add_agent(package, "reviewer", "---\ndescription: Reviewer\n---\nReview\n")
    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=environment)
    repo = repositories.create("rules-kit", source_tree=package.root)
    first = repositories.commit(repo, message="seed cursor rules")
    remote = "https://gitlab.example.invalid/cursor/rules-kit.git"
    repositories.install_url_rewrite(repo, remote)
    dependency = {"git": remote, "type": "gitlab", "ref": "main", "alias": "rules-kit"}
    project = (
        LocalPackageFactory(isolated.work_root)
        .create("consumer", dependencies=(dependency,), targets=("cursor",))
        .root
    )
    scope = ("--global",) if user_scope else ()
    state_root = isolated.config_root if user_scope else project
    if user_scope:
        dump_yaml(load_yaml(project / "apm.yml"), state_root / "apm.yml")
    target_root = (isolated.home if user_scope else project) / ".cursor"
    rules = target_root / "rules"
    rules.mkdir(parents=True)
    user_rule = rules / "personal.mdc"
    user_rule.write_text(_USER_RULE, encoding="utf-8")
    collision = rules / "collision.mdc"
    collision.write_text(_USER_COLLISION, encoding="utf-8")
    install = ("install", *scope, "--target", "cursor", "--no-policy", "--parallel-downloads", "0")

    def run(*args: str, cwd: Path = project, expected_returncode: int = 0):
        result = runner.run(args, scenario_id="cursor-rules-lifecycle", cwd=cwd, env=environment)
        assert result.returncode == expected_returncode, f"{args}\n{result.stdout}\n{result.stderr}"
        return result

    def snapshot():
        return ArtifactSnapshotSet.capture(
            {"state": state_root, **({"native": target_root} if user_scope else {})}
        )

    def lock():
        return load_yaml(state_root / "apm.lock.yaml")

    first_install = run(*install)
    assert "cursor (instructions)" not in first_install.stdout + first_install.stderr
    style = rules / "style.mdc"
    assert style.read_text(encoding="utf-8") == (
        "---\ndescription: Style rules\nglobs: **/*.py\n---\n\nCURSOR_STYLE_V1\n"
    )
    assert "CURSOR_GLOBAL" in (rules / "always.mdc").read_text(encoding="utf-8")
    assert (rules / "retired.mdc").exists()
    assert (target_root / "agents/reviewer.md").exists()
    if user_scope:
        assert not (project / ".cursor").exists()
    assert user_rule.read_text(encoding="utf-8") == _USER_RULE
    # Existing rule-dir policy (apm#1662): a same-named rule is APM-owned.
    collision_text = collision.read_text(encoding="utf-8")
    assert "PKG" in collision_text
    assert "USER_COLLISION" not in collision_text
    assert lock()["dependencies"][0]["resolved_commit"] == first.sha
    records = {
        record["value"]: record for record in lock()["deployments"] if record["target"] == "cursor"
    }
    assert records[".cursor/rules/style.mdc"]["content_hash"] == (
        "sha256:" + hashlib.sha256(style.read_bytes()).hexdigest()
    )
    assert len(records[".cursor/rules/style.mdc"]["owners"]) == 1
    assert ".cursor/rules/collision.mdc" in records
    assert ".cursor/rules/personal.mdc" not in records

    before_repeat = snapshot()
    before_mtime = style.stat().st_mtime_ns
    before_lock = (state_root / "apm.lock.yaml").read_bytes()
    run(*install)
    assert snapshot() == before_repeat
    assert style.stat().st_mtime_ns == before_mtime
    assert (state_root / "apm.lock.yaml").read_bytes() == before_lock

    (repo.worktree / ".apm/instructions/style.instructions.md").write_text(
        _STYLE.replace("V1", "V2"), encoding="utf-8"
    )
    (repo.worktree / ".apm/instructions/retired.instructions.md").unlink()
    second = repositories.commit(repo, message="update style and retire a rule")
    run("deps", "update", *scope, "--parallel-downloads", "0")
    assert "CURSOR_STYLE_V2" in style.read_text(encoding="utf-8")
    assert not (rules / "retired.mdc").exists()
    assert lock()["dependencies"][0]["resolved_commit"] == second.sha
    assert user_rule.read_text(encoding="utf-8") == _USER_RULE

    frozen_before = snapshot()
    run(*install, "--frozen")
    assert snapshot() == frozen_before
    audit_before = snapshot()
    report = json.loads(run("audit", "--no-policy", "--format", "json", cwd=state_root).stdout)
    assert report["passed"] is True
    assert snapshot() == audit_before
    edited = rules / "edited.mdc"
    edited.write_text("USER_EDITED_MANAGED_RULE\n", encoding="utf-8")
    audit_before = snapshot()
    drift = json.loads(
        run(
            "audit",
            "--ci",
            "--no-policy",
            "--format",
            "json",
            cwd=state_root,
            expected_returncode=1,
        ).stdout
    )
    assert {check["name"] for check in drift["checks"] if not check["passed"]} == {
        "content-integrity",
        "drift",
    }
    assert [(item["path"], item["kind"]) for item in drift["drift"]["drift"]] == [
        (".cursor/rules/edited.mdc", "modified")
    ]
    assert snapshot() == audit_before

    manifest_before = (state_root / "apm.yml").read_bytes()
    claims_before = {record["value"] for record in lock()["deployments"]}
    owner = records[".cursor/rules/style.mdc"]["owners"][0]
    removal = run("uninstall", *scope, owner, expected_returncode=1)
    assert "Retained user-edited file" in removal.stdout
    assert (state_root / "apm.yml").read_bytes() == manifest_before
    # The refusal drops already-removed files from the lockfile (#3656).
    claims = {record["value"] for record in lock()["deployments"]}
    assert ".cursor/rules/edited.mdc" in claims and claims < claims_before
    assert all((target_root.parent / value).exists() for value in claims if "://" not in value)
    assert not style.exists()
    assert not (rules / "always.mdc").exists()
    assert not collision.exists()
    assert not (target_root / "agents/reviewer.md").exists()
    assert user_rule.read_text(encoding="utf-8") == _USER_RULE
    assert edited.read_text(encoding="utf-8") == "USER_EDITED_MANAGED_RULE\n"
    saved_edit = rules / "saved-edit.mdc"
    edited.rename(saved_edit)
    run("uninstall", *scope, owner)
    assert saved_edit.read_text(encoding="utf-8") == "USER_EDITED_MANAGED_RULE\n"
    assert user_rule.read_text(encoding="utf-8") == _USER_RULE
    assert sorted(path.name for path in rules.iterdir()) == ["personal.mdc", "saved-edit.mdc"]
    assert not (state_root / "apm.lock.yaml").exists()
