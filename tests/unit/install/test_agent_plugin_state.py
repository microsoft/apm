"""Focused lifecycle-state tests for installed Agent Plugins."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
from apm_cli.core.deployment_state import (
    DeploymentLedger,
    DeploymentLocator,
    DeploymentRecord,
    LocatorKind,
)
from apm_cli.deps.lockfile import (
    InstalledPluginComponentFact,
    InstalledPluginRecord,
    InstalledPluginRecordCodec,
    LockedDependency,
    LockFile,
    LockfileFormatError,
)
from apm_cli.install.agent_plugin_state import (
    PreparedAgentPluginRoot,
    _managed_path,
    prepare_agent_plugin_root,
    prepare_installed_plugin_state,
    project_installed_plugin_record,
    remove_installed_plugin_root,
    resolve_installed_plugin_record_roots,
)
from apm_cli.models.validation import validate_apm_package

GIT_SHA = "a" * 40
SOURCE_DIGEST = f"sha256:{'b' * 64}"


def _write_plugin(
    root: Path,
    *,
    name: str = "stable-plugin",
    version: str = "1.2.3",
    with_components: bool = False,
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, object] = {
        "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
        "name": name,
        "version": version,
        "description": "Lifecycle state test",
    }
    if with_components:
        manifest["extensions"] = {
            "com.microsoft.apm": {
                "schemaVersion": "1",
                "feature": {"enabled": True},
            }
        }
    (root / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    if with_components:
        skill_root = root / "skills" / "deploy"
        skill_root.mkdir(parents=True)
        (skill_root / "SKILL.md").write_text(
            "---\nname: deploy\ndescription: deploy skill\n---\n\nUse it.\n",
            encoding="utf-8",
        )
        (root / "mcp.json").write_text(
            json.dumps(
                {
                    "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
                    "mcpServers": {
                        "local": {
                            "type": "stdio",
                            "command": "tool",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )


def _record(identity: str, *, generation: int = 1) -> InstalledPluginRecord:
    plugin_root, data_root = InstalledPluginRecordCodec.root_values(identity, "project")
    return InstalledPluginRecord(
        record_version=1,
        ownership_generation=generation,
        identity=identity,
        version="1.0.0",
        source_kind="git",
        source_locator="https://example.invalid/acme/plugin.git",
        resolved_ref=GIT_SHA,
        source_digest=None,
        plugin_root=plugin_root,
        data_root=data_root,
        scope="project",
        components=(
            InstalledPluginComponentFact(
                kind="mcp",
                name="demo",
                metadata=(("transport", "stdio"),),
            ),
        ),
    )


def _locator(value: str) -> DeploymentLocator:
    return DeploymentLocator(
        kind=LocatorKind.PROJECT_RELATIVE,
        target="copilot",
        value=value,
        runtime=None,
        scope="project",
    )


def _owned_record(owner: str, value: str, digest: str) -> DeploymentRecord:
    return DeploymentRecord(
        locator=_locator(value),
        owners=(owner,),
        active_owner=owner,
        content_hash=digest,
    )


def test_projection_is_stable_across_archive_and_cache_renames(tmp_path: Path) -> None:
    first_source = tmp_path / "renamed-release.zip.unpacked"
    second_source = tmp_path / "cache" / "different-object-name"
    _write_plugin(first_source)
    _write_plugin(second_source)
    first_validation = validate_apm_package(first_source)
    second_validation = validate_apm_package(second_source)

    first = project_installed_plugin_record(
        first_validation,
        tmp_path / "project",
        global_=False,
        source_kind="archive",
        source_locator="https://example.invalid/releases/renamed.zip",
        source_digest=SOURCE_DIGEST,
    )
    second = project_installed_plugin_record(
        second_validation,
        tmp_path / "project",
        global_=False,
        source_kind="cache",
        source_locator="/cache/different-object-name",
        resolved_ref="cache-key",
    )

    assert first.identity == second.identity == "stable-plugin"
    assert first.version == second.version == "1.2.3"
    assert first.plugin_root == second.plugin_root
    assert first.data_root == second.data_root
    assert first.components == second.components

    projections = (
        ("local", str(first_source), None, None),
        ("cache", "/cache/object", "cache-key", None),
        ("git", "https://example.invalid/acme/plugin.git", GIT_SHA, None),
        ("archive", "https://example.invalid/plugin.zip", None, SOURCE_DIGEST),
        ("registry", "registry.example.invalid/acme/plugin", None, SOURCE_DIGEST),
    )
    for source_kind, locator, resolved_ref, source_digest in projections:
        projected = project_installed_plugin_record(
            first_validation,
            tmp_path / "project",
            global_=False,
            source_kind=source_kind,
            source_locator=locator,
            resolved_ref=resolved_ref,
            source_digest=source_digest,
        )
        assert projected.identity == first.identity
        assert projected.version == first.version
        assert projected.plugin_root == first.plugin_root
        assert projected.data_root == first.data_root
        assert projected.components == first.components


def test_distinct_identities_have_distinct_roots_and_casefold_aliases_fail() -> None:
    alpha = _record("alpha")
    beta = _record("beta")
    assert alpha.plugin_root != beta.plugin_root
    assert alpha.data_root != beta.data_root

    folded = _record("ALPHA")
    rows = InstalledPluginRecordCodec.rows({"alpha": alpha})
    rows.extend(InstalledPluginRecordCodec.rows({"ALPHA": folded}))
    with pytest.raises(ValueError, match="case-fold ambiguous"):
        InstalledPluginRecordCodec.from_rows(rows)


def test_lockfile_round_trip_is_strict_and_preserves_legacy_entries() -> None:
    dependency = LockedDependency(
        repo_url="owner/legacy",
        version="4.5.6",
        deployed_files=[".github/skills/legacy/SKILL.md"],
    )
    lockfile = LockFile()
    lockfile.add_dependency(dependency)
    record = _record("stable-plugin")
    lockfile.installed_plugins[record.identity] = record

    restored = LockFile.from_yaml(lockfile.to_yaml())

    assert restored.installed_plugins == {record.identity: record}
    assert restored.get_dependency("owner/legacy") is not None
    assert restored.get_dependency("owner/legacy").to_dict() == dependency.to_dict()
    assert restored.to_yaml() == lockfile.to_yaml()
    legacy = LockFile.from_yaml(
        "lockfile_version: '1'\ndependencies:\n  - repo_url: owner/legacy\n"
    )
    assert legacy.installed_plugins == {}
    assert legacy.get_dependency("owner/legacy") is not None

    with pytest.raises(LockfileFormatError, match="installed_plugins must be a list"):
        LockFile.from_yaml("lockfile_version: '1'\ndependencies: []\ninstalled_plugins:\n")


def test_replace_only_ledger_transition_removes_stale_plugin_facts() -> None:
    record = _record("stable-plugin")
    owner = record.owner_key
    stale = _owned_record(owner, ".github/agents/stale.agent.md", "sha256:stale")
    kept = _owned_record(owner, ".github/agents/kept.agent.md", "sha256:old")
    unrelated = _owned_record("owner/dependency", ".github/skills/user/SKILL.md", "sha256:user")
    shared_locator = _locator(".github/agents/shared.agent.md")
    shared = DeploymentRecord(
        locator=shared_locator,
        owners=(owner, "owner/dependency"),
        active_owner="owner/dependency",
        content_hash="sha256:dependency",
    )
    prior_records = {
        stale.locator.key: stale,
        kept.locator.key: kept,
        unrelated.locator.key: unrelated,
        shared.locator.key: shared,
    }
    prior = DeploymentLedger(records=prior_records)
    desired = _owned_record(owner, ".github/agents/kept.agent.md", "sha256:new")

    transition = DeploymentLedgerCodec.prepare_owner_replacement(prior, owner, (desired,))
    prior_records.clear()

    assert stale.locator.key not in transition.replacement.records
    assert stale.locator.key in transition.prior.records
    assert transition.replacement.records[kept.locator.key].content_hash == "sha256:new"
    assert transition.replacement.records[unrelated.locator.key] == unrelated
    assert transition.replacement.records[shared.locator.key].owners == ("owner/dependency",)


def test_state_rollback_restores_prior_record_root_and_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    old_source = tmp_path / "old-source"
    new_source = tmp_path / "renamed-new-source"
    _write_plugin(old_source, version="1.0.0")
    _write_plugin(new_source, version="2.0.0")
    old_validation = validate_apm_package(old_source)
    new_validation = validate_apm_package(new_source)
    old_record = project_installed_plugin_record(
        old_validation,
        project,
        global_=False,
        source_kind="git",
        source_locator="https://example.invalid/acme/plugin.git",
        resolved_ref=GIT_SHA,
    )
    active_root = resolve_installed_plugin_record_roots(old_record, project).plugin_root
    active_root.mkdir(parents=True)
    (active_root / "payload.txt").write_text("old", encoding="utf-8")
    owner = old_record.owner_key
    old_deployment = _owned_record(owner, ".github/agents/old.agent.md", "sha256:old")
    lockfile = LockFile(
        installed_plugins={old_record.identity: old_record},
        deployment_ledger=DeploymentLedger(records={old_deployment.locator.key: old_deployment}),
    )
    lockfile._deployments_present = True
    new_deployment = _owned_record(owner, ".github/agents/new.agent.md", "sha256:new")
    prepared = prepare_installed_plugin_state(
        new_validation,
        new_source,
        project,
        lockfile,
        global_=False,
        source_kind="archive",
        source_locator="https://example.invalid/releases/plugin-v2.zip",
        source_digest=SOURCE_DIGEST,
        owned_records=(new_deployment,),
    )

    prepared.commit()
    assert lockfile.installed_plugins["stable-plugin"].version == "2.0.0"
    assert lockfile.installed_plugins["stable-plugin"].ownership_generation == 2
    assert new_deployment.locator.key in lockfile.deployment_ledger.records
    assert old_deployment.locator.key not in lockfile.deployment_ledger.records
    assert (active_root / "plugin.json").read_text(encoding="utf-8").find("2.0.0") >= 0

    original_rollback = PreparedAgentPluginRoot.rollback

    def fail_rollback(self: PreparedAgentPluginRoot) -> None:
        raise OSError("locked")

    monkeypatch.setattr(PreparedAgentPluginRoot, "rollback", fail_rollback)
    with pytest.raises(OSError, match="locked"):
        prepared.rollback()
    assert lockfile.installed_plugins["stable-plugin"].version == "2.0.0"
    assert lockfile.deployment_ledger == prepared.ledger.replacement
    assert "2.0.0" in (active_root / "plugin.json").read_text(encoding="utf-8")

    monkeypatch.setattr(PreparedAgentPluginRoot, "rollback", original_rollback)
    prepared.rollback()
    assert lockfile.installed_plugins == {"stable-plugin": old_record}
    assert lockfile.deployment_ledger == prepared.ledger.prior
    assert (active_root / "payload.txt").read_text(encoding="utf-8") == "old"


def test_projection_rejects_discarded_canonical_ir(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_plugin(source)
    validation = validate_apm_package(source)
    assert validation.package is not None
    validation.package.agent_plugin = None

    with pytest.raises(ValueError, match="discarded its canonical Agent Plugin IR"):
        project_installed_plugin_record(
            validation,
            tmp_path / "project",
            global_=False,
            source_kind="local",
            source_locator=str(source),
        )


def test_projection_retains_component_facts_after_source_changes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_plugin(source, with_components=True)
    validation = validate_apm_package(source)
    assert validation.is_valid
    (source / "mcp.json").unlink()
    (source / "skills" / "deploy" / "SKILL.md").unlink()

    record = project_installed_plugin_record(
        validation,
        tmp_path / "project",
        global_=False,
        source_kind="local",
        source_locator=str(source),
    )

    assert tuple((fact.kind, fact.name) for fact in record.components) == (
        ("apm-extension", "com.microsoft.apm"),
        ("mcp", "local"),
        ("skill", "deploy"),
    )
    assert record.components[1].metadata == (("transport", "stdio"),)
    assert record.components[2].relative_path == "skills/deploy"


def test_prepare_rejects_casefold_collision_before_staging(tmp_path: Path) -> None:
    source = tmp_path / "source"
    project = tmp_path / "project"
    _write_plugin(source, name="stable-plugin")
    validation = validate_apm_package(source)
    conflicting = _record("Stable-Plugin")
    lockfile = LockFile(installed_plugins={conflicting.identity: conflicting})

    with pytest.raises(ValueError, match="case-fold ambiguous"):
        prepare_installed_plugin_state(
            validation,
            source,
            project,
            lockfile,
            global_=False,
            source_kind="local",
            source_locator=str(source),
        )

    assert lockfile.installed_plugins == {"Stable-Plugin": conflicting}
    assert not (project / "apm_modules").exists()


def test_commit_conflict_restores_root_and_record_but_preserves_newer_ledger(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    old_source = tmp_path / "old"
    new_source = tmp_path / "new"
    _write_plugin(old_source, version="1.0.0")
    _write_plugin(new_source, version="2.0.0")
    old_validation = validate_apm_package(old_source)
    new_validation = validate_apm_package(new_source)
    old_record = project_installed_plugin_record(
        old_validation,
        project,
        global_=False,
        source_kind="git",
        source_locator="https://example.invalid/acme/plugin.git",
        resolved_ref=GIT_SHA,
    )
    active_root = resolve_installed_plugin_record_roots(old_record, project).plugin_root
    active_root.mkdir(parents=True)
    (active_root / "payload.txt").write_text("old", encoding="utf-8")
    lockfile = LockFile(installed_plugins={old_record.identity: old_record})
    prepared = prepare_installed_plugin_state(
        new_validation,
        new_source,
        project,
        lockfile,
        global_=False,
        source_kind="archive",
        source_locator="https://example.invalid/plugin.zip",
        source_digest=SOURCE_DIGEST,
    )
    newer = _owned_record("owner/dependency", ".github/skills/new/SKILL.md", "sha256:newer")
    lockfile.deployment_ledger = DeploymentLedger(records={newer.locator.key: newer})
    lockfile._deployments_present = True

    with pytest.raises(RuntimeError, match="changed after owner replacement preparation"):
        prepared.commit()

    assert lockfile.installed_plugins == {old_record.identity: old_record}
    assert lockfile.deployment_ledger.records == {newer.locator.key: newer}
    assert (active_root / "payload.txt").read_text(encoding="utf-8") == "old"


def test_failed_backup_rename_never_deletes_active_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    source = tmp_path / "source"
    _write_plugin(source)
    validation = validate_apm_package(source)
    assert validation.agent_plugin is not None
    roots = prepare_agent_plugin_root(
        source,
        project,
        global_=False,
        identity=validation.agent_plugin.identity,
    )
    roots.plugin_root.mkdir(parents=True)
    (roots.plugin_root / "payload.txt").write_text("old", encoding="utf-8")
    roots.had_existing_root = True
    real_replace = os.replace

    def fail_first_replace(source_path: Path, destination_path: Path) -> None:
        if Path(source_path) == roots.plugin_root:
            raise OSError("rename failed")
        real_replace(source_path, destination_path)

    monkeypatch.setattr("apm_cli.install.agent_plugin_state.os.replace", fail_first_replace)
    with pytest.raises(OSError, match="rename failed"):
        roots.commit()

    assert (roots.plugin_root / "payload.txt").read_text(encoding="utf-8") == "old"
    roots.rollback()


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda row: row["components"][0].update(relative_path="C:\\Windows\\System32"),
            "component paths must be relative",
        ),
        (
            lambda row: row["source"].update(locator="https://token@example.invalid/plugin.git"),
            "credential-free",
        ),
        (
            lambda row: row["source"].update(locator="//token@example.invalid/plugin.git"),
            "credential-free",
        ),
        (
            lambda row: row["source"].update(locator="https:token@example.invalid/plugin.git"),
            "credential-free URL",
        ),
        (
            lambda row: row["source"].update(locator="git+https:token@example.invalid/plugin.git"),
            "credential-free URL",
        ),
        (
            lambda row: row["source"].update(
                locator="x-access-token:ghs_TOKEN@github.com:org/repo.git"
            ),
            "credential-free URL",
        ),
        (
            lambda row: row["source"].update(locator="oauth2:TOKEN@host:path"),
            "credential-free URL",
        ),
        (
            lambda row: row["source"].update(locator="user:password@host:path"),
            "credential-free URL",
        ),
        (
            lambda row: row["source"].update(locator="Git@github.com:org/repo.git"),
            "canonical git@host:path",
        ),
        (
            lambda row: row["source"].update(
                locator="https://example.invalid/plugin.git?access_token=secret"
            ),
            "credential-free",
        ),
        (
            lambda row: row["source"].update(
                locator="https://example.invalid/plugin.git#access-token"
            ),
            "credential-free",
        ),
        (
            lambda row: row["source"].update(
                locator="https://token%40value@example.invalid/plugin.git"
            ),
            "credential-free",
        ),
        (
            lambda row: row["source"].update(locator="ssh://g%69t@github.com/org/repo.git"),
            "credential-free",
        ),
        (
            lambda row: row["source"].update(
                locator="https://X-ACCESS-TOKEN:SECRET@github.com/org/repo.git"
            ),
            "credential-free",
        ),
        (
            lambda row: row["source"].update(
                locator="https://user%3Avalue%40github.com/org/repo.git"
            ),
            "credential-free",
        ),
        (
            lambda row: row["source"].update(locator="ssh://git%40github.com/org/repo.git"),
            "credential-free",
        ),
        (
            lambda row: row["source"].update(locator="https://github.com\\evil/org/repo.git"),
            "credential-free",
        ),
        (
            lambda row: row["source"].update(locator="https://github.com:notaport/org/repo.git"),
            "invalid remote URL",
        ),
        (
            lambda row: row["source"].update(locator="https://github.com /org/repo.git"),
            "credential-free",
        ),
        (
            lambda row: row["source"].update(locator="https://host..example/org/repo.git"),
            "invalid URL host",
        ),
        (
            lambda row: row["source"].update(locator="https://host-.example/org/repo.git"),
            "invalid URL host",
        ),
        (
            lambda row: row["source"].update(locator="git@[:]:org/repo.git"),
            "invalid host",
        ),
        (
            lambda row: row["source"].update(resolved_ref="main"),
            "full commit SHA",
        ),
        (
            lambda row: row["source"].update(kind="archive", digest="sha256:short"),
            "canonical sha256",
        ),
        (
            lambda row: row["source"].update(kind="archive", digest="b" * 64),
            "canonical sha256",
        ),
        (
            lambda row: row.update(plugin_root="../.agent-plugins/plugin-invalid"),
            "canonical logical layout",
        ),
        (
            lambda row: row.pop("ownership_generation"),
            "invalid fields",
        ),
    ],
)
def test_malformed_plugin_rows_fail_closed(mutate, message: str) -> None:
    record = _record("stable-plugin")
    rows = InstalledPluginRecordCodec.rows({record.identity: record})
    mutate(rows[0])

    with pytest.raises(ValueError, match=message):
        InstalledPluginRecordCodec.from_rows(rows)


@pytest.mark.parametrize(
    ("source_kind", "source_locator", "resolved_ref"),
    [
        ("git", "git@example.invalid:acme/plugin.git", GIT_SHA),
        ("git", "git@github.com:acme/plugin.git", GIT_SHA),
        ("git", "git@ssh.dev.azure.com:v3/acme/project/plugin", GIT_SHA),
        ("git", "ssh://git@github.com/acme/plugin.git", GIT_SHA),
        ("git", "ssh://git@ssh.dev.azure.com/v3/acme/project/plugin", GIT_SHA),
        ("local", "/tmp/user@example.invalid/plugin", None),
        ("local", "/tmp/plugin@release", None),
    ],
)
def test_credential_free_non_url_locators_remain_supported(
    source_kind: str,
    source_locator: str,
    resolved_ref: str | None,
) -> None:
    record = InstalledPluginRecordCodec.build(
        identity="stable-plugin",
        version="1.0.0",
        source_kind=source_kind,
        source_locator=source_locator,
        resolved_ref=resolved_ref,
        source_digest=None,
        scope="project",
        components=(),
        prior_record=None,
    )

    assert record.source_locator == source_locator


@pytest.mark.parametrize(
    ("source_kind", "source_locator", "resolved_ref", "source_digest"),
    [
        ("local", "file:///tmp/plugin@release", None, None),
        ("local", "file://localhost/tmp/plugin", None, None),
        ("cache", "/cache/plugin@release", "cache-key", None),
        ("cache", "file:///cache/plugin", "cache-key", None),
        ("archive", "/tmp/plugin.zip", None, SOURCE_DIGEST),
        ("archive", "https://example.invalid/plugin.zip", None, SOURCE_DIGEST),
        ("registry", "registry.example.invalid/acme/plugin", None, SOURCE_DIGEST),
        ("registry", "https://registry.example.invalid/acme/plugin", None, SOURCE_DIGEST),
    ],
)
def test_source_kind_positive_locator_shapes_remain_supported(
    source_kind: str,
    source_locator: str,
    resolved_ref: str | None,
    source_digest: str | None,
) -> None:
    record = InstalledPluginRecordCodec.build(
        identity="stable-plugin",
        version="1.0.0",
        source_kind=source_kind,
        source_locator=source_locator,
        resolved_ref=resolved_ref,
        source_digest=source_digest,
        scope="project",
        components=(),
        prior_record=None,
    )

    assert record.source_locator == source_locator


@pytest.mark.parametrize(
    ("source_kind", "source_locator", "resolved_ref", "source_digest"),
    [
        ("local", "file://user:password@localhost/tmp/plugin", None, None),
        ("cache", "file://oauth2:TOKEN@localhost/cache/plugin", "cache-key", None),
        (
            "archive",
            "https://x-access-token:ghs_TOKEN@example.invalid/plugin.zip",
            None,
            SOURCE_DIGEST,
        ),
        (
            "registry",
            "https://oauth2:TOKEN@registry.example.invalid/acme/plugin",
            None,
            SOURCE_DIGEST,
        ),
        ("git", "ssh://oauth2:TOKEN@github.com/acme/plugin.git", GIT_SHA, None),
    ],
)
def test_all_source_kinds_reject_url_userinfo_credentials(
    source_kind: str,
    source_locator: str,
    resolved_ref: str | None,
    source_digest: str | None,
) -> None:
    with pytest.raises(ValueError, match="credential-free"):
        InstalledPluginRecordCodec.build(
            identity="stable-plugin",
            version="1.0.0",
            source_kind=source_kind,
            source_locator=source_locator,
            resolved_ref=resolved_ref,
            source_digest=source_digest,
            scope="project",
            components=(),
            prior_record=None,
        )


@pytest.mark.parametrize(
    ("source_kind", "source_locator", "resolved_ref", "source_digest"),
    [
        ("local", "https:placeholder@example.invalid/plugin", None, None),
        ("cache", "file:oauth2:TOKEN@localhost/cache", "cache-key", None),
        (
            "archive",
            "https:x-access-token@secret.example.invalid/plugin.zip",
            None,
            SOURCE_DIGEST,
        ),
        (
            "registry",
            "https:oauth2@secret.registry.example.invalid/plugin",
            None,
            SOURCE_DIGEST,
        ),
    ],
)
def test_non_git_paths_reject_malformed_uri_prefixes(
    source_kind: str,
    source_locator: str,
    resolved_ref: str | None,
    source_digest: str | None,
) -> None:
    with pytest.raises(ValueError, match="valid source-kind path or URL"):
        InstalledPluginRecordCodec.build(
            identity="stable-plugin",
            version="1.0.0",
            source_kind=source_kind,
            source_locator=source_locator,
            resolved_ref=resolved_ref,
            source_digest=source_digest,
            scope="project",
            components=(),
            prior_record=None,
        )


def test_file_url_rejects_port_without_host() -> None:
    with pytest.raises(ValueError, match="invalid file URL"):
        InstalledPluginRecordCodec.build(
            identity="stable-plugin",
            version="1.0.0",
            source_kind="local",
            source_locator="file://:123/tmp/plugin",
            resolved_ref=None,
            source_digest=None,
            scope="project",
            components=(),
            prior_record=None,
        )


def test_normal_root_removal_retains_plugin_data(tmp_path: Path) -> None:
    record = _record("stable-plugin")
    roots = resolve_installed_plugin_record_roots(record, tmp_path)
    plugin_root = roots.plugin_root
    data_root = roots.data_root
    plugin_root.mkdir(parents=True)
    data_root.mkdir(parents=True)
    (plugin_root / "payload.txt").write_text("owned", encoding="utf-8")
    (data_root / "state.json").write_text("persistent", encoding="utf-8")

    remove_installed_plugin_root(record, tmp_path)

    assert not plugin_root.exists()
    assert (data_root / "state.json").read_text(encoding="utf-8") == "persistent"


def test_prepare_rejects_source_root_different_from_validated_plugin(tmp_path: Path) -> None:
    validated_source = tmp_path / "validated"
    different_source = tmp_path / "different"
    _write_plugin(validated_source, name="validated-plugin")
    _write_plugin(different_source, name="different-plugin")
    validation = validate_apm_package(validated_source)

    with pytest.raises(ValueError, match="differs from validated plugin root"):
        prepare_installed_plugin_state(
            validation,
            different_source,
            tmp_path / "project",
            LockFile(),
            global_=False,
            source_kind="local",
            source_locator=str(different_source),
        )


@pytest.mark.parametrize("global_", [False, True])
def test_managed_destination_symlink_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    global_: bool,
) -> None:
    state_base = tmp_path / ("apm-home" if global_ else "project")
    if global_:
        monkeypatch.setenv("APM_HOME", str(state_base))
    record = _record("stable-plugin")
    if global_:
        plugin_root, data_root = InstalledPluginRecordCodec.root_values(
            record.identity,
            "user",
        )
        record = InstalledPluginRecord(
            record_version=record.record_version,
            ownership_generation=record.ownership_generation,
            identity=record.identity,
            version=record.version,
            source_kind=record.source_kind,
            source_locator=record.source_locator,
            resolved_ref=record.resolved_ref,
            source_digest=record.source_digest,
            plugin_root=plugin_root,
            data_root=data_root,
            scope="user",
            components=record.components,
        )
    unrelated = state_base / "unrelated"
    unrelated.mkdir(parents=True)
    logical_root = state_base / record.plugin_root
    logical_root.parent.mkdir(parents=True)
    logical_root.symlink_to(unrelated, target_is_directory=True)

    with pytest.raises(ValueError, match="cannot contain symbolic links"):
        resolve_installed_plugin_record_roots(record, tmp_path / "project")

    assert unrelated.exists()


def test_finalized_state_cannot_rollback_active_root_or_metadata(tmp_path: Path) -> None:
    source = tmp_path / "source"
    project = tmp_path / "project"
    _write_plugin(source)
    validation = validate_apm_package(source)
    lockfile = LockFile()
    prepared = prepare_installed_plugin_state(
        validation,
        source,
        project,
        lockfile,
        global_=False,
        source_kind="local",
        source_locator=str(source),
    )

    prepared.commit()
    prepared.finalize()
    active_root = prepared.root.plugin_root

    with pytest.raises(RuntimeError, match="cannot be rolled back"):
        prepared.rollback()

    assert active_root.exists()
    assert lockfile.installed_plugins == prepared.replacement_records
    assert lockfile.deployment_ledger == prepared.ledger.replacement


def test_managed_path_canonicalizes_noncanonical_contained_input(tmp_path: Path) -> None:
    state_base = tmp_path / "state"
    state_base.mkdir()

    managed = _managed_path(state_base / "nested" / ".." / "plugin", state_base)

    assert managed == state_base / "plugin"


def test_owner_replacement_rejects_unrelated_active_owner_takeover() -> None:
    owner = _record("stable-plugin").owner_key
    locator = ".github/agents/shared.agent.md"
    unrelated = _owned_record("owner/dependency", locator, "sha256:dependency")
    desired = _owned_record(owner, locator, "sha256:plugin")
    prior = DeploymentLedger(records={unrelated.locator.key: unrelated})

    with pytest.raises(ValueError, match="actively owned by an unrelated owner"):
        DeploymentLedgerCodec.prepare_owner_replacement(prior, owner, (desired,))

    assert prior.records == {unrelated.locator.key: unrelated}


def test_owner_replacement_preserves_shared_claim_when_plugin_is_already_active() -> None:
    owner = _record("stable-plugin").owner_key
    locator = _locator(".github/agents/shared.agent.md")
    prior_record = DeploymentRecord(
        locator=locator,
        owners=("owner/dependency", owner),
        active_owner=owner,
        content_hash="sha256:old",
    )
    desired = _owned_record(owner, locator.value, "sha256:new")

    transition = DeploymentLedgerCodec.prepare_owner_replacement(
        DeploymentLedger(records={locator.key: prior_record}),
        owner,
        (desired,),
    )

    replacement = transition.replacement.records[locator.key]
    assert replacement.owners == ("owner/dependency", owner)
    assert replacement.active_owner == owner
    assert replacement.content_hash == "sha256:new"
