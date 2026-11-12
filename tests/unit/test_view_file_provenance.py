"""Tests for ``apm view --file PATH`` read-only reverse provenance (#739)."""

from __future__ import annotations

import hashlib
import os
import socket
import sys
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from apm_cli.cli import cli
from apm_cli.utils.content_hash import compute_file_hash

SKILL = ".github/skills/alpha/SKILL.md"
RESOURCE = ".github/skills/alpha/scripts/run.sh"
COMMIT = "0123456789abcdef0123456789abcdef01234567"


def _row(
    value,
    owners,
    active=None,
    content_hash=None,
    target="copilot",
    scope="project",
    kind="project-relative",
):
    return {
        "kind": kind,
        "target": target,
        "value": value,
        "runtime": None,
        "scope": scope,
        "owners": list(owners),
        "active_owner": active or owners[-1],
        "content_hash": content_hash,
    }


def _remote_dep(**extra):
    dep = {
        "repo_url": "acme/skills",
        "host": "github.com",
        "name": "acme-skills",
        "version": "1.2.0",
        "resolved_ref": "v1.2.0",
        "resolved_commit": COMMIT,
    }
    dep.update(extra)
    return dep


def _write(root: Path, rel: str, text: str) -> str:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return compute_file_hash(path)


def _write_lock(lock_dir: Path, data: dict, name: str = "apm.lock.yaml") -> Path:
    lock_dir.mkdir(parents=True, exist_ok=True)
    data = {"lockfile_version": "1", **data}
    path = lock_dir / name
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def _run(*args: str):
    result = CliRunner().invoke(cli, ["view", *args])
    return result, " ".join(result.output.split())


def _snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    state = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            st = path.lstat()
            data = path.read_bytes() if path.is_file() and not path.is_symlink() else b""
            state[str(path)] = (hashlib.sha256(data).digest(), st.st_mtime_ns)
    return state


def _set_home(monkeypatch, home: Path) -> None:
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.chdir(root)
    _set_home(monkeypatch, tmp_path / "home")
    return root


@pytest.fixture
def no_network(monkeypatch):
    def _blocked(*_args, **_kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)


def _standard_project(root: Path) -> None:
    skill_hash = _write(root, SKILL, "---\nname: alpha\n---\nAlpha\n")
    resource_hash = _write(root, RESOURCE, "echo hi\n")
    base_hash = _write(root, ".github/skills/base/SKILL.md", "base\n")
    own_hash = _write(root, ".github/skills/own/SKILL.md", "own\n")
    bundle_hash = _write(root, ".github/skills/bundled/SKILL.md", "bundle\n")
    _write_lock(
        root,
        {
            "dependencies": [
                _remote_dep(deployed_files=[SKILL, RESOURCE]),
                {
                    "repo_url": "acme/base",
                    "host": "github.com",
                    "depth": 2,
                    "resolved_by": "acme/skills",
                    "resolved_commit": COMMIT,
                },
            ],
            "deployments": [
                _row(SKILL, ["acme/skills"], content_hash=skill_hash),
                _row(RESOURCE, ["acme/skills"], content_hash=resource_hash),
                _row(".github/skills/base/SKILL.md", ["acme/base"], content_hash=base_hash),
                _row(".github/skills/own/SKILL.md", ["."], content_hash=own_hash),
                _row(
                    ".github/skills/bundled/SKILL.md",
                    [".", "local-bundle"],
                    content_hash=bundle_hash,
                ),
            ],
        },
    )


