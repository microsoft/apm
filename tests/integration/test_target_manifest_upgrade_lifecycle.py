"""Projects written by APM 0.35.1 and 0.36.0 upgrade without target churn (#3732).

Promise under test (0.36.1 T2 brief, sections 4 and 5.5, decision D1): a
project whose ``apm.yml`` and ``apm.lock.yaml`` were produced by 0.35.1 or
0.36.0, with any target spelling (``target:`` scalar, comma-separated, alias,
``all``; ``targets:`` list, alias, ``all``), keeps working on the fixed build:

* ``install``, ``install --frozen``, ``update`` and ``compile --dry-run`` never
  rewrite the manifest and never change the deployed target set or the
  lockfile's deployment rows, except for the one move #3866 makes on the first
  install: Copilot project MCP servers leave ``.github/mcp.json`` (and the
  former ``vscode`` alias's ``.vscode/mcp.json``) for ``.mcp.json``, and their
  deployment rows are recorded for ``copilot``. Nothing changes after that.
* ``audit --ci`` writes nothing.
* Only compilation-target policy verdicts change, exactly as the brief
  predicts: comma-separated and alias spellings stop failing, ``target: all``
  is judged by its expansion instead of the literal ``all``.

Fixtures under ``tests/fixtures/upgrade_0361/t2`` were produced by the tagged
source; see the README there.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import load_yaml
from tests.integration._policy_scope_kit import output, tree_snapshot
from tests.integration._target_policy_kit import (
    block_policy,
    changed_paths,
    quoted_tokens,
    target_policy_world,
    violation_text,
)
from tests.utils.local_github_https_fixture import openssl_executable

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.skipif(openssl_executable() is None, reason="openssl CLI not available"),
]

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "upgrade_0361" / "t2"
VERSIONS = ("v0.35.1", "v0.36.0")
# Compilation-target verdict under ``allow: [claude, copilot]`` on the fixed build.
VERDICTS = {
    "singular": True,
    "singular-csv": True,
    "singular-alias": True,
    "singular-all": False,
    "plural": True,
    "plural-alias": True,
    "plural-all": True,
}
_UNSTABLE_KEYS = frozenset({"apm_version", "generated_at", "content_hash", "deployed_file_hashes"})


def _normalized_lock(path: Path) -> object:
    """Lockfile without version stamps and content hashes (stable across releases)."""

    def strip(node: object) -> object:
        if isinstance(node, dict):
            return {k: strip(v) for k, v in node.items() if k not in _UNSTABLE_KEYS}
        if isinstance(node, list):
            return [strip(item) for item in node]
        return node

    return strip(load_yaml(path) or {})


def _deployment_rows(path: Path) -> set[tuple]:
    lock = load_yaml(path) or {}
    return {
        (row.get("kind"), row.get("target"), row.get("value"), row.get("runtime"))
        for row in lock.get("deployments") or []
    }


def _project_files(project: Path) -> dict[str, str]:
    return tree_snapshot(project, exclude=(".git", "apm_modules", ".apm-local"))


_RETIRED_MCP_FILES = frozenset({".github/mcp.json", ".vscode/mcp.json"})


def _after_mcp_move(rows: set[tuple], files: set[str]) -> tuple[set[tuple], set[str]]:
    """The deployment rows and files #3866's one-time MCP destination move yields."""
    moved_rows = {
        (kind, target, value, "copilot" if runtime == "vscode" else runtime)
        for kind, target, value, runtime in rows
    }
    moved_files = set(files) - _RETIRED_MCP_FILES
    if files & _RETIRED_MCP_FILES:
        moved_files.add(".mcp.json")
    return moved_rows, moved_files


@pytest.mark.parametrize("case", list(VERDICTS))
@pytest.mark.parametrize("version", VERSIONS)
def test_upgraded_project_keeps_targets_and_only_policy_verdicts_change(
    version: str, case: str, tmp_path: Path, apm_binary_path: Path
) -> None:
    source = FIXTURES / version
    with target_policy_world(tmp_path, apm_binary_path, block_policy()) as tw:
        world = tw.world
        shutil.copytree(source / "pkg", world.isolated.work_root / "pkg")
        project = world.consumer(case)
        shutil.copytree(source / case, project, dirs_exist_ok=True)
        manifest = (project / "apm.yml").read_bytes()
        lock_path = project / "apm.lock.yaml"
        original_rows, original_paths = _after_mcp_move(
            _deployment_rows(lock_path), set(_project_files(project))
        )
        settled_files: dict[str, str] | None = None
        settled_lock_bytes: bytes | None = None

        for step, args in (
            ("1-install", ("install",)),
            ("2-install-frozen", ("install", "--frozen")),
            ("3-update", ("update", "--yes")),
            ("4-compile-dry-run", ("compile", "--dry-run")),
        ):
            result = tw.run(project, *args, step=step, expected=(0, 1))
            assert result.returncode == 0, f"[{step}] {output(result)}"
            assert (project / "apm.yml").read_bytes() == manifest, f"[{step}] rewrote apm.yml"
            assert _deployment_rows(lock_path) == original_rows, f"[{step}] deployment churn"
            files = _project_files(project)
            assert set(files) == original_paths, (
                f"[{step}] deployed set changed: {sorted(set(files) ^ original_paths)}"
            )
            if settled_files is None:
                settled_files, settled_lock_bytes = files, lock_path.read_bytes()
            elif version == "v0.36.0":
                assert lock_path.read_bytes() == settled_lock_bytes, f"[{step}] lockfile churn"
                assert files == settled_files, (
                    f"[{step}] content churn: {changed_paths(settled_files, files)}"
                )
        settled_lock = lock_path.read_bytes()
        fixture_lock = _normalized_lock(source / case / "apm.lock.yaml")
        settled = _normalized_lock(lock_path)
        for lock in (fixture_lock, settled):
            lock.pop("deployments", None)
            servers = lock.get("mcp_target_servers") or {}
            if "vscode" in servers:
                servers["copilot"] = sorted({*servers.pop("vscode"), *servers.get("copilot", ())})
        assert settled == fixture_lock

        before = tree_snapshot(project)
        tw.run(project, "audit", "--ci", step="5-audit-unrestricted")
        assert tree_snapshot(project) == before, "audit --ci wrote project files"

        world.publish_policy(block_policy({"allow": ["claude", "copilot"]}))
        world.clear_policy_cache()
        expected = VERDICTS[case]

        result, check = tw.audit_read_only(project, step="6-audit-restricted")
        assert check["passed"] is expected, f"{case}: {check}"
        assert result.returncode == (0 if expected else 1), output(result)
        assert "all" not in quoted_tokens(str(check.get("message"))), check

        before = tree_snapshot(project)
        result = tw.run(project, "install", step="7-install-restricted", expected=(0, 1))
        assert result.returncode == (0 if expected else 1), output(result)
        if expected:
            assert lock_path.read_bytes() == settled_lock, "allowed reinstall churned the lock"
        else:
            after = tree_snapshot(project)
            assert after == before, f"blocked install wrote: {changed_paths(before, after)}"
            assert "all" not in quoted_tokens(violation_text(result)), output(result)
        assert (project / "apm.yml").read_bytes() == manifest
