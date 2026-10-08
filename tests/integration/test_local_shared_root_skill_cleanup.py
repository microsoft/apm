"""Install regressions for local skills deployed under the shared ``.agents`` root (#3179)."""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
from apm_cli.deps.lockfile import LockFile
from apm_cli.models.apm_package import clear_apm_yml_cache
from apm_cli.utils.content_hash import compute_file_hash

pytestmark = pytest.mark.component

_PATCH_UPDATES = "apm_cli.commands._helpers.check_for_updates"


def _write_skill(root: Path, name: str) -> None:
    skill = root / ".apm" / "skills" / name
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Demo\n---\n\nBody.\n", encoding="utf-8"
    )


def _write_project(project: Path, targets: tuple[str, ...] | None) -> None:
    """Write a consumer with one local dependency so the lockfile phase runs in full."""
    dependency = project / "dep"
    if not dependency.exists():
        dependency.mkdir()
        (dependency / "apm.yml").write_text("name: dep\nversion: 1.0.0\n", encoding="utf-8")
        _write_skill(dependency, "dep-skill")
    declared = "".join(f"  - {target}\n" for target in targets or ())
    (project / "apm.yml").write_text(
        "name: shared-root-consumer\nversion: 1.0.0\n"
        + (f"targets:\n{declared}" if targets else "")
        + "dependencies:\n  apm:\n    - ./dep\n",
        encoding="utf-8",
    )


def _rename_skill(project: Path, old: str, new: str) -> None:
    skills = project / ".apm" / "skills"
    (skills / old).rename(skills / new)
    _write_skill(project, new)


def _run(project: Path, monkeypatch: pytest.MonkeyPatch, *argv: str) -> None:
    clear_apm_yml_cache()
    monkeypatch.chdir(project)
    with patch(_PATCH_UPDATES, return_value=None):
        result = CliRunner().invoke(cli, list(argv), catch_exceptions=False)
    assert result.exit_code == 0, result.output


def _install(project: Path, monkeypatch: pytest.MonkeyPatch, *args: str) -> None:
    _run(project, monkeypatch, "install", *args)


def _lock(project: Path) -> dict:
    return yaml.safe_load((project / "apm.lock.yaml").read_text(encoding="utf-8"))


def _deployment_targets(lock: dict) -> dict[str, str]:
    return {row["value"]: row["target"] for row in lock.get("deployments") or []}


def _mark_shared_root_rows_unattributed(project: Path, *, owner: str) -> None:
    """Rewrite *owner*'s ``.agents`` rows as an earlier release recorded them."""
    lock = _lock(project)
    for row in lock["deployments"]:
        if row["value"].startswith(".agents/") and row["owners"] == [owner]:
            row["target"] = "legacy"
    (project / "apm.lock.yaml").write_text(yaml.safe_dump(lock, sort_keys=False), encoding="utf-8")


@pytest.mark.parametrize(
    ("targets", "flags"),
    [(("claude", "copilot"), ()), (None, ("--target", "claude,copilot"))],
    ids=["apm-yml-targets", "target-flag-only"],
)
def test_renamed_local_skill_leaves_no_shared_root_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    targets: tuple[str, ...] | None,
    flags: tuple[str, ...],
) -> None:
    """A rename removes the old copy from every target root, not just dedicated ones."""
    _write_project(tmp_path, targets)
    _write_skill(tmp_path, "demo-v1")
    _install(tmp_path, monkeypatch, *flags)

    _rename_skill(tmp_path, "demo-v1", "demo-v2")
    _install(tmp_path, monkeypatch, *flags)

    lock = _lock(tmp_path)
    assert lock["local_deployed_files"] == [
        ".agents/skills/demo-v2",
        ".agents/skills/demo-v2/SKILL.md",
        ".claude/skills/demo-v2",
        ".claude/skills/demo-v2/SKILL.md",
    ]
    assert _deployment_targets(lock)[".agents/skills/demo-v2/SKILL.md"] == "copilot"
    assert not (tmp_path / ".agents" / "skills" / "demo-v1").exists()


def test_dropped_target_removes_local_shared_root_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Removing the only ``.agents/skills`` target removes the local copy there."""
    _write_project(tmp_path, ("claude", "copilot"))
    _write_skill(tmp_path, "demo")
    _install(tmp_path, monkeypatch)

    _write_project(tmp_path, ("claude",))
    _install(tmp_path, monkeypatch)

    assert _lock(tmp_path)["local_deployed_files"] == [
        ".claude/skills/demo",
        ".claude/skills/demo/SKILL.md",
    ]
    assert not (tmp_path / ".agents" / "skills" / "demo").exists()


def test_install_repairs_unattributed_local_rows_from_older_lockfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rows written as ``target: legacy`` by earlier releases still contract."""
    _write_project(tmp_path, ("claude", "copilot"))
    _write_skill(tmp_path, "demo-v1")
    _install(tmp_path, monkeypatch)
    _mark_shared_root_rows_unattributed(tmp_path, owner=".")

    _rename_skill(tmp_path, "demo-v1", "demo-v2")
    _install(tmp_path, monkeypatch)

    lock = _lock(tmp_path)
    assert ".agents/skills/demo-v1/SKILL.md" not in lock["local_deployed_files"]
    assert _deployment_targets(lock)[".agents/skills/demo-v2/SKILL.md"] == "copilot"
    assert not (tmp_path / ".agents" / "skills" / "demo-v1").exists()