class TestRecordedProvenance:
    def test_direct_dependency_skill_reports_source_ref_commit(self, project, no_network):
        _standard_project(project)
        before = _snapshot(project.parent)

        result, out = _run("--file", SKILL)

        assert result.exit_code == 0, result.output
        assert "Owner: acme/skills [active writer]" in out
        assert "Kind: direct dependency" in out
        assert "Package: acme-skills 1.2.0" in out
        assert "Ref: v1.2.0" in out
        assert f"Commit: {COMMIT}" in out
        assert "Current file: matches the recorded deployed hash" in out
        assert "Edit the dependency's source instead" in out
        assert _snapshot(project.parent) == before

    def test_bundled_resource_has_its_own_exact_record(self, project):
        _standard_project(project)
        result, out = _run("--file", RESOURCE)
        assert result.exit_code == 0, result.output
        assert f"{RESOURCE} (project scope" in out
        assert "Owner: acme/skills [active writer]" in out

    def test_transitive_dependency_names_its_parent(self, project):
        _standard_project(project)
        result, out = _run("--file", ".github/skills/base/SKILL.md")
        assert result.exit_code == 0, result.output
        assert "Kind: transitive dependency via acme/skills" in out

    def test_project_self_owner_is_distinct(self, project):
        _standard_project(project)
        result, out = _run("--file", ".github/skills/own/SKILL.md")
        assert result.exit_code == 0, result.output
        assert "Kind: this project (project-self)" in out
        assert "Owned by this project" in out
        assert "Source:" not in out

    def test_local_bundle_active_with_surviving_project_owner(self, project):
        _standard_project(project)
        result, out = _run("--file", ".github/skills/bundled/SKILL.md")
        assert result.exit_code == 0, result.output
        assert "Owner: . [surviving owner]" in out
        assert "Owner: local-bundle [active writer]" in out
        assert "Kind: local bundle (imperative install)" in out
        assert "Deployed from a local bundle" in out

    def test_local_dependency_shows_path_without_ref(self, project):
        skill_hash = _write(project, SKILL, "x\n")
        _write_lock(
            project,
            {
                "dependencies": [
                    {"repo_url": "_local/pkg", "source": "local", "local_path": "./packages/pkg"}
                ],
                "deployments": [_row(SKILL, ["./packages/pkg"], content_hash=skill_hash)],
            },
        )
        result, out = _run("--file", SKILL)
        assert result.exit_code == 0, result.output
        assert "Source: local path ./packages/pkg" in out
        assert "Ref:" not in out

    @pytest.mark.parametrize(
        ("source", "extra", "expected"),
        [
            ({"path": "../checkout"}, {}, "Source: local path ../checkout (replacement)"),
            (
                {"git": "https://github.com/fork/skills.git", "ref": "fix"},
                {"resolved_ref": "fix", "resolved_commit": COMMIT},
                "Source: github.com/fork/skills (replacement)",
            ),
        ],
    )
    def test_replacement_shows_effective_source(self, project, source, extra, expected):
        skill_hash = _write(project, SKILL, "x\n")
        request = {
            "parent": None,
            "ref": None,
            "skills": None,
            "targets": None,
            "dev": False,
            "alias": None,
        }
        _write_lock(
            project,
            {
                "lockfile_version": "3",
                "replacements": [
                    {"for": {"git": "https://github.com/acme/skills.git"}, "with": source}
                ],
                "dependencies": [
                    {
                        "repo_url": "acme/skills",
                        "host": "github.com",
                        "replacement": {"source": source, "requests": [request]},
                        **extra,
                    }
                ],
                "deployments": [_row(SKILL, ["acme/skills"], content_hash=skill_hash)],
            },
        )
        result, out = _run("--file", SKILL)
        assert result.exit_code == 0, result.output
        assert "Owner: acme/skills [active writer]" in out
        assert expected in out
        assert "Source: github.com/acme/skills" not in out

    def test_target_relative_record_resolves_under_target_root(
        self, project, tmp_path, monkeypatch
    ):
        from dataclasses import replace

        from apm_cli.integration.targets import KNOWN_TARGETS

        external = tmp_path / "cursor-root"
        rel = "skills/alpha/SKILL.md"
        skill_hash = _write(external, rel, "x\n")
        monkeypatch.setitem(
            KNOWN_TARGETS,
            "cursor",
            replace(KNOWN_TARGETS["cursor"], resolved_deploy_root=external),
        )
        _write_lock(
            project,
            {
                "dependencies": [_remote_dep()],
                "deployments": [
                    _row(
                        rel,
                        ["acme/skills"],
                        content_hash=skill_hash,
                        target="cursor",
                        kind="target-relative",
                    )
                ],
            },
        )
        result, out = _run("--file", str(external / rel))
        assert result.exit_code == 0, result.output
        assert "Target: cursor" in out
        assert "Owner: acme/skills [active writer]" in out
        assert "matches the recorded deployed hash" in out

    def test_absolute_path_inside_project_is_accepted(self, project):
        _standard_project(project)
        result, out = _run("--file", str(project / SKILL))
        assert result.exit_code == 0, result.output
        assert "Owner: acme/skills" in out


