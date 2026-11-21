"""Installed-CLI contract: a gated package's deployed hooks retire (#3924).

When the executables gate stops deploying a package's hooks -- ``apm deny``,
or an approval removed while the gate is on -- the next ``apm install`` must
remove the hook entries and copied scripts that package deployed earlier,
from every harness config, while user-authored hooks stay. ``apm audit --ci``
must agree with that state, and the skip message must name a denied package
as denied instead of telling the user to approve it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from apm_cli.deps.lockfile import LockFile
from apm_cli.utils.yaml_io import dump_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
    pytest.mark.promise("P-INV-I3", "P-OWN-frozen-install-mutation-eligibility"),
]

_SCRIPT = "hooky-guard.sh"
_USER_COMMAND = "echo user-owned-hook"


def _hook_package(root: Path) -> None:
    hooks = root / ".apm" / "hooks"
    (hooks / "scripts").mkdir(parents=True)
    dump_yaml({"name": "hooky", "version": "1.0.0"}, root / "apm.yml")
    document = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": f"bash ./scripts/{_SCRIPT}"}],
                }
            ]
        }
    }
    (hooks / "guard.json").write_text(json.dumps(document, indent=2) + "\n", encoding="ascii")
    (hooks / "scripts" / _SCRIPT).write_text("#!/bin/bash\necho guard\n", encoding="ascii")


def _project_files(project: Path) -> list[Path]:
    return [
        path
        for path in project.rglob("*")
        if path.is_file()
        and "apm_modules" not in path.relative_to(project).parts
        and path.name not in {"apm.yml", "apm.lock.yaml"}
    ]


def _hook_residue(project: Path) -> list[str]:
    """Return every deployed file that still names or is the package script."""
    residue = []
    for path in _project_files(project):
        rel = path.relative_to(project).as_posix()
        if path.name == _SCRIPT or _SCRIPT in path.read_text(encoding="utf-8"):
            residue.append(rel)
    return sorted(residue)


def _claude_commands(project: Path) -> list[str]:
    settings = project / ".claude" / "settings.json"
    if not settings.is_file():
        return []
    data = json.loads(settings.read_text(encoding="utf-8"))
    return [
        handler.get("command", "")
        for groups in data.get("hooks", {}).values()
        for group in groups
        for handler in group.get("hooks", [])
    ]


def _exec_status(project: Path) -> str | None:
    lock = LockFile.read(project / "apm.lock.yaml")
    assert lock is not None
    (dependency,) = lock.dependencies.values()
    return dependency.exec_status


@pytest.mark.parametrize(
    ("target", "gate_off"),
    [
        ("claude", "deny"),
        ("claude", "revoke"),
        ("copilot", "deny"),
        ("cursor", "deny"),
        ("codex", "deny"),
        ("gemini", "deny"),
        ("windsurf", "deny"),
    ],
    ids=lambda value: value,
)
def test_gating_a_package_retires_its_deployed_hooks(
    tmp_path: Path, apm_binary_path: Path, target: str, gate_off: str
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    _hook_package(isolated.work_root / "hooky")
    project = isolated.work_root / "consumer"
    project.mkdir()
    manifest = {
        "name": "consumer",
        "version": "0.1.0",
        "targets": [target],
        "dependencies": {"apm": ["../hooky"]},
    }
    if gate_off == "revoke":
        manifest["executables"] = {"allow": {"../hooky": {"hooks": True}}}
    dump_yaml(manifest, project / "apm.yml")
    if target == "claude":
        user_hook = {"hooks": [{"type": "command", "command": _USER_COMMAND}]}
        (project / ".claude").mkdir()
        (project / ".claude" / "settings.json").write_text(
            json.dumps({"hooks": {"PreToolUse": [user_hook]}}, indent=2) + "\n",
            encoding="ascii",
        )
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=180)

    def apm(*args: str, expect: int = 0) -> str:
        result = runner.run(args, cwd=project, env=env)
        output = result.stdout + result.stderr
        assert result.returncode == expect, f"apm {' '.join(args)}\n{output}"
        return output

    apm("install", "--no-policy")
    assert _hook_residue(project), "the hook must deploy before it is gated"
    deployed_configs = {
        path: path.read_bytes() for path in _project_files(project) if path.suffix == ".json"
    }

    if gate_off == "deny":
        denied = " ".join(apm("deny", "../hooky").split())
        assert "Run 'apm install' to remove executables already deployed." in denied, denied
    else:
        dump_yaml({**manifest, "executables": {}}, project / "apm.yml")

    gated = " ".join(apm("install", "--no-policy").split())
    assert "hooks skipped" in gated, gated
    if gate_off == "deny":
        assert "hooks skipped (denied)" in gated, gated
        assert "apm approve" not in gated, gated
        assert _exec_status(project) == "denied"
    else:
        assert "not approved in executables.allow" in gated, gated
        assert _exec_status(project) == "gated_pending_approval"

    assert _hook_residue(project) == []
    if target == "claude":
        assert _claude_commands(project) == [_USER_COMMAND]

    audit = apm("audit", "--ci", "--no-policy")
    assert "Drift detected" not in audit, audit

    if target != "copilot":
        # A denied hook still registered in a shared config (e.g. by an older
        # CLI) fails the audit.
        for path, content in deployed_configs.items():
            path.write_bytes(content)
        leftover = apm("audit", "--ci", "--no-policy", expect=1)
        assert "Drift detected" in leftover, leftover


@pytest.mark.parametrize("target", ["claude", "copilot", "cursor", "codex", "gemini", "kiro"])
def test_audit_and_update_honor_the_current_executables_decision(
    tmp_path: Path, apm_binary_path: Path, target: str
) -> None:
    """A gated package audits green; ``apm update`` with no ref changes deploys
    an approval and retires a deny, which audits red until then (#3958)."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    _hook_package(isolated.work_root / "hooky")
    project = isolated.work_root / "consumer"
    project.mkdir()
    manifest = {
        "name": "consumer",
        "version": "0.1.0",
        "targets": [target],
        "dependencies": {"apm": ["../hooky"]},
        "executables": {},
    }
    dump_yaml(manifest, project / "apm.yml")
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=180)

    def apm(*args: str, expect: int = 0) -> str:
        result = runner.run(args, cwd=project, env=env)
        output = result.stdout + result.stderr
        assert result.returncode == expect, f"apm {' '.join(args)}\n{output}"
        return output

    apm("install", "--no-policy")
    assert _exec_status(project) == "gated_pending_approval"
    assert _hook_residue(project) == []
    apm("audit", "--ci", "--no-policy")

    apm("approve", "../hooky")
    approved = " ".join(apm("update", "--yes").split())
    assert "Reconciled executables without changing refs." in approved, approved
    assert _exec_status(project) == "deployed"
    assert _hook_residue(project)

    apm("deny", "../hooky")
    lingering = apm("audit", "--ci", "--no-policy", expect=1)
    assert "Drift detected" in lingering, lingering

    lock_bytes, residue = (project / "apm.lock.yaml").read_bytes(), _hook_residue(project)
    preview = " ".join(apm("update", "--dry-run").split())
    assert "Dry run: no changes applied" in preview, preview
    assert (project / "apm.lock.yaml").read_bytes() == lock_bytes
    assert _hook_residue(project) == residue

    updated = " ".join(apm("update", "--yes").split())
    assert "already at their latest" not in updated, updated
    assert "hooks skipped (denied)" in updated, updated
    assert _exec_status(project) == "denied"
    assert _hook_residue(project) == []
    apm("audit", "--ci", "--no-policy")
    assert "already at their latest" in apm("update", "--yes")


