"""Current-intent and read-only CLI controls bound to the corrective assessment."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests
from click.testing import CliRunner, Result

from apm_cli import config
from apm_cli.cli import cli
from apm_cli.install.audit_target_roots import resolve_audit_targets
from apm_cli.integration.targets import resolve_targets
from apm_cli.utils import console
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.artifact_snapshot import ArtifactSnapshot, assert_unchanged
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_package import LocalPackageFactory

pytestmark = pytest.mark.component

_DEPLOYED_SKILL = ".grok/skills/intent/SKILL.md"


@dataclass(frozen=True)
class _AuditProject:
    project: Path
    home: Path
    source: Path

    def command(self, *arguments: str) -> Result:
        """Invoke the real command stack in process, with no binary prerequisite."""
        return CliRunner().invoke(cli, list(arguments), catch_exceptions=False)

    def audit_result(self, expected_exit: int) -> Result:
        """Keep all live manifest, lockfile, configuration and target bytes intact."""
        project_before = ArtifactSnapshot.capture(self.project)
        home_before = ArtifactSnapshot.capture(self.home)
        result = self.command("audit", "--ci", "--no-policy", "--no-fail-fast", "-f", "json")
        assert result.exit_code == expected_exit, result.output
        assert_unchanged(project_before, ArtifactSnapshot.capture(self.project))
        assert_unchanged(home_before, ArtifactSnapshot.capture(self.home))
        return result

    def audit(self, expected_exit: int) -> dict:
        """Return structured checks from a completed audit."""
        result = self.audit_result(expected_exit)
        return {row["name"]: row for row in json.loads(result.stdout)["checks"]}


@pytest.fixture
def installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_AuditProject]:
    """Materialize source, saved intent and ownership through actual commands."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "audit", base_env=dict(os.environ))
    environment = isolated.subprocess_env()
    for key in os.environ.keys() - environment.keys():
        monkeypatch.delenv(key)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(config, "CONFIG_DIR", str(isolated.config_root))
    monkeypatch.setattr(config, "CONFIG_FILE", str(isolated.config_root / "config.json"))
    monkeypatch.setattr(config, "_config_cache", None)
    monkeypatch.setattr(console, "_console_instance", None)
    monkeypatch.setattr(console, "_console_stderr", False)

    def no_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("Conformance audit must not issue HTTP requests")

    monkeypatch.setattr(requests.Session, "request", no_network)
    factory = LocalPackageFactory(isolated.package_root)
    package = factory.create("source")
    source = factory.add_skill(
        package, "intent", "---\nname: intent\ndescription: Source intent\n---\n# Expected bytes\n"
    )
    project = (
        LocalPackageFactory(isolated.work_root)
        .create("consumer", dependencies=[{"path": str(package.root)}])
        .root
    )
    sentinel = project / ".github/workflows/unrelated.yml"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("name: unrelated\n", encoding="utf-8")
    monkeypatch.chdir(project)
    case = _AuditProject(project, isolated.home, source)
    for arguments in (
        ("experimental", "enable", "grok-cloud"),
        ("config", "set", "target", "grok-cloud"),
        ("install", "--no-policy", "--parallel-downloads", "0"),
    ):
        result = case.command(*arguments)
        assert result.exit_code == 0, result.output
    assert (project / _DEPLOYED_SKILL).read_bytes() == source.read_bytes()
    yield case


@pytest.mark.req("req-lk-023")
@pytest.mark.parametrize("cold", [False, True], ids=["warm", "cold"])
def test_saved_target_drives_read_only_source_replay(installed: _AuditProject, cold: bool) -> None:
    """An unrelated detection signal must not replace the saved current target."""
    if cold:
        shutil.rmtree(installed.project / "apm_modules")
    checks = installed.audit(0)
    assert all(row["passed"] for row in checks.values())
    assert checks["drift"]["passed"] is True
    assert not (installed.project / ".agents/skills/intent").exists()


