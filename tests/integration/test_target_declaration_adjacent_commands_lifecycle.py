"""Adjacent commands keep package targets and dependency target subsets apart (#3732).

Promise under test (0.36.1 T2 brief, section 4 "L" rows and section 5.4 risk):
the declaration owner and the widened guard touch every reader of the
package-level ``target:`` / ``targets:`` field, but a per-dependency
``targets:`` subset is a different concept and must keep working.

* Read-only commands (``compile --dry-run``, ``deps list/tree/why``, ``view``,
  ``audit --ci``, ``export-patch --dry-run``) never rewrite ``apm.yml`` or the
  lockfile, whatever the package target spelling.
* ``uninstall`` and ``prune`` keep the package target declaration verbatim and
  keep the remaining dependency's ``targets:`` subset.
* A dependency subset narrows deployment and is never judged by
  compilation-target policy.
* Re-initialising a project with ``init --yes`` keeps the deployment set a
  singular ``target: all`` selects (decision D1).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration._policy_scope_kit import output
from tests.integration._target_policy_kit import (
    SELF_DEFINED_MCP,
    block_policy,
    target_policy_world,
)
from tests.utils.local_github_https_fixture import openssl_executable

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.skipif(openssl_executable() is None, reason="openssl CLI not available"),
]

CLAUDE_COPILOT = {"allow": ["claude", "copilot"]}


def _dep(name: str, *subset: str) -> dict:
    return {"git": f"https://github.com/contoso/{name}", "ref": "v1.0.0", "targets": list(subset)}


def _declaration(project: Path) -> dict:
    """The package-level ``target:`` / ``targets:`` keys as parsed YAML."""
    manifest = load_yaml(project / "apm.yml") or {}
    return {key: manifest[key] for key in ("target", "targets") if key in manifest}


@pytest.mark.parametrize(
    "declaration",
    [{"targets": ["claude", "copilot"]}, {"target": "claude,copilot"}],
    ids=["plural", "singular-csv"],
)
def test_adjacent_commands_keep_package_targets_and_dependency_subsets(
    declaration: dict, tmp_path: Path, apm_binary_path: Path
) -> None:
    with target_policy_world(
        tmp_path, apm_binary_path, block_policy(CLAUDE_COPILOT), packages=("pkg-a", "pkg-b")
    ) as tw:
        project = tw.consumer(
            "consumer",
            declaration,
            dependencies=[_dep("pkg-a", "claude"), _dep("pkg-b", "cursor")],
        )
        tw.run(project, "install", step="1-install")
        assert (project / ".claude/skills/pkg-a-skill/SKILL.md").is_file()
        assert not (project / ".agents/skills/pkg-a-skill").exists(), "subset ignored"
        assert not list(project.glob(".*/skills/pkg-b-skill")), "cursor-only dep deployed"

        manifest = (project / "apm.yml").read_bytes()
        lock = (project / "apm.lock.yaml").read_bytes()
        for step, args in (
            ("2-compile-dry-run", ("compile", "--dry-run")),
            ("3-deps-list", ("deps", "list")),
            ("4-deps-tree", ("deps", "tree")),
            ("5-deps-why", ("deps", "why", "contoso/pkg-a")),
            ("6-view", ("view", "contoso/pkg-a")),
            ("7-install-frozen", ("install", "--frozen")),
            ("8-update", ("update", "--yes")),
            ("9-export-patch", ("export-patch", "--dry-run")),
        ):
            tw.run(project, *args, step=step)
            assert (project / "apm.yml").read_bytes() == manifest, f"[{step}] rewrote apm.yml"
            assert (project / "apm.lock.yaml").read_bytes() == lock, f"[{step}] lockfile churn"

        _result, check = tw.audit_read_only(project, step="10-audit")
        assert check["passed"] is True, check
        assert "cursor" not in str(check), check

        tw.run(project, "uninstall", "contoso/pkg-b", step="11-uninstall")
        assert _declaration(project) == declaration, (project / "apm.yml").read_text()
        remaining = load_yaml(project / "apm.yml")["dependencies"]["apm"]
        assert remaining == [_dep("pkg-a", "claude")], remaining

        tw.run(project, "prune", step="12-prune")
        tw.run(project, "install", step="13-reinstall")
        assert _declaration(project) == declaration
        assert (project / ".claude/skills/pkg-a-skill/SKILL.md").is_file()
        assert not (project / ".agents/skills/pkg-a-skill").exists()


@pytest.mark.parametrize(
    "declaration",
    [{"targets": ["claude", "copilot"]}, {"target": "claude,copilot"}, {"target": "all"}],
    ids=["plural", "singular-csv", "singular-all"],
)
def test_export_patch_finds_local_edits_for_every_spelling(
    declaration: dict, tmp_path: Path, apm_binary_path: Path
) -> None:
    with target_policy_world(tmp_path, apm_binary_path, block_policy()) as tw:
        project = tw.consumer("consumer", declaration)
        tw.run(project, "install", step="1-install")
        skill = project / ".claude/skills/pkg-a-skill/SKILL.md"
        skill.write_text(skill.read_text(encoding="utf-8") + "local edit\n", encoding="utf-8")
        manifest = (project / "apm.yml").read_bytes()
        result = tw.run(project, "export-patch", "--dry-run", step="2-export-patch")
        assert "pkg-a" in output(result), output(result)
        assert "No local edits" not in output(result), output(result)
        assert (project / "apm.yml").read_bytes() == manifest


def test_reinit_keeps_the_singular_all_deployment_set(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    with target_policy_world(tmp_path, apm_binary_path, block_policy()) as tw:
        project = tw.consumer("consumer", {"target": "all"}, dirs=(".claude",))
        tw.run(project, "install", step="1-install")
        expanded = sorted(p.parent.parent.parent.name for p in project.glob(".*/skills/*/SKILL.md"))
        assert len(expanded) > 1, expanded

        tw.run(project, "init", "--yes", step="2-reinit")
        tw.run(project, "install", "contoso/pkg-a#v1.0.0", step="3-readd")
        after = sorted(p.parent.parent.parent.name for p in project.glob(".*/skills/*/SKILL.md"))
        assert after == expanded, (
            f"re-init narrowed target: all from {expanded} to {after}:\n"
            f"{(project / 'apm.yml').read_text(encoding='utf-8')}"
        )
        # The rewritten spellings name copilot (never the vscode alias), whose
        # project MCP destination is the workspace-root .mcp.json (#3866).
        manifest = load_yaml(project / "apm.yml") or {}
        assert "copilot" in manifest.get("targets", []), manifest
        assert "vscode" not in manifest.get("targets", []), manifest
        manifest.setdefault("dependencies", {})["mcp"] = [dict(SELF_DEFINED_MCP)]
        dump_yaml(manifest, project / "apm.yml")
        tw.run(project, "install", step="4-mcp")
        assert (project / ".mcp.json").is_file(), "re-init lost the Copilot MCP destination"
        lock = load_yaml(project / "apm.lock.yaml") or {}
        assert "copilot" in (lock.get("mcp_target_servers") or {}), lock
        for retired in (".vscode/mcp.json", ".github/mcp.json"):
            assert not (project / retired).exists(), f"re-init wrote {retired}"