def test_dependency_rows_follow_a_target_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Carried-forward local provenance never pins dependency rows to an old target."""
    _write_project(tmp_path, ("claude", "copilot"))
    _write_skill(tmp_path, "demo")
    _install(tmp_path, monkeypatch)

    _write_project(tmp_path, ("claude", "codex"))
    _install(tmp_path, monkeypatch)

    targets = _deployment_targets(_lock(tmp_path))
    assert targets[".agents/skills/dep-skill/SKILL.md"] == "codex"
    assert targets[".agents/skills/demo/SKILL.md"] == "codex"


def _record_bundle(project: Path, name: str) -> str:
    """Record ``.agents`` output the way ``apm install <bundle>`` does."""
    bundled = f".agents/skills/{name}/SKILL.md"
    (project / bundled).parent.mkdir(parents=True, exist_ok=True)
    (project / bundled).write_text("bundled", encoding="utf-8")
    lockfile = LockFile.read(project / "apm.lock.yaml")
    DeploymentLedgerCodec.record_local_bundle_files(
        lockfile, [bundled], {bundled: compute_file_hash(project / bundled)}
    )
    lockfile.save(project / "apm.lock.yaml")
    return bundled


def _assert_bundle_kept(project: Path, bundled: str) -> None:
    assert (project / bundled).exists()
    assert DeploymentLedgerCodec.local_bundle_paths(
        LockFile.read(project / "apm.lock.yaml")
    ) == frozenset({bundled})


@pytest.mark.parametrize(
    "between",
    [("install",), ("uninstall", "./dep"), ("install", "--legacy-skill-paths")],
    ids=["install", "uninstall-dependency", "legacy-skill-path-migration"],
)
def test_local_bundle_output_survives_lockfile_mutations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, between: tuple[str, ...]
) -> None:
    """Shared-root bundle output keeps its provenance through commands that rewrite rows."""
    _write_project(tmp_path, ("claude", "copilot"))
    _write_skill(tmp_path, "demo")
    _install(tmp_path, monkeypatch)
    bundled = _record_bundle(tmp_path, "bundled")

    _run(tmp_path, monkeypatch, *between)
    _install(tmp_path, monkeypatch)

    _assert_bundle_kept(tmp_path, bundled)


def test_local_bundle_output_survives_an_authored_skill_coming_and_going(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Authoring a skill over bundle output never hands the path to a sweepable target."""
    _write_project(tmp_path, ("claude", "copilot"))
    _install(tmp_path, monkeypatch)
    bundled = _record_bundle(tmp_path, "shared")

    _write_skill(tmp_path, "shared")
    _install(tmp_path, monkeypatch)
    shutil.rmtree(tmp_path / ".apm" / "skills" / "shared")
    _install(tmp_path, monkeypatch)

    _assert_bundle_kept(tmp_path, bundled)


def test_narrowed_install_attributes_local_rows_to_the_run_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``--target`` run records its own target, so its next rename still contracts."""
    _write_project(tmp_path, ("codex", "copilot"))
    _write_skill(tmp_path, "demo-v1")
    _install(tmp_path, monkeypatch)
    _install(tmp_path, monkeypatch, "--target", "copilot")
    assert _deployment_targets(_lock(tmp_path))[".agents/skills/demo-v1/SKILL.md"] == "copilot"

    _rename_skill(tmp_path, "demo-v1", "demo-v2")
    _install(tmp_path, monkeypatch, "--target", "copilot")

    assert ".agents/skills/demo-v1/SKILL.md" not in _lock(tmp_path)["local_deployed_files"]
    assert not (tmp_path / ".agents" / "skills" / "demo-v1").exists()


def test_alias_target_contracts_unattributed_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``targets: [vscode]`` counts as declaring copilot when repairing old rows."""
    _write_project(tmp_path, ("vscode",))
    _write_skill(tmp_path, "demo-v1")
    _install(tmp_path, monkeypatch)
    _mark_shared_root_rows_unattributed(tmp_path, owner=".")

    _rename_skill(tmp_path, "demo-v1", "demo-v2")
    _install(tmp_path, monkeypatch)

    assert ".agents/skills/demo-v1/SKILL.md" not in _lock(tmp_path)["local_deployed_files"]
    assert not (tmp_path / ".agents" / "skills" / "demo-v1").exists()


