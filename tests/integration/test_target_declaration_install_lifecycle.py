"""Manifest target declarations install consistently across spellings and scopes (#3732).

Promise under test (0.36.1 T2 brief, decisions C and D1, fact F12):

* A singular ``target:`` naming a harness that install cannot deploy
  (``intellij``, ``copilot-app``) is rejected before any write: no lockfile,
  no deployed primitives, no ``.gitignore`` edit.
* A singular ``target: all`` keeps expanding to the install set (D1), and the
  MCP phase no longer claims the field is "treated as omitted" in the same
  run (F10). A plural ``targets: [all]`` still folds, with the warning.
* The MCP destination depends on the runtime spelling, identically for
  ``target:`` and ``targets:``: ``vscode`` writes ``.vscode/mcp.json`` and
  ``copilot`` writes ``.github/mcp.json`` (F12, regression lock).
* ``apm install -g`` reads ``~/.apm/apm.yml`` through the same declaration
  owner, so every spelling of the same set deploys the same files (L7).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration._policy_scope_kit import output, tree_snapshot
from tests.integration._target_policy_kit import (
    SELF_DEFINED_MCP,
    TREATED_AS_OMITTED,
    block_policy,
    changed_paths,
    target_policy_world,
)
from tests.utils.local_github_https_fixture import openssl_executable

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.skipif(openssl_executable() is None, reason="openssl CLI not available"),
]


@pytest.mark.parametrize("token", ["intellij", "copilot-app"])
def test_undeployable_singular_target_is_rejected_before_any_write(
    token: str, tmp_path: Path, apm_binary_path: Path
) -> None:
    with target_policy_world(tmp_path, apm_binary_path, block_policy()) as tw:
        project = tw.consumer("consumer", {"target": token})
        before = tree_snapshot(project)
        result = tw.run(project, "install", step="1-install", expected=(1, 2))
        assert result.returncode == 2, output(result)
        assert token in output(result), output(result)
        after = tree_snapshot(project)
        assert after == before, f"rejected install wrote: {changed_paths(before, after)}"
        assert not (project / "apm.lock.yaml").exists()
        assert not (project / ".github").exists()
        assert not (project / ".agents").exists()


def test_singular_all_expands_without_claiming_it_was_omitted(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    with target_policy_world(tmp_path, apm_binary_path, block_policy()) as tw:
        project = tw.consumer("consumer", {"target": "all"}, mcp=[SELF_DEFINED_MCP])
        result = tw.run(project, "install", step="1-install")
        text = " ".join(output(result).split())
        assert TREATED_AS_OMITTED not in text, text
        # D1: the expansion still deploys primitives and MCP to many harnesses.
        for deployed in (
            ".claude/skills/pkg-a-skill/SKILL.md",
            ".agents/skills/pkg-a-skill/SKILL.md",
            ".kiro/skills/pkg-a-skill/SKILL.md",
            ".vscode/mcp.json",
            ".cursor/mcp.json",
            ".mcp.json",
        ):
            assert (project / deployed).is_file(), f"target: all no longer deploys {deployed}"
        manifest = (project / "apm.yml").read_bytes()
        result = tw.run(project, "install", step="2-reinstall")
        assert TREATED_AS_OMITTED not in " ".join(output(result).split())
        assert (project / "apm.yml").read_bytes() == manifest


def test_plural_all_still_folds_to_auto_detect_with_warning(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    with target_policy_world(tmp_path, apm_binary_path, block_policy()) as tw:
        project = tw.consumer(
            "consumer", {"targets": ["all"]}, mcp=[SELF_DEFINED_MCP], dirs=(".claude",)
        )
        result = tw.run(project, "install", step="1-install")
        text = " ".join(output(result).split())
        assert "deprecated" in text, text
        assert (project / ".claude/skills/pkg-a-skill/SKILL.md").is_file()
        assert not (project / ".cursor").exists(), "targets: [all] expanded beyond auto-detect"
        assert not (project / ".agents").exists()


@pytest.mark.parametrize(
    ("declaration", "destination", "absent"),
    [
        ({"target": "vscode"}, ".vscode/mcp.json", ".github/mcp.json"),
        ({"targets": ["vscode"]}, ".vscode/mcp.json", ".github/mcp.json"),
        ({"target": "copilot"}, ".github/mcp.json", ".vscode/mcp.json"),
        ({"targets": ["copilot"]}, ".github/mcp.json", ".vscode/mcp.json"),
    ],
    ids=["singular-vscode", "plural-vscode", "singular-copilot", "plural-copilot"],
)
def test_mcp_destination_follows_runtime_spelling(
    declaration: dict, destination: str, absent: str, tmp_path: Path, apm_binary_path: Path
) -> None:
    policy = block_policy({"allow": ["claude", "copilot"]})
    with target_policy_world(tmp_path, apm_binary_path, policy) as tw:
        project = tw.consumer("consumer", declaration, dependencies=[], mcp=[SELF_DEFINED_MCP])
        tw.run(project, "install", step="1-install")
        assert (project / destination).is_file(), f"{declaration} did not write {destination}"
        assert not (project / absent).exists(), f"{declaration} also wrote {absent}"
        lock = load_yaml(project / "apm.lock.yaml") or {}
        runtime = "vscode" if destination.startswith(".vscode") else "copilot"
        assert list((lock.get("mcp_target_servers") or {}).keys()) == [runtime], lock


@pytest.mark.parametrize(
    "declaration",
    [
        {"targets": ["claude", "cursor"]},
        {"target": "claude,cursor"},
        {"target": ["claude", "cursor"]},
    ],
    ids=["plural", "singular-csv", "singular-list"],
)
def test_user_scope_reads_declared_targets_for_every_spelling(
    declaration: dict, tmp_path: Path, apm_binary_path: Path
) -> None:
    with target_policy_world(tmp_path, apm_binary_path, block_policy()) as tw:
        isolated = tw.world.isolated
        manifest = {"name": "user", "version": "0.1.0", **declaration}
        manifest["dependencies"] = {"apm": ["contoso/pkg-a#v1.0.0"]}
        dump_yaml(manifest, isolated.config_root / "apm.yml")
        cwd = isolated.work_root
        tw.run(cwd, "install", "-g", step="1-install-g")
        home = isolated.home
        assert (home / ".claude/skills/pkg-a-skill/SKILL.md").is_file()
        assert (home / ".agents/skills/pkg-a-skill/SKILL.md").is_file()
        assert not (home / ".copilot").exists(), "undeclared copilot received files"
        lock = load_yaml(isolated.config_root / "apm.lock.yaml") or {}
        targets = sorted({row.get("target") for row in lock.get("deployments") or []})
        assert targets == ["claude", "cursor"], lock.get("deployments")
        before = (isolated.config_root / "apm.yml").read_bytes()
        tw.run(cwd, "compile", "-g", step="2-compile-g")
        assert (isolated.config_root / "apm.yml").read_bytes() == before


def test_user_scope_singular_all_expands_without_claiming_it_was_omitted(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    with target_policy_world(tmp_path, apm_binary_path, block_policy()) as tw:
        isolated = tw.world.isolated
        manifest = {"name": "user", "version": "0.1.0", "target": "all"}
        manifest["dependencies"] = {"apm": ["contoso/pkg-a#v1.0.0"]}
        dump_yaml(manifest, isolated.config_root / "apm.yml")
        result = tw.run(isolated.work_root, "install", "-g", step="1-install-g")
        assert TREATED_AS_OMITTED not in " ".join(output(result).split()), output(result)
        assert (isolated.home / ".claude/skills/pkg-a-skill/SKILL.md").is_file()
        assert (isolated.home / ".config/opencode/skills/pkg-a-skill/SKILL.md").is_file()
