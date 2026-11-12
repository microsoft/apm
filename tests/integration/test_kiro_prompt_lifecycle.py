"""Real installed-CLI Kiro prompt delivery in both scopes and package shapes.

Set APM_TEST_KIRO_NODE and APM_TEST_KIRO_BUNDLE to an owned Kiro CLI 2.33.0
runtime to also execute its hash-pinned native loader against deployed files.
The optional native probe does not authenticate or invoke a model.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlparse

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

_BODY = (
    "KIRO_APM_V1\nFirst: ${1}\nAll: $ARGUMENTS\nTenth: ${10}\nAt: ${@}\n[Guide](../../guide.md)\n"
)
_HEADER = "---\nname: not-the-command\ndescription: Review\n---\n"


@pytest.mark.parametrize("user_scope", [False, True], ids=["project", "user"])
@pytest.mark.parametrize("monorepo", [False, True], ids=["ordinary", "monorepo-hybrid"])
def test_kiro_prompt_installed_lifecycle(
    tmp_path: Path, apm_binary_path: Path, user_scope: bool, monorepo: bool
) -> None:
    """Install, repeat, update, frozen replay, audit and remove without losing user files."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    environment["APM_NO_CACHE"] = "1"
    runner = ApmLifecycleRunner((str(apm_binary_path),), scenario_timeout_seconds=300)
    source_root = isolated.package_root / "repository"
    factory = LocalPackageFactory(source_root / "packages" if monorepo else source_root)
    package = factory.create("prompt-kit", targets=("kiro",))
    factory.add_prompt(package, "review", _HEADER + _BODY)
    factory.add_prompt(package, "retired", _HEADER + "Remove on update\n")
    factory.add_prompt(package, "edited", _HEADER + "Originally managed\n")
    factory.add_prompt(package, "workflow", "---\ninterval: daily\n---\nDo not deploy\n")
    factory.add_prompt(package, "collision", _HEADER + "Package collision\n")
    factory.add_instruction(package, "style", "---\ndescription: Style\n---\nKIRO_STYLE\n")
    factory.add_agent(
        package, "reviewer", "---\ndescription: Reviewer\ntools: [read]\n---\nReview\n"
    )
    factory.add_skill(package, "helper", "---\nname: helper\ndescription: Helper\n---\n# Helper\n")
    (package.root / "guide.md").write_text("Source guide\n", encoding="utf-8")
    if monorepo:
        # A repository-level skill must not replace the explicitly selected package.
        (source_root / "SKILL.md").write_text(
            "---\nname: root-skill\ndescription: Not selected\n---\nROOT_NOT_SELECTED\n"
        )
        sibling = factory.create("unselected", targets=("kiro",))
        factory.add_prompt(sibling, "leak", _HEADER + "SIBLING_NOT_SELECTED\n")
    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=environment)
    repo = repositories.create("prompt-kit", source_tree=source_root if monorepo else package.root)
    first = repositories.commit(repo, message="seed prompt lifecycle")
    remote = "https://gitlab.example.invalid/kiro/prompt-kit.git"
    repositories.install_url_rewrite(repo, remote)
    dependency = {"git": remote, "type": "gitlab", "ref": "main", "alias": "prompt-kit"}
    if monorepo:
        dependency["path"] = "packages/prompt-kit"
    project = (
        LocalPackageFactory(isolated.work_root)
        .create("consumer", dependencies=(dependency,), targets=("kiro",))
        .root
    )
    scope = ("--global",) if user_scope else ()
    state_root = isolated.config_root if user_scope else project
    if user_scope:
        dump_yaml(load_yaml(project / "apm.yml"), state_root / "apm.yml")
    target_root = (isolated.home if user_scope else project) / ".kiro"
    prompts = target_root / "prompts"
    prompts.mkdir(parents=True)
    user_file = prompts / "personal.md"
    user_file.write_text("USER_PROMPT\n")
    collision = prompts / "collision.md"
    collision.write_text("USER_COLLISION\n")
    install = ("install", *scope, "--target", "kiro", "--no-policy", "--parallel-downloads", "0")

    def run(*args: str, cwd: Path = project, expected_returncode: int = 0):
        result = runner.run(args, scenario_id="kiro-prompt-lifecycle", cwd=cwd, env=environment)
        assert result.returncode == expected_returncode, f"{args}\n{result.stdout}\n{result.stderr}"
        return result

    def snapshot():
        return ArtifactSnapshotSet.capture(
            {"state": state_root, **({"native": target_root} if user_scope else {})}
        )

    def lock():
        return load_yaml(state_root / "apm.lock.yaml")

    def qualify_native(phase: str) -> None:
        bundle = os.environ.get("APM_TEST_KIRO_BUNDLE")
        if not bundle:
            return
        native = subprocess.run(
            (
                os.environ["APM_TEST_KIRO_NODE"],
                str(Path(__file__).parents[1] / "utils/kiro_prompt_loader.mjs"),
                bundle,
                str(project),
                str(isolated.home),
                "user" if user_scope else "project",
            ),
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert native.returncode == 0, native.stdout + native.stderr
        (tmp_path / f"native-{phase}.json").write_text(native.stdout, encoding="utf-8")

    run(*install)
    deployed = prompts / "review.md"
    text = deployed.read_text()
    assert text.startswith("KIRO_APM_V1\nFirst: ${1}\nAll: $ARGUMENTS\n")
    assert not (prompts / "review.prompt.md").exists()
    assert not (prompts / "not-the-command.md").exists()
    assert not (prompts / "workflow.md").exists()
    assert not (prompts / "leak.md").exists()
    assert not (target_root / "skills/root-skill").exists()
    link = urlparse(text.split("](", 1)[1].split(")", 1)[0])
    assert link.scheme == ""
    assert link.netloc == ""
    assert (deployed.parent / unquote(link.path)).read_text() == "Source guide\n"
    assert "KIRO_STYLE" in (target_root / "steering/style.md").read_text()
    assert (target_root / "agents/reviewer.md").exists()
    assert (target_root / "skills/helper/SKILL.md").exists()
    assert collision.read_text() == "USER_COLLISION\n"
    assert lock()["dependencies"][0]["resolved_commit"] == first.sha
    records = {
        record["value"]: record for record in lock()["deployments"] if record["target"] == "kiro"
    }
    assert records[".kiro/prompts/review.md"]["content_hash"] == (
        "sha256:" + hashlib.sha256(deployed.read_bytes()).hexdigest()
    )
    assert len(records[".kiro/prompts/review.md"]["owners"]) == 1
    assert ".kiro/prompts/collision.md" not in records
    assert ".kiro/prompts/personal.md" not in records
    qualify_native("install")
    before_repeat = snapshot()
    before_mtime = deployed.stat().st_mtime_ns
    before_lock = (state_root / "apm.lock.yaml").read_bytes()
    run(*install)
    assert snapshot() == before_repeat
    assert deployed.stat().st_mtime_ns == before_mtime
    assert (state_root / "apm.lock.yaml").read_bytes() == before_lock

    updated_root = repo.worktree / "packages/prompt-kit" if monorepo else repo.worktree
    (updated_root / ".apm/prompts/review.prompt.md").write_text(_HEADER + _BODY.replace("V1", "V2"))
    (updated_root / ".apm/prompts/retired.prompt.md").unlink()
    second = repositories.commit(repo, message="update prompt and remove retired command")
    run("deps", "update", *scope, "--parallel-downloads", "0")
    assert deployed.read_text().startswith("KIRO_APM_V2\n")
    assert not (prompts / "retired.md").exists()
    assert lock()["dependencies"][0]["resolved_commit"] == second.sha
    assert user_file.read_text() == "USER_PROMPT\n"
    assert collision.read_text() == "USER_COLLISION\n"
    qualify_native("update")

    frozen_before = snapshot()
    run(*install, "--frozen")
    assert snapshot() == frozen_before
    audit_before = snapshot()
    audit = run("audit", "--no-policy", "--format", "json", cwd=state_root)
    report = json.loads(audit.stdout)
    assert report["passed"] is True
    assert snapshot() == audit_before
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
    assert {check["name"] for check in drift["checks"] if not check["passed"]} == {"drift"}
    assert [(item["path"], item["kind"]) for item in drift["drift"]["drift"]] == [
        (".kiro/prompts/collision.md", "modified")
    ]
    assert snapshot() == audit_before

    edited = prompts / "edited.md"
    edited.write_text("USER_EDITED_MANAGED_PROMPT\n")
    manifest_before = (state_root / "apm.yml").read_bytes()
    lock_before = (state_root / "apm.lock.yaml").read_bytes()
    owner = records[".kiro/prompts/review.md"]["owners"][0]
    removal = run("uninstall", *scope, owner, expected_returncode=1)
    assert "Retained user-edited file" in removal.stdout
    assert (state_root / "apm.yml").read_bytes() == manifest_before
    assert (state_root / "apm.lock.yaml").read_bytes() == lock_before
    assert not deployed.exists()
    assert not (target_root / "agents/reviewer.md").exists()
    assert not (target_root / "steering/style.md").exists()
    assert user_file.read_text() == "USER_PROMPT\n"
    assert collision.read_text() == "USER_COLLISION\n"
    assert edited.read_text() == "USER_EDITED_MANAGED_PROMPT\n"
    saved_edit = prompts / "saved-edit.md"
    edited.rename(saved_edit)
    run("uninstall", *scope, owner)
    assert saved_edit.read_text() == "USER_EDITED_MANAGED_PROMPT\n"
    assert user_file.read_text() == "USER_PROMPT\n"
    assert collision.read_text() == "USER_COLLISION\n"
    assert not (state_root / "apm.lock.yaml").exists()