@pytest.mark.parametrize("target", ["copilot", "claude", "cursor"])
def test_deny_names_each_retired_hook_file_and_never_reports_it_retained(
    tmp_path: Path, apm_binary_path: Path, target: str
) -> None:
    """After ``apm deny``, install names every hook file it deletes (#4173)."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    env = isolated.subprocess_env(overrides={"COLUMNS": "200"})
    _hook_package(isolated.work_root / "hooky")
    project = isolated.work_root / "consumer"
    project.mkdir()
    manifest = {
        "name": "consumer",
        "version": "0.1.0",
        "targets": [target],
        "dependencies": {"apm": ["../hooky"]},
    }
    dump_yaml(manifest, project / "apm.yml")
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=180)

    def apm(*args: str) -> str:
        result = runner.run(args, cwd=project, env=env)
        output = result.stdout + result.stderr
        assert result.returncode == 0, f"apm {' '.join(args)}\n{output}"
        assert output.isascii(), output
        status = [line for line in result.stdout.splitlines() if line.startswith("[")]
        assert len(status) == len(set(status)), output
        return output

    apm("install")
    apm("deny", "../hooky")
    before = {path.relative_to(project).as_posix() for path in _project_files(project)}

    output = apm("install", "--verbose")

    after = {path.relative_to(project).as_posix() for path in _project_files(project)}
    # apm-hooks.json is APM's ownership sidecar for merged configs, not a hook file.
    removed = sorted(path for path in before - after if not path.endswith("/apm-hooks.json"))
    assert removed, "the denied package's hook files must be deleted"
    assert _hook_residue(project) == []
    listed = [line[len("  - ") :] for line in output.splitlines() if line.startswith("  - ")]
    for path in removed:
        assert any(path == item or path.startswith(item) for item in listed), (
            f"{path} was deleted but not named\n{output}"
        )
    assert "Historical dependency claims retained" not in output, output
    assert _exec_status(project) == "denied"


@pytest.mark.parametrize("gate_off", ["deny", "revoke"])
def test_frozen_fails_when_a_gated_hook_retires_but_the_lock_still_claims_it(
    tmp_path: Path, apm_binary_path: Path, gate_off: str
) -> None:
    """Frozen refuses a gated hook before any write, as audit --ci does (#4043, RA-024)."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    _hook_package(isolated.work_root / "hooky")
    project = isolated.work_root / "consumer"
    project.mkdir()
    manifest = {
        "name": "consumer",
        "version": "0.1.0",
        "targets": ["copilot"],
        "dependencies": {"apm": ["../hooky"]},
    }
    if gate_off == "revoke":
        manifest["executables"] = {"allow": {"../hooky": {"hooks": True}}}
    dump_yaml(manifest, project / "apm.yml")
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=180)
    lock_path = project / "apm.lock.yaml"

    def apm(*args: str, expect: int = 0) -> str:
        result = runner.run(args, cwd=project, env=env)
        output = result.stdout + result.stderr
        assert result.returncode == expect, f"apm {' '.join(args)}\n{output}"
        return output

    def claimed() -> set[str]:
        lock = LockFile.read(lock_path)
        assert lock is not None
        (dependency,) = lock.dependencies.values()
        return set(dependency.deployed_files)

    apm("install", "--no-policy")
    hook_paths = set(_hook_residue(project))
    assert hook_paths, "the hook must deploy before it is gated"
    assert hook_paths <= claimed()

    if gate_off == "deny":
        apm("deny", "../hooky")
        source = "../hooky is denied locally"
    else:
        dump_yaml({**manifest, "executables": {}}, project / "apm.yml")
        source = "../hooky is no longer approved locally"
    lock_bytes = lock_path.read_bytes()

    # Read-only modes report the retirement without applying it.
    preview = " ".join(apm("install", "--dry-run", "--no-policy").split())
    assert "Executables that would be retired" in preview, preview
    assert f"- {source}" in preview, preview
    assert set(_hook_residue(project)) == hook_paths
    apm("audit", "--ci", "--no-policy", expect=1)

    # RA-024: frozen refuses the stale decision before any write, naming the
    # package and 'apm install'; a second run fails the same way.
    for _attempt in range(2):
        frozen = " ".join(apm("install", "--frozen", "--no-policy", expect=1).split())
        assert "[x] --frozen: apm.lock.yaml records an executables decision" in frozen, frozen
        assert "../hooky: recorded " in frozen, frozen
        assert "Run 'apm install'" in frozen, frozen
        assert "commit apm.lock.yaml" in frozen, frozen
        assert set(_hook_residue(project)) == hook_paths
        assert lock_path.read_bytes() == lock_bytes
        apm("audit", "--ci", "--no-policy", expect=1)

    apm("install", "--no-policy")
    assert not hook_paths & claimed()
    assert _exec_status(project) == ("denied" if gate_off == "deny" else "gated_pending_approval")
    apm("install", "--frozen", "--no-policy")
    apm("audit", "--ci", "--no-policy")