class TestCurrentFileStatus:
    def test_modified_file_keeps_recorded_provenance(self, project):
        _standard_project(project)
        (project / SKILL).write_text("edited\n", encoding="utf-8")
        result, out = _run("--file", SKILL)
        assert result.exit_code == 0, result.output
        assert "Owner: acme/skills [active writer]" in out
        assert "modified since deployment" in out
        assert "recorded provenance, not verification of the current bytes" in out

    def test_missing_file_keeps_recorded_provenance(self, project):
        _standard_project(project)
        (project / SKILL).unlink()
        result, out = _run("--file", SKILL)
        assert result.exit_code == 0, result.output
        assert "Current file: missing" in out
        assert "recorded provenance, not verification" in out

    def test_absent_hash_is_unverifiable(self, project):
        _write(project, SKILL, "x\n")
        _write_lock(
            project,
            {
                "dependencies": [_remote_dep()],
                "deployments": [_row(SKILL, ["acme/skills"], content_hash=None)],
            },
        )
        result, out = _run("--file", SKILL)
        assert result.exit_code == 0, result.output
        assert "unverifiable (no deployed hash was recorded)" in out

    @pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privileges")
    def test_symlinked_component_is_not_followed(self, project, tmp_path):
        external = tmp_path / "external"
        _write(external, "SKILL.md", "outside\n")
        (project / ".github/skills").mkdir(parents=True)
        (project / ".github/skills/alpha").symlink_to(external, target_is_directory=True)
        _write_lock(
            project,
            {
                "dependencies": [_remote_dep()],
                "deployments": [
                    _row(
                        SKILL,
                        ["acme/skills"],
                        content_hash=compute_file_hash(external / "SKILL.md"),
                    )
                ],
            },
        )
        result, out = _run("--file", SKILL)
        assert result.exit_code == 0, result.output
        assert "unverifiable (path crosses a symlink, which is not followed)" in out
        assert "matches the recorded" not in out

    @pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privileges")
    def test_logical_absolute_path_through_symlinked_cwd(self, project, tmp_path, monkeypatch):
        """A shell ``$PWD`` keeps symlinked parents (macOS /tmp) that getcwd resolves."""
        _standard_project(project)
        link = tmp_path / "linked"
        link.symlink_to(project, target_is_directory=True)
        monkeypatch.chdir(link)
        result, out = _run("--file", str(link / SKILL))
        assert result.exit_code == 0, result.output
        assert "Owner: acme/skills [active writer]" in out
        assert "matches the recorded deployed hash" in out

    @pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privileges")
    def test_symlinked_user_deploy_root_is_not_followed(self, tmp_path, monkeypatch):
        real_home = tmp_path / "real-home"
        home = tmp_path / "home"
        rel = ".copilot/skills/alpha/SKILL.md"
        skill_hash = _write(real_home, rel, "user\n")
        _write_lock(
            real_home / ".apm",
            {
                "dependencies": [_remote_dep()],
                "deployments": [_row(rel, ["acme/skills"], content_hash=skill_hash, scope="user")],
            },
        )
        home.symlink_to(real_home, target_is_directory=True)
        _set_home(monkeypatch, home)
        monkeypatch.chdir(tmp_path)
        result, out = _run("--file", str(home / rel), "-g")
        assert result.exit_code == 0, result.output
        assert "Owner: acme/skills [active writer]" in out
        assert "unverifiable (path crosses a symlink, which is not followed)" in out
        assert "matches the recorded" not in out


