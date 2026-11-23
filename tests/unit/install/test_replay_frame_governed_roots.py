"""Drift replay frames and walks nested or mixed harness layouts accurately (#5162).

PROMISE: P-INV-I3 -- install --frozen and audit --ci agree on kept and
deployed files: a kept claim replays in its own harness's (deepest) external
root whatever the target order, and the drift walk covers the converged skills
root of a project lock that mixes legacy and converged layouts, so a tampered
file there is drift (source: #5162 scope record).
owner: install/deployment_plan.py carry_kept_loose_into_replay,
install/audit_target_roots.py governed_root_dirs
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from apm_cli.claude_plugins.admission import ClaudeNativePlugin
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.install.audit_target_roots import governed_root_dirs
from apm_cli.install.deployment_plan import PackageDeploymentPlan, carry_kept_loose_into_replay
from apm_cli.install.drift import diff_scratch_against_project
from apm_cli.integration.targets import KNOWN_TARGETS, apply_legacy_skill_paths

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-INV-I3")]


def _parked_plan() -> PackageDeploymentPlan:
    native = ClaudeNativePlugin(
        "pkg", "demo", Path("/src"), "sha256:x", parked="Parked native plugin pkg: denied"
    )
    selection = SimpleNamespace(targets=[KNOWN_TARGETS["claude"], KNOWN_TARGETS["codex"]])
    return PackageDeploymentPlan(selection, claude_native=native)


@pytest.mark.parametrize("order", ["hermes-first", "claude-first"])
def test_a_kept_claim_replays_in_its_own_nested_external_root(tmp_path: Path, order: str) -> None:
    # HERMES_HOME=/p and CLAUDE_CONFIG_DIR=/p/claude: the Claude root is the deeper one.
    profiles = tmp_path.resolve() / "profiles"
    skill = profiles / "claude" / "skills" / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("kept", encoding="ascii")
    hermes = replace(KNOWN_TARGETS["hermes"], root_dir=str(profiles))
    claude = replace(KNOWN_TARGETS["claude"], root_dir=str(profiles / "claude"))
    live = [hermes, claude] if order == "hermes-first" else [claude, hermes]
    value = skill.as_posix()
    lock_dep = SimpleNamespace(deployed_files=[value], deployed_file_hashes={value: "sha256:a"})
    scratch = tmp_path.resolve() / "scratch"

    carry_kept_loose_into_replay(
        {"deployment_plan": _parked_plan()}, lock_dep, tmp_path / "home", scratch, True, live
    )

    replayed = scratch / ".apm-audit-targets/claude/skills/demo/SKILL.md"
    assert replayed.read_text(encoding="ascii") == "kept"
    assert not (scratch / ".apm-audit-targets/hermes").exists()


def test_a_legacy_layout_opencode_project_walks_its_converged_skills_root() -> None:
    (legacy,) = apply_legacy_skill_paths([KNOWN_TARGETS["opencode"]])

    roots = governed_root_dirs([legacy])

    assert {".opencode", ".agents"} <= roots


def test_user_scope_opencode_walks_only_its_own_root() -> None:
    # At user scope OpenCode's skills override owns the skills root: nothing to add.
    user = KNOWN_TARGETS["opencode"].for_scope(user_scope=True)

    assert ".agents" not in governed_root_dirs([user])


def test_a_tampered_converged_skill_beside_legacy_opencode_claims_is_drift(
    tmp_path: Path,
) -> None:
    (legacy,) = apply_legacy_skill_paths([KNOWN_TARGETS["opencode"]])
    scratch, project = tmp_path / "scratch", tmp_path / "project"
    files = {".opencode/skills/a/SKILL.md": "a", ".agents/skills/b/SKILL.md": "b"}
    for root in (scratch, project):
        for rel, body in files.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(body, encoding="utf-8")
    lock = LockFile()
    lock.add_dependency(LockedDependency(repo_url="owner/a", deployed_files=[*files][:1]))
    lock.add_dependency(LockedDependency(repo_url="owner/b", deployed_files=[*files][1:]))
    assert diff_scratch_against_project(scratch, project, lock, [legacy]) == []

    (project / ".agents/skills/b/SKILL.md").write_text("tampered", encoding="utf-8")

    findings = diff_scratch_against_project(scratch, project, lock, [legacy])
    assert [(f.path, f.kind) for f in findings] == [(".agents/skills/b/SKILL.md", "modified")]