def test_install_repairs_unattributed_dependency_rows_from_older_lockfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dependency rows written as ``target: legacy`` contract once the dependency drops a skill."""
    _write_project(tmp_path, ("claude", "copilot"))
    _write_skill(tmp_path / "dep", "dep-extra")
    _install(tmp_path, monkeypatch)
    _mark_shared_root_rows_unattributed(tmp_path, owner="./dep")

    shutil.rmtree(tmp_path / "dep" / ".apm" / "skills" / "dep-extra")
    _install(tmp_path, monkeypatch)

    dependency = next(
        row for row in _lock(tmp_path)["dependencies"] if row["repo_url"] == "_local/dep"
    )
    assert ".agents/skills/dep-extra/SKILL.md" not in dependency["deployed_files"]
    assert not (tmp_path / ".agents" / "skills" / "dep-extra").exists()


def test_deleted_local_tree_without_known_targets_stays_a_no_op(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no declared or explicit target, retained rows are left alone instead of failing."""
    (tmp_path / "apm.yml").write_text("name: no-targets\nversion: 1.0.0\n", encoding="utf-8")
    _write_skill(tmp_path, "demo")
    _install(tmp_path, monkeypatch, "--target", "claude,copilot")
    shutil.rmtree(tmp_path / ".apm")
    shutil.rmtree(tmp_path / ".claude")
    before = (tmp_path / "apm.lock.yaml").read_bytes()

    _install(tmp_path, monkeypatch)

    assert (tmp_path / "apm.lock.yaml").read_bytes() == before
    assert (tmp_path / ".agents" / "skills" / "demo" / "SKILL.md").exists()


def test_dry_run_flags_unpreviewed_local_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dry run must not imply nothing changes when local stale cleanup is pending."""
    _write_project(tmp_path, ("claude", "copilot"))
    _write_skill(tmp_path, "demo")
    _install(tmp_path, monkeypatch)
    _rename_skill(tmp_path, "demo", "demo-v2")

    def preview(*args: str) -> str:
        clear_apm_yml_cache()
        with patch(_PATCH_UPDATES, return_value=None):
            result = CliRunner().invoke(
                cli, ["install", "--dry-run", *args], catch_exceptions=False
            )
        assert result.exit_code == 0, result.output
        return " ".join(result.output.split())

    assert "project's .apm/ content" in preview()
    assert "project's .apm/ content" not in preview("--only=mcp")


def test_deleted_local_tree_is_cleaned_on_the_install_that_migrates_a_legacy_lockfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cleanup decision reads the lockfile after the legacy ``apm.lock`` migration."""
    (tmp_path / "apm.yml").write_text(
        "name: legacy-lock\nversion: 1.0.0\ntargets:\n  - claude\n  - copilot\n", encoding="utf-8"
    )
    _write_skill(tmp_path, "old")
    _install(tmp_path, monkeypatch)
    (tmp_path / "apm.lock.yaml").rename(tmp_path / "apm.lock")
    shutil.rmtree(tmp_path / ".apm")

    _install(tmp_path, monkeypatch)

    assert not (tmp_path / ".agents" / "skills" / "old").exists()
    assert not (tmp_path / ".claude" / "skills" / "old").exists()
    assert _lock(tmp_path).get("local_deployed_files") in (None, [])


def test_dry_run_tolerates_an_unreadable_lockfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The local-cleanup check never turns a dry-run preview into a failure."""
    _write_project(tmp_path, ("claude", "copilot"))
    (tmp_path / "apm.lock.yaml").write_text("x: [unclosed\n", encoding="utf-8")

    clear_apm_yml_cache()
    monkeypatch.chdir(tmp_path)
    with patch(_PATCH_UPDATES, return_value=None):
        result = CliRunner().invoke(cli, ["install", "--dry-run"], catch_exceptions=False)

    assert result.exit_code == 0, result.output


def test_deleted_local_tree_is_cleaned_for_a_detected_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without declared targets, a detected harness still reconciles its own rows."""
    (tmp_path / "apm.yml").write_text("name: detected\nversion: 1.0.0\n", encoding="utf-8")
    (tmp_path / ".claude").mkdir()
    _write_skill(tmp_path, "demo")
    _install(tmp_path, monkeypatch)
    assert (tmp_path / ".claude" / "skills" / "demo" / "SKILL.md").exists()
    shutil.rmtree(tmp_path / ".apm")

    _install(tmp_path, monkeypatch)

    assert not (tmp_path / ".claude" / "skills" / "demo").exists()
    assert _lock(tmp_path).get("local_deployed_files") in (None, [])
