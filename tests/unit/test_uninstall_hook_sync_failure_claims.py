"""A hook file uninstall cannot remove keeps its lock claim (#4851)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from apm_cli.cli import cli

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise("P-OWN-file-level-deploy-sync-cleanup"),
]

_HOOK = {
    "hooks": {
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo t"}]}]
    }
}


def _hook_package(root: Path) -> None:
    hooks = root / ".apm" / "hooks"
    hooks.mkdir(parents=True)
    (root / "apm.yml").write_text("name: hooked\nversion: 1.0.0\n", encoding="ascii")
    (hooks / "pretool.json").write_text(json.dumps(_HOOK), encoding="ascii")


def _claims(project: Path) -> dict[str, list[str]]:
    data = yaml.safe_load((project / "apm.lock.yaml").read_text(encoding="utf-8")) or {}
    return {
        row.get("name") or row.get("repo_url"): row.get("deployed_files") or []
        for row in data.get("dependencies") or []
    }


def test_uninstall_keeps_the_claim_of_a_hook_sync_could_not_remove(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _hook_package(tmp_path / "pkg")
    project = tmp_path / "app"
    project.mkdir()
    (project / "apm.yml").write_text(
        "name: app\nversion: 1.0.0\ntargets: [copilot]\n"
        "dependencies:\n  apm:\n    - path: ../pkg\n",
        encoding="ascii",
    )
    monkeypatch.chdir(project)
    runner = CliRunner()
    install = runner.invoke(cli, ["install", "--target", "copilot"])
    assert install.exit_code == 0, install.output
    hooks = [path for path in _claims(project).get("hooked", []) if "/hooks/" in path]
    assert hooks, _claims(project)
    targets = {(project / path).resolve() for path in hooks}
    unlink = Path.unlink
    attempts: list[Path] = []

    def race_then_fail(self: Path, *args, **kwargs) -> None:
        # Selected cleanup's unlink is undone (the hook reappears), and the
        # later hook-sync unlink of the same file is denied.
        if self.resolve() in targets:
            attempts.append(self)
            if len(attempts) > 1:
                raise PermissionError(13, "Permission denied", str(self))
            return
        unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", race_then_fail)

    uninstall = runner.invoke(cli, ["uninstall", "../pkg"])

    monkeypatch.undo()
    assert len(attempts) >= 2, uninstall.output
    assert uninstall.exit_code == 1, uninstall.output
    assert all((project / path).is_file() for path in hooks)
    assert set(hooks) <= set(_claims(project).get("hooked", [])), _claims(project)


def _fail_second_unlink(monkeypatch: pytest.MonkeyPatch, targets: set[Path]) -> list[Path]:
    unlink = Path.unlink
    attempts: list[Path] = []

    def race_then_fail(self: Path, *args, **kwargs) -> None:
        if self.resolve() in targets:
            attempts.append(self)
            if len(attempts) > 1:
                raise PermissionError(13, "Permission denied", str(self))
            return
        unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", race_then_fail)
    return attempts


def test_a_retained_hook_claim_does_not_keep_its_mcp_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _hook_package(tmp_path / "pkg")
    (tmp_path / "pkg" / "apm.yml").write_text(
        "name: hooked\nversion: 1.0.0\ndependencies:\n  mcp:\n"
        "    - name: hooked-srv\n      registry: false\n      transport: stdio\n"
        "      command: echo\n",
        encoding="ascii",
    )
    project = tmp_path / "app"
    project.mkdir()
    (project / "apm.yml").write_text(
        "name: app\nversion: 1.0.0\ntargets: [copilot]\n"
        "dependencies:\n  apm:\n    - path: ../pkg\n",
        encoding="ascii",
    )
    monkeypatch.chdir(project)
    runner = CliRunner()
    install = runner.invoke(cli, ["install", "--target", "copilot"])
    assert install.exit_code == 0, install.output
    lock = yaml.safe_load((project / "apm.lock.yaml").read_text(encoding="utf-8"))
    assert "hooked-srv" in lock["mcp_servers"]
    hooks = [path for path in _claims(project)["hooked"] if "/hooks/" in path]
    attempts = _fail_second_unlink(monkeypatch, {(project / path).resolve() for path in hooks})

    uninstall = runner.invoke(cli, ["uninstall", "../pkg"])

    monkeypatch.undo()
    assert len(attempts) >= 2, uninstall.output
    assert uninstall.exit_code == 1, uninstall.output
    assert set(hooks) <= set(_claims(project).get("hooked", [])), _claims(project)
    lock = yaml.safe_load((project / "apm.lock.yaml").read_text(encoding="utf-8"))
    assert "hooked-srv" not in (lock.get("mcp_servers") or []), lock
    servers = json.loads((project / ".mcp.json").read_text(encoding="utf-8"))
    assert "hooked-srv" not in servers.get("mcpServers", {}), servers


@pytest.mark.parametrize("row_hashes", [{}, {".github/hooks/h.json": "sha256:" + "b" * 64}])
def test_a_retained_claim_keeps_the_canonical_hash_baseline(
    tmp_path: Path, row_hashes: dict
) -> None:
    from apm_cli.commands.uninstall.lockfile_state import retain_failed_claims
    from apm_cli.deps.lockfile import LockedDependency, LockFile

    path = ".github/hooks/h.json"
    canonical = "sha256:" + "a" * 64
    row = LockedDependency(
        repo_url="owner/pkg", deployed_files=[path], deployed_file_hashes=dict(row_hashes)
    )
    lock = LockFile()

    assert retain_failed_claims(lock, {"owner/pkg": row}, [path], {path: canonical})

    lock.write(tmp_path / "apm.lock.yaml")
    persisted = yaml.safe_load((tmp_path / "apm.lock.yaml").read_text(encoding="utf-8"))
    (row_data,) = persisted["dependencies"]
    assert row_data["deployed_file_hashes"] == {path: canonical}
    assert [record["content_hash"] for record in persisted["deployments"]] == [canonical]


def test_a_retained_claim_whose_parent_was_also_removed_still_persists(tmp_path: Path) -> None:
    from apm_cli.commands.uninstall.lockfile_state import retain_failed_claims
    from apm_cli.deps.lockfile import LockedDependency, LockFile
    from apm_cli.models.dependency.reference import DependencyReference
    from apm_cli.models.dependency.replacement import (
        build_replacement_index,
        parse_replace_block,
        replacement_rule_to_dict,
    )

    rule = parse_replace_block(
        [{"for": {"git": "https://github.com/owner/child"}, "with": {"path": "../child"}}]
    )[0]
    selected = build_replacement_index([rule]).bind(
        DependencyReference.parse("https://github.com/owner/child#v1"),
        consumer_root=tmp_path,
        modules_dir=tmp_path / "apm_modules",
        parent="owner/parent",
        is_dev=False,
    )
    child = LockedDependency.from_dependency_ref(selected, None, 2, "owner/parent")
    hook = ".github/hooks/child.json"
    child.deployed_files = [hook]
    parent = LockedDependency(repo_url="owner/parent")
    plain = LockedDependency(
        repo_url="owner/plain", resolved_by="owner/parent", depth=2, deployed_files=[".x/y.md"]
    )
    lock = LockFile(replacements=[replacement_rule_to_dict(rule)])
    removed = {
        "owner/parent": parent,
        child.get_unique_key(): child,
        "owner/plain": plain,
    }

    assert retain_failed_claims(lock, removed, [hook, ".x/y.md"], {})

    lock.write(tmp_path / "apm.lock.yaml")
    persisted = LockFile.from_yaml((tmp_path / "apm.lock.yaml").read_text(encoding="utf-8"))
    assert set(persisted.dependencies) == {child.get_unique_key(), "owner/plain"}
    assert persisted.dependencies[child.get_unique_key()].parent_keys == (None,)
    assert persisted.dependencies[child.get_unique_key()].deployed_files == [hook]
    assert persisted.dependencies["owner/plain"].resolved_by is None


def test_a_parent_swapped_for_a_symlink_keeps_the_hook_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _hook_package(tmp_path / "pkg")
    project = tmp_path / "app"
    project.mkdir()
    (project / "apm.yml").write_text(
        "name: app\nversion: 1.0.0\ntargets: [copilot]\n"
        "dependencies:\n  apm:\n    - path: ../pkg\n",
        encoding="ascii",
    )
    monkeypatch.chdir(project)
    runner = CliRunner()
    install = runner.invoke(cli, ["install", "--target", "copilot"])
    assert install.exit_code == 0, install.output
    hooks = [path for path in _claims(project)["hooked"] if "/hooks/" in path]
    hook = project / hooks[0]
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / hook.name).write_text("outside bytes", encoding="ascii")
    moved = hook.parent.with_name("hooks-moved")
    unlink = Path.unlink
    target = hook.resolve()

    def swap_parent(self: Path, *args, **kwargs) -> None:
        # Selected cleanup's unlink is undone, and the hook's parent is swapped
        # for a symlink to an outside directory before hook sync runs.
        if self.resolve() == target and not moved.exists():
            hook.parent.rename(moved)
            hook.parent.symlink_to(outside, target_is_directory=True)
            return
        unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", swap_parent)

    uninstall = runner.invoke(cli, ["uninstall", "../pkg"])

    monkeypatch.undo()
    assert moved.is_dir(), uninstall.output
    assert uninstall.exit_code == 1, uninstall.output
    assert ".github/hooks" in uninstall.output
    assert "Error uninstalling packages" not in uninstall.output
    assert (moved / hook.name).is_file()
    assert (outside / hook.name).read_text(encoding="ascii") == "outside bytes"
    assert set(hooks) <= set(_claims(project).get("hooked", [])), _claims(project)