@pytest.mark.req("req-lk-023")
def test_user_scope_replay_keeps_user_target_bytes(
    installed: _AuditProject, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The user manifest and HOME deployment root remain distinct during replay."""
    result = installed.command(
        "install",
        "--global",
        str(installed.source.parents[2]),
        "--no-policy",
        "--parallel-downloads",
        "0",
    )
    assert result.exit_code == 0, result.output
    assert (installed.home / _DEPLOYED_SKILL).read_bytes() == installed.source.read_bytes()
    user = replace(installed, project=installed.home / ".apm")
    monkeypatch.chdir(user.project)
    assert all(check["passed"] for check in user.audit(0).values())


@pytest.mark.req("req-lk-023")
def test_full_audit_does_not_recreate_absent_configuration(
    installed: _AuditProject, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scanner discovery must share the replay adapter's read-only configuration."""
    manifest_path = installed.project / "apm.yml"
    manifest = load_yaml(manifest_path)
    manifest["targets"] = ["grok-build"]
    dump_yaml(manifest, manifest_path)
    config_path = installed.home / ".apm/config.json"
    config_path.unlink()
    monkeypatch.setattr(config, "_config_cache", None)
    assert all(check["passed"] for check in installed.audit(0).values())
    assert not config_path.exists()


@pytest.mark.req("req-lk-023")
@pytest.mark.parametrize("invalid", ["claudee", [], 42, None])
@pytest.mark.parametrize("manifest_wins", [False, True])
def test_malformed_saved_intent_fails_only_when_selected(
    installed: _AuditProject, invalid: object, manifest_wins: bool
) -> None:
    """Absent and invalid saved targets differ, but manifest precedence remains."""
    config.update_config({"install_target": invalid})
    if manifest_wins:
        path = installed.project / "apm.yml"
        manifest = load_yaml(path)
        manifest["targets"] = ["grok-build"]
        dump_yaml(manifest, path)
        assert all(check["passed"] for check in installed.audit(0).values())
    else:
        checks = installed.audit(1)
        assert checks["target-resolution"]["passed"] is False


@pytest.mark.req("req-lk-023")
def test_native_workflow_replay_fails_before_writer(
    installed: _AuditProject, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An available native DB is not a scratch backend; no native writer may run."""
    from apm_cli.integration.copilot_app_workflow_integrator import CopilotAppWorkflowIntegrator

    runtime = installed.home / "stand-in-app"
    runtime.mkdir()
    database = runtime / "data.db"
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript(
            "CREATE TABLE workflows(id TEXT PRIMARY KEY, prompt TEXT, enabled INTEGER);"
            "INSERT INTO workflows VALUES('sentinel', 'unchanged', 1);"
            "PRAGMA user_version=13;"
        )
    for suffix in ("-wal", "-shm"):
        Path(f"{database}{suffix}").write_bytes(b"sidecar sentinel")
    monkeypatch.setenv("APM_COPILOT_APP_DB", str(database))
    native_writer = Mock(side_effect=AssertionError("Native workflow writer reached by audit"))
    monkeypatch.setattr(CopilotAppWorkflowIntegrator, "integrate", native_writer)
    prompt = installed.source.parents[2] / ".apm/prompts/daily.prompt.md"
    prompt.parent.mkdir(parents=True)
    prompt.write_text("---\nname: daily\ninterval: daily\n---\nDo not deploy during audit.\n")
    assert installed.command("experimental", "enable", "copilot-app").exit_code == 0
    assert installed.command("config", "set", "target", "copilot-app").exit_code == 0
    checks = installed.audit(1)
    native_writer.assert_not_called()
    assert "scratch replay" in checks["drift"]["message"]


@pytest.mark.req("req-lk-023")
@pytest.mark.parametrize(
    "mutation",
    [
        "clean",
        "missing-claim",
        "contracted",
        "legacy-directory",
        "legacy-contracted",
        "forged-content",
        "unavailable-root",
        "unavailable-root-no-config",
        "symlink",
    ],
)
def test_native_user_skill_replay_preserves_layout_and_comparison(
    installed: _AuditProject, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    """Audit a controlled installed snapshot; Cowork install itself is not exercised."""
    native_root = installed.home / "stand-in-cowork-skills"
    native_root.mkdir()
    neighbor = native_root / "unrelated.txt"
    neighbor.write_bytes(b"untouched neighbor")
    monkeypatch.setenv("APM_COPILOT_COWORK_SKILLS_DIR", str(native_root))
    assert installed.command("experimental", "enable", "copilot-cowork").exit_code == 0
    assert installed.command("config", "set", "target", "copilot-cowork").exit_code == 0
    native_source = installed.source.parents[2] / "SKILL.md"
    shutil.copy2(installed.source, native_source)
    installed.source.unlink()
    shutil.copytree(native_source.parent, native_root / "source")
    user = replace(installed, project=installed.home / ".apm")
    shutil.copy2(installed.project / "apm.yml", user.project / "apm.yml")
    shutil.copytree(installed.project / "apm_modules", user.project / "apm_modules", symlinks=True)
    document = load_yaml(installed.project / "apm.lock.yaml")
    for deployment in document["deployments"]:
        deployment.update(
            kind="uri",
            target="copilot-cowork",
            scope="user",
            value=deployment["value"].replace(".grok/skills/intent", "cowork://skills/source"),
        )
    for dependency in document["dependencies"]:
        dependency["deployed_files"] = [
            value.replace(".grok/skills/intent", "cowork://skills/source")
            for value in dependency["deployed_files"]
        ]
        dependency["deployed_file_hashes"] = {
            value.replace(".grok/skills/intent", "cowork://skills/source"): digest
            for value, digest in dependency["deployed_file_hashes"].items()
        }
    dump_yaml(document, user.project / "apm.lock.yaml")
    assert (native_root / "source/SKILL.md").read_bytes() == native_source.read_bytes()
    monkeypatch.chdir(user.project)
    if mutation in {"missing-claim", "legacy-directory", "legacy-contracted", "forged-content"}:
        path = user.project / "apm.lock.yaml"
        document = load_yaml(path)
        if mutation == "forged-content":
            contents = b"Locally forged bytes\n"
            (native_root / "source/SKILL.md").write_bytes(contents)
            digest = "sha256:" + hashlib.sha256(contents).hexdigest()
            for deployment in document["deployments"]:
                if deployment["value"] == "cowork://skills/source/SKILL.md":
                    deployment["content_hash"] = digest
            for dependency in document["dependencies"]:
                dependency["deployed_file_hashes"]["cowork://skills/source/SKILL.md"] = digest
        else:
            document["deployments"] = []
            if mutation != "missing-claim":
                document.pop("deployments")
            for dependency in document["dependencies"]:
                dependency["deployed_files"] = (
                    [] if mutation == "missing-claim" else ["cowork://skills/source"]
                )
                dependency["deployed_file_hashes"] = {}
        dump_yaml(document, path)
    if mutation in {
        "contracted",
        "legacy-contracted",
        "unavailable-root",
        "unavailable-root-no-config",
    }:
        assert installed.command("config", "set", "target", "claude").exit_code == 0
    if mutation.startswith("unavailable-root"):
        native_root.rename(native_root.with_name("unavailable-cowork-skills"))
        monkeypatch.delenv("APM_COPILOT_COWORK_SKILLS_DIR")
        if mutation == "unavailable-root-no-config":
            manifest_path = user.project / "apm.yml"
            manifest = load_yaml(manifest_path)
            manifest["targets"] = ["claude"]
            dump_yaml(manifest, manifest_path)
            Path(config.CONFIG_FILE).unlink()
            config._invalidate_config_cache()
    elif mutation == "symlink":
        moved = native_root / "source"
        external = installed.home / "outside-native-root"
        moved.rename(external)
        moved.symlink_to(external, target_is_directory=True)
    checks = user.audit(0 if mutation in {"clean", "legacy-directory"} else 1)
    if mutation in {"clean", "legacy-directory"}:
        assert all(check["passed"] for check in checks.values())
    elif mutation.startswith("unavailable-root"):
        assert "deployment root is unavailable" in checks["drift"]["message"]
    else:
        kind = {
            "missing-claim": "unrecorded",
            "forged-content": "modified",
            "symlink": "unintegrated",
        }.get(mutation, "orphaned")
        assert any(
            detail.startswith(f"{kind}: ") and detail.endswith("source/SKILL.md")
            for detail in checks["drift"]["details"]
        )
    if not mutation.startswith("unavailable-root"):
        assert neighbor.read_bytes() == b"untouched neighbor"


@pytest.mark.req("req-lk-023")
@pytest.mark.parametrize("intent", ["manifest", "config", "detection"])
def test_current_intent_overrides_old_target_ownership(
    installed: _AuditProject, intent: str
) -> None:
    """Contracted targets stay comparable; ownership cannot authorize replay."""
    expected = ".claude/skills/intent/SKILL.md"
    if intent == "manifest":
        path = installed.project / "apm.yml"
        document = load_yaml(path)
        document["targets"] = ["claude"]
        dump_yaml(document, path)
    elif intent == "config":
        assert installed.command("config", "set", "target", "claude").exit_code == 0
    else:
        assert installed.command("config", "unset", "target").exit_code == 0
        expected = ".agents/skills/intent/SKILL.md"
    checks = installed.audit(1)
    assert f"unintegrated: {expected}" in checks["drift"]["details"]
    if intent != "detection":
        assert f"orphaned: {_DEPLOYED_SKILL}" in checks["drift"]["details"]
    else:
        # Existing .grok detection also selects Grok Build, which shares this root.
        assert (installed.project / _DEPLOYED_SKILL).read_bytes() == installed.source.read_bytes()


@pytest.mark.req("req-lk-023")
@pytest.mark.parametrize("invalid", ["disabled", "manifest"])
def test_invalid_current_selection_cannot_pass_empty_replay(
    installed: _AuditProject, invalid: str
) -> None:
    """A malformed manifest or unavailable selected target is not absent intent."""
    if invalid == "disabled":
        assert installed.command("experimental", "disable", "grok-cloud").exit_code == 0
        checks = installed.audit(1)
        assert checks["target-resolution"]["passed"] is False
    else:
        path = installed.project / "apm.yml"
        document = load_yaml(path)
        document["targets"] = ["grok-cloud"]
        dump_yaml(document, path)
        result = installed.audit_result(2)
        assert "Unknown target 'grok-cloud'" in result.output


@pytest.mark.req("req-lk-023")
@pytest.mark.parametrize("mutation", ["content", "missing-claim"])
def test_configured_target_keeps_integrity_and_membership_checks(
    installed: _AuditProject, mutation: str
) -> None:
    """Re-verify configured-target hashes and detect omitted deployment records."""
    if mutation == "content":
        (installed.project / _DEPLOYED_SKILL).write_bytes(b"Unexpected deployed bytes\n")
    else:
        path = installed.project / "apm.lock.yaml"
        document = load_yaml(path)
        document["deployments"] = []
        for dependency in document["dependencies"]:
            dependency["deployed_files"] = []
            dependency["deployed_file_hashes"] = {}
        dump_yaml(document, path)
    checks = installed.audit(1)
    if mutation == "content":
        assert checks["content-integrity"]["passed"] is False
        kind = "modified"
    else:
        kind = "unrecorded"
    assert f"{kind}: {_DEPLOYED_SKILL}" in checks["drift"]["details"]


@pytest.mark.req("req-pl-016")
def test_configured_target_does_not_authorize_invalid_owners(installed: _AuditProject) -> None:
    """Current target intent cannot bless an owner missing from the dependency set."""
    path = installed.project / "apm.lock.yaml"
    document = load_yaml(path)
    for deployment in document["deployments"]:
        deployment["owners"] = ["removed/owner"]
        deployment["active_owner"] = "removed/owner"
    dump_yaml(document, path)
    checks = installed.audit(1)
    assert checks["deployment-ledger-owners"]["passed"] is False


@pytest.mark.req("req-lk-023")
def test_saved_explicit_only_target_replays_without_detection(
    installed: _AuditProject, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Saved agent-skills intent is explicit selection, not a detection predicate."""
    project = (
        LocalPackageFactory(installed.project.parent)
        .create("explicit-only-consumer", dependencies=[{"path": str(installed.source.parents[2])}])
        .root
    )
    case = replace(installed, project=project)
    monkeypatch.chdir(project)
    manifest = load_yaml(project / "apm.yml")
    assert "target" not in manifest and "targets" not in manifest
    assert case.command("config", "set", "target", "agent-skills").exit_code == 0
    result = case.command("install", "--no-policy", "--parallel-downloads", "0")
    assert result.exit_code == 0, result.output
    assert {target.name for target in resolve_audit_targets(project)} == {"agent-skills"}
    assert "agent-skills" not in {
        target.name for target in resolve_targets(project, create_config=False)
    }
    deployed = project / ".agents/skills/intent/SKILL.md"
    assert deployed.read_bytes() == case.source.read_bytes()
    assert all(check["passed"] for check in case.audit(0).values())


@pytest.mark.req("req-lk-023")
@pytest.mark.parametrize("representation", ["canonical", "legacy", "both"])
def test_historical_native_claims_cannot_pass_filesystem_only_audit(
    installed: _AuditProject, monkeypatch: pytest.MonkeyPatch, representation: str
) -> None:
    """Portable filesystem intent cannot silently discard retained native ownership."""
    from apm_cli.integration.copilot_app_workflow_integrator import CopilotAppWorkflowIntegrator

    manifest_path = installed.project / "apm.yml"
    manifest = load_yaml(manifest_path)
    manifest["targets"] = ["grok-build"]
    dump_yaml(manifest, manifest_path)
    assert all(check["passed"] for check in installed.audit(0).values())

    database = installed.home / "retained-native.db"
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript(
            "CREATE TABLE workflows(id TEXT PRIMARY KEY, prompt TEXT, enabled INTEGER);"
            "INSERT INTO workflows VALUES('retained', 'original host state', 1);"
        )
    for suffix in ("-wal", "-shm"):
        Path(f"{database}{suffix}").write_bytes(b"retained native sidecar")
    monkeypatch.setenv("APM_COPILOT_APP_DB", str(database))
    writer = Mock(side_effect=AssertionError("Historical native writer reached by audit"))
    monkeypatch.setattr(CopilotAppWorkflowIntegrator, "integrate", writer)

    lock_path = installed.project / "apm.lock.yaml"
    document = load_yaml(lock_path)
    owner = document["deployments"][0]["active_owner"]
    uri = "copilot-app-db://workflows/retained-host-workflow"
    if representation in {"canonical", "both"}:
        document["deployments"].append(
            {
                "kind": "uri",
                "target": "copilot-app",
                "value": uri,
                "runtime": None,
                "scope": "project",
                "owners": [owner],
                "active_owner": owner,
                "content_hash": None,
            }
        )
    if representation in {"legacy", "both"}:
        document["dependencies"][0]["deployed_files"].append(uri)
    dump_yaml(document, lock_path)

    checks = installed.audit(1)
    writer.assert_not_called()
    assert all(check["passed"] for name, check in checks.items() if name != "drift"), checks
    assert checks["drift"]["passed"] is False
    assert "no isolated filesystem scratch replay backend" in checks["drift"]["message"]


class _KnownInternalLinkReplayGap(AssertionError):
    """Only the demonstrated false-orphan outcome is an expected failure."""


@pytest.mark.req("req-lk-023")
@pytest.mark.xfail(
    strict=True,
    raises=_KnownInternalLinkReplayGap,
    reason="Inherited replay omits an internal resource link dereferenced by local acquisition",
)
def test_admitted_internal_resource_link_survives_unchanged_audit(
    installed: _AuditProject,
) -> None:
    """Acquired regular-file content should not become obsolete during replay."""
    payload = installed.source.parent / "payload.txt"
    payload.write_bytes(b"Internal resource payload\n")
    (installed.source.parent / "linked.txt").symlink_to("payload.txt")
    result = installed.command("install", "--no-policy", "--parallel-downloads", "0")
    assert result.exit_code == 0, result.output
    relative = ".grok/skills/intent/linked.txt"
    deployed = installed.project / relative
    assert deployed.is_file() and not deployed.is_symlink()
    assert deployed.read_bytes() == payload.read_bytes()

    project_before = ArtifactSnapshot.capture(installed.project)
    home_before = ArtifactSnapshot.capture(installed.home)
    result = installed.command("audit", "--ci", "--no-policy", "--no-fail-fast", "-f", "json")
    assert_unchanged(project_before, ArtifactSnapshot.capture(installed.project))
    assert_unchanged(home_before, ArtifactSnapshot.capture(installed.home))
    checks = {row["name"]: row for row in json.loads(result.stdout)["checks"]}
    assert checks["content-integrity"]["passed"] is True, checks
    assert all(row["passed"] for name, row in checks.items() if name != "drift"), checks
    if (
        result.exit_code == 1
        and checks["drift"]["passed"] is False
        and checks["drift"]["details"] == [f"orphaned: {relative}"]
    ):
        raise _KnownInternalLinkReplayGap(
            "Unchanged CI audit must preserve admitted linked.txt content, not report it orphaned"
        )
    assert result.exit_code == 0, result.output
    assert checks["drift"]["passed"] is True


@pytest.mark.req("req-mf-016")
def test_local_resource_escape_is_rejected_before_audit(installed: _AuditProject) -> None:
    """The replay limitation does not permit acquiring an escaping resource link."""
    outside = installed.source.parents[2].parent / "outside.txt"
    outside.write_bytes(b"Outside selected package\n")
    (installed.source.parent / "escaped.txt").symlink_to(outside)
    result = installed.command("install", "--no-policy", "--parallel-downloads", "0")
    assert result.exit_code != 0, result.output
    assert outside.read_bytes() == b"Outside selected package\n"
    assert not (installed.project / ".grok/skills/intent/escaped.txt").exists()