class TestExactPathOnly:
    @pytest.mark.parametrize(
        "query",
        [
            ".github/skills/alpha",  # directory prefix of a recorded file
            ".github/skills/alpha/SKILL.md.bak",  # superstring
            ".github/skills/alph/SKILL.md",  # near name
            ".claude/skills/alpha/SKILL.md",  # same skill name, other root
            "skills/alpha/SKILL.md",  # suffix substring
            ".github/skills/ALPHA/SKILL.md",  # case variant
        ],
    )
    def test_no_name_prefix_or_substring_guess(self, project, query):
        _standard_project(project)
        result, out = _run("--file", query)
        assert result.exit_code == 1, result.output
        assert "No recorded provenance" in out
        assert "do not assume it is project-owned or safe to edit" in out
        assert "acme/skills" not in out

    def test_canonical_row_from_other_scope_is_ignored(self, project):
        skill_hash = _write(project, SKILL, "x\n")
        _write_lock(
            project,
            {
                "dependencies": [_remote_dep()],
                "deployments": [
                    _row(SKILL, ["acme/skills"], content_hash=skill_hash, scope="user")
                ],
            },
        )
        result, out = _run("--file", SKILL)
        assert result.exit_code == 1, result.output
        assert "No recorded provenance" in out
        assert "acme/skills" not in out

    def test_unrecorded_local_file_is_not_project_owned(self, project):
        _standard_project(project)
        _write(project, ".github/skills/handmade/SKILL.md", "mine\n")
        result, out = _run("--file", ".github/skills/handmade/SKILL.md")
        assert result.exit_code == 1
        assert "Owned by this project" not in out


class TestStateErrors:
    def test_missing_lockfile_is_actionable(self, project):
        before = _snapshot(project.parent)
        result, out = _run("--file", SKILL)
        assert result.exit_code == 2
        assert "No lockfile found" in out
        assert "apm install" in out
        assert _snapshot(project.parent) == before

    def test_malformed_deployments_is_actionable(self, project):
        _write_lock(project, {"deployments": [{"kind": "project-relative", "target": "x"}]})
        result, out = _run("--file", SKILL)
        assert result.exit_code == 2
        assert "Cannot read deployment records" in out
        assert "regenerate the lockfile" in out

    def test_unparseable_lockfile_is_actionable(self, project):
        (project / "apm.lock.yaml").write_text("dependencies: [\n", encoding="utf-8")
        result, out = _run("--file", SKILL)
        assert result.exit_code == 2
        assert "Cannot read deployment records" in out

    def test_owner_without_dependency_record_is_incomplete(self, project):
        skill_hash = _write(project, SKILL, "x\n")
        _write_lock(
            project,
            {"deployments": [_row(SKILL, ["acme/gone"], content_hash=skill_hash)]},
        )
        before = _snapshot(project.parent)
        result, out = _run("--file", SKILL)
        assert result.exit_code == 1
        assert "unresolved (owner has no dependency record" in out
        assert "provenance is incomplete" in out
        assert _snapshot(project.parent) == before

    @pytest.mark.parametrize(
        ("owners", "active"),
        [
            ([".", "acme/gone"], "acme/gone"),  # unresolved active writer
            (["acme/gone", "acme/skills"], "acme/skills"),  # unresolved surviving owner
        ],
    )
    def test_any_unresolved_owner_is_incomplete(self, project, owners, active):
        skill_hash = _write(project, SKILL, "x\n")
        _write_lock(
            project,
            {
                "dependencies": [_remote_dep()],
                "deployments": [_row(SKILL, owners, active=active, content_hash=skill_hash)],
            },
        )
        result, out = _run("--file", SKILL)
        assert result.exit_code == 1, result.output
        assert "Owner: acme/gone" in out
        assert "provenance is incomplete" in out

    def test_resolved_record_does_not_mask_unresolved_one(self, project):
        skill_hash = _write(project, SKILL, "x\n")
        _write_lock(
            project,
            {
                "dependencies": [_remote_dep()],
                "deployments": [
                    _row(SKILL, ["acme/skills"], content_hash=skill_hash),
                    _row(SKILL, ["acme/gone"], content_hash=skill_hash, target="claude"),
                ],
            },
        )
        result, out = _run("--file", SKILL)
        assert result.exit_code == 1, result.output
        assert "Owner: acme/skills [active writer]" in out
        assert "provenance is incomplete" in out

    def test_path_outside_project_root(self, project):
        _standard_project(project)
        result, out = _run("--file", "../elsewhere/SKILL.md")
        assert result.exit_code == 2
        assert "outside the project deploy root" in out
        assert "Use -g" in out

    @pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privileges")
    def test_symlinked_lockfile_is_refused(self, project, tmp_path):
        real = _write_lock(tmp_path / "elsewhere", {"dependencies": []})
        (project / "apm.lock.yaml").symlink_to(real)
        result, out = _run("--file", SKILL)
        assert result.exit_code == 2
        assert "Refusing to read symlinked lockfile" in out