_LOCAL_EDIT_ADVICE = ("local edit", "export-patch", "Delete the edited files")
_RESYNC = "Run 'apm install' to re-sync deployed files with the lockfile."


def _approved_hook_consumer(
    tmp_path: Path, apm_binary_path: Path, target: str
) -> tuple[Path, object]:
    """A consumer whose approved hook package is deployed; returns (project, apm)."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    _hook_package(isolated.work_root / "hooky")
    project = isolated.work_root / "consumer"
    project.mkdir()
    manifest = {
        "name": "consumer",
        "version": "0.1.0",
        "targets": [target],
        "dependencies": {"apm": ["../hooky"]},
        "executables": {},
    }
    dump_yaml(manifest, project / "apm.yml")
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=180)

    def apm(*args: str, expect: int = 0) -> str:
        result = runner.run(args, cwd=project, env=env)
        output = " ".join((result.stdout + result.stderr).split())
        assert result.returncode == expect, f"apm {' '.join(args)}\n{output}"
        return output

    apm("install", "--no-policy")
    apm("approve", "../hooky")
    apm("update", "--yes")
    assert _exec_status(project) == "deployed"
    assert _hook_residue(project)
    return project, apm


@pytest.mark.parametrize("target", ["claude", "copilot", "cursor", "codex", "gemini", "kiro"])
def test_deny_residue_is_not_a_local_edit_and_its_remedy_converges(
    tmp_path: Path, apm_binary_path: Path, target: str
) -> None:
    """What a deny retires audits red without local-edit advice; 'apm install' fixes it."""
    project, apm = _approved_hook_consumer(tmp_path, apm_binary_path, target)

    apm("deny", "../hooky")
    lingering = apm("audit", "--ci", "--no-policy", expect=1)

    assert "Drift detected" in lingering, lingering
    for advice in _LOCAL_EDIT_ADVICE:
        assert advice not in lingering, lingering
    assert _RESYNC in lingering, lingering

    apm("install", "--no-policy")
    assert _hook_residue(project) == []
    converged = apm("audit", "--ci", "--no-policy")
    assert "Drift detected" not in converged, converged

    def snapshot() -> dict[str, bytes]:
        files = [*_project_files(project), project / "apm.yml", project / "apm.lock.yaml"]
        return {path.relative_to(project).as_posix(): path.read_bytes() for path in files}

    settled = snapshot()
    apm("install", "--no-policy")
    assert snapshot() == settled


def test_user_edit_to_an_apm_hook_entry_stays_a_local_edit(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    """With no gate change, a user-edited APM hook entry is still reported as a local edit."""
    project, apm = _approved_hook_consumer(tmp_path, apm_binary_path, "claude")
    settings = project / ".claude" / "settings.json"
    edited = settings.read_text(encoding="utf-8").replace(_SCRIPT, "user-guard.sh")
    assert edited != settings.read_text(encoding="utf-8")
    settings.write_text(edited, encoding="utf-8")

    audit = apm("audit", "--ci", "--no-policy", expect=1)

    assert "Drift detected" in audit, audit
    assert "1 local edit(s) to managed files" in audit, audit
    assert settings.read_text(encoding="utf-8") == edited