class TestLegacyAndReadOnly:
    def test_legacy_views_without_canonical_rows(self, project):
        skill_hash = _write(project, SKILL, "x\n")
        _write_lock(
            project,
            {
                "dependencies": [
                    _remote_dep(deployed_files=[SKILL], deployed_file_hashes={SKILL: skill_hash})
                ]
            },
        )
        result, out = _run("--file", SKILL)
        assert result.exit_code == 0, result.output
        assert "Owner: acme/skills [active writer]" in out
        assert "legacy deployed_files views" in out

    def test_legacy_lock_filename_is_not_migrated(self, project, no_network):
        skill_hash = _write(project, SKILL, "x\n")
        _write_lock(
            project,
            {
                "dependencies": [
                    _remote_dep(deployed_files=[SKILL], deployed_file_hashes={SKILL: skill_hash})
                ]
            },
            name="apm.lock",
        )
        before = _snapshot(project.parent)
        result, out = _run("--file", SKILL)
        assert result.exit_code == 0, result.output
        assert "from apm.lock)" in out
        assert not (project / "apm.lock.yaml").exists()
        assert _snapshot(project.parent) == before


class TestUserScope:
    def test_global_reads_user_lock_and_home_relative_paths(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        cwd = tmp_path / "somewhere"
        cwd.mkdir()
        _set_home(monkeypatch, home)
        monkeypatch.chdir(cwd)
        rel = ".copilot/skills/alpha/SKILL.md"
        skill_hash = _write(home, rel, "user\n")
        _write_lock(
            home / ".apm",
            {
                "dependencies": [_remote_dep()],
                "deployments": [_row(rel, ["acme/skills"], content_hash=skill_hash, scope="user")],
            },
        )
        before = _snapshot(tmp_path)

        result, out = _run("--file", str(home / rel), "-g")

        assert result.exit_code == 0, result.output
        assert f"{rel} (user scope" in out
        assert "Owner: acme/skills [active writer]" in out
        assert "matches the recorded deployed hash" in out
        assert _snapshot(tmp_path) == before

    def test_global_legacy_rows_keep_inferred_scope_compatibility(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        _set_home(monkeypatch, home)
        monkeypatch.chdir(tmp_path)
        rel = ".github/skills/alpha/SKILL.md"
        skill_hash = _write(home, rel, "user\n")
        _write_lock(
            home / ".apm",
            {
                "dependencies": [
                    _remote_dep(deployed_files=[rel], deployed_file_hashes={rel: skill_hash})
                ]
            },
        )
        result, out = _run("--file", str(home / rel), "-g")
        assert result.exit_code == 0, result.output
        assert "Owner: acme/skills [active writer]" in out
        assert "legacy deployed_files views" in out

    def test_project_scope_does_not_read_user_lock(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        project = tmp_path / "proj"
        project.mkdir()
        _set_home(monkeypatch, home)
        monkeypatch.chdir(project)
        rel = ".copilot/skills/alpha/SKILL.md"
        _write_lock(
            home / ".apm",
            {"dependencies": [_remote_dep()], "deployments": [_row(rel, ["acme/skills"])]},
        )
        result, out = _run("--file", str(home / rel))
        assert result.exit_code == 2
        assert "No lockfile found" in out or "outside the project deploy root" in out


class TestSafeDisplay:
    def test_registry_credentials_are_redacted(self, project):
        skill_hash = _write(project, SKILL, "x\n")
        _write_lock(
            project,
            {
                "dependencies": [
                    {
                        "repo_url": "acme/reg",
                        "source": "registry",
                        "version": "2.0.0",
                        "resolved_url": "https://user:s3cretT0ken@reg.example.com/acme/reg.tgz",
                        "resolved_hash": "sha256:" + "a" * 64,
                    }
                ],
                "deployments": [_row(SKILL, ["acme/reg"], content_hash=skill_hash)],
            },
        )
        result, out = _run("--file", SKILL)
        assert result.exit_code == 0, result.output
        assert "s3cretT0ken" not in out
        assert "Source: registry" in out

    def test_control_characters_from_lockfile_are_not_echoed(self, project):
        skill_hash = _write(project, SKILL, "x\n")
        _write_lock(
            project,
            {
                "dependencies": [
                    _remote_dep(
                        name="evil\x1b[2Jname", version="1.0\x07", resolved_ref="v1\x1b]0;x"
                    )
                ],
                "deployments": [_row(SKILL, ["acme/skills"], content_hash=skill_hash)],
            },
        )
        result, out = _run("--file", SKILL)
        assert result.exit_code == 0, result.output
        assert "\x1b" not in result.output
        assert "\x07" not in result.output
        assert "Package: evil?[2Jname 1.0?" in out


class TestArgumentValidation:
    def test_file_and_package_conflict(self, project):
        result, out = _run("acme/skills", "--file", SKILL)
        assert result.exit_code == 2
        assert "not both" in out

    def test_file_and_registry_conflict(self, project):
        result, out = _run("--file", SKILL, "--registry")
        assert result.exit_code == 2
        assert "--registry cannot be combined with --file" in out

    def test_missing_package_and_file(self, project):
        result, out = _run()
        assert result.exit_code == 2
        assert "Or pass --file PATH" in out

    def test_empty_file_path(self, project):
        _standard_project(project)
        result, out = _run("--file", "")
        assert result.exit_code == 2
        assert "non-empty PATH" in out

    def test_deploy_root_itself(self, project):
        _standard_project(project)
        result, out = _run("--file", ".")
        assert result.exit_code == 2
        assert "deploy root itself" in out

    def test_help_documents_file_mode(self):
        result = CliRunner().invoke(cli, ["view", "--help"])
        assert result.exit_code == 0
        assert "--file PATH" in result.output


class TestNoUpdateCheck:
    @pytest.mark.parametrize("command", ["view", "info"])
    def test_file_mode_skips_update_check(self, project, monkeypatch, command):
        calls = []
        monkeypatch.setattr("apm_cli.cli._check_and_notify_updates", lambda: calls.append(1))
        _standard_project(project)
        result = CliRunner().invoke(cli, [command, "--file", SKILL])
        assert result.exit_code == 0, result.output
        assert calls == []

    def test_package_mode_still_runs_update_check(self, project, monkeypatch):
        calls = []
        monkeypatch.setattr("apm_cli.cli._check_and_notify_updates", lambda: calls.append(1))
        CliRunner().invoke(cli, ["view", "acme/skills"])
        assert calls == [1]
