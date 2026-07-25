from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from apm_cli.core.deployment_state import (
    DeploymentLedger,
    DeploymentLocator,
    DeploymentRecord,
    LocatorKind,
)
from apm_cli.deps.lockfile import LockFile
from tests.utils.lifecycle_state import LifecycleStateRoot, LifecycleStateSnapshot


def _record(
    *,
    kind: LocatorKind,
    target: str,
    value: str,
    content_hash: str | None = None,
    scope: str = "project",
) -> DeploymentRecord:
    locator = DeploymentLocator(
        kind=kind,
        target=target,
        value=value,
        runtime=None,
        scope=scope,
    )
    return DeploymentRecord(
        locator=locator,
        owners=("fixture",),
        active_owner="fixture",
        content_hash=content_hash,
    )


def _write_lock(
    workspace: Path,
    records: tuple[DeploymentRecord, ...],
    *,
    generated_at: str = "2026-01-01T00:00:00+00:00",
    mcp_label: str = "\u03bb",
) -> None:
    lock = LockFile(generated_at=generated_at)
    lock.deployment_ledger = DeploymentLedger(
        records={record.locator.key: record for record in records}
    )
    lock._deployments_present = True
    lock.mcp_servers = ["fixture-mcp"]
    lock.mcp_configs = {
        "fixture-mcp": {
            "name": "fixture-mcp",
            "transport": "stdio",
            "command": "fixture-mcp",
            "label": mcp_label,
        }
    }
    lock.lsp_servers = ["fixture-lsp"]
    lock.lsp_configs = {
        "fixture-lsp": {
            "name": "fixture-lsp",
            "command": "fixture-lsp",
            "extensionToLanguage": {".py": "python"},
        }
    }
    lock.write(workspace / "apm.lock.yaml")


def test_capture_preserves_raw_bytes_and_canonical_semantics(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manifest = b'name: fixture\nversion: 0.1.0\ndescription: "\\u03bb"\n'
    (workspace / "apm.yml").write_bytes(manifest)
    deployed = workspace / ".agents/skills/review/SKILL.md"
    deployed.parent.mkdir(parents=True)
    deployed_bytes = b"---\nname: review\n---\n# \xcf\x80\n"
    deployed.write_bytes(deployed_bytes)
    (workspace / "AGENTS.md").write_bytes(b"# Compiled\n")
    hook_config = workspace / ".claude/settings.json"
    hook_config.parent.mkdir(parents=True)
    hook_config.write_bytes(b'{"hooks":{"PreToolUse":[]}}\n')
    (workspace / ".claude/apm-hooks.json").write_bytes(
        b'{"PreToolUse":[{"_apm_source":"fixture"}]}\n'
    )
    config = workspace / ".vscode/mcp.json"
    config.parent.mkdir(parents=True)
    config.write_bytes(b'{"servers":{"fixture-mcp":{"label":"\xcf\x80"}}}\n')

    file_record = _record(
        kind=LocatorKind.PROJECT_RELATIVE,
        target="agents",
        value=".agents/skills/review/SKILL.md",
        content_hash="sha256:fixture",
    )
    uri_record = _record(
        kind=LocatorKind.URI,
        target="copilot-app",
        value="copilot-app-db://workflows/dynamic-row",
    )
    _write_lock(workspace, (uri_record, file_record))

    snapshot = LifecycleStateSnapshot.capture(
        workspace,
        targets=("claude",),
        config_paths=(PurePosixPath(".vscode/mcp.json"),),
    )

    assert snapshot.manifest_bytes == manifest
    assert snapshot.lockfile_bytes == (workspace / "apm.lock.yaml").read_bytes()
    assert tuple(record.locator.key for record in snapshot.deployment_records) == tuple(
        sorted((file_record.locator.key, uri_record.locator.key))
    )
    assert snapshot.mcp_state_bytes == (
        b'{"configs":{"fixture-mcp":{"command":"fixture-mcp","label":"'
        b'\\u03bb","name":"fixture-mcp","transport":"stdio"}},'
        b'"provenance":{},"servers":["fixture-mcp"],"target_servers":{}}'
    )
    assert snapshot.lsp_state_bytes == (
        b'{"configs":{"fixture-lsp":{"command":"fixture-lsp",'
        b'"extensionToLanguage":{".py":"python"},"name":"fixture-lsp"}},'
        b'"servers":["fixture-lsp"]}'
    )
    assert snapshot.file(".agents/skills/review/SKILL.md").content == deployed_bytes
    assert snapshot.file(".agents/skills/review/SKILL.md").roles == frozenset({"deployment"})
    assert snapshot.file(".claude/settings.json").roles == frozenset({"config", "hook-config"})
    assert snapshot.file(".claude/apm-hooks.json").roles == frozenset({"config", "hook-sidecar"})
    assert snapshot.file(".vscode/mcp.json").content == (
        b'{"servers":{"fixture-mcp":{"label":"\xcf\x80"}}}\n'
    )
    assert snapshot.file("AGENTS.md").roles == frozenset({"compiled"})
    with pytest.raises(KeyError, match="not tracked"):
        snapshot.file("copilot-app-db://workflows/dynamic-row")
    assert tuple(file.relative_path for file in snapshot.files) == tuple(
        sorted(file.relative_path for file in snapshot.files)
    )


def test_semantic_state_ignores_yaml_formatting_and_generated_time(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "apm.yml").write_bytes(b"name: fixture\nversion: 0.1.0\n")
    _write_lock(workspace, ())
    before = LifecycleStateSnapshot.capture(workspace)

    (workspace / "apm.yml").write_bytes(b"{version: 0.1.0, name: fixture}\n")
    _write_lock(workspace, (), generated_at="2026-02-02T00:00:00+00:00")
    after = LifecycleStateSnapshot.capture(workspace)

    assert before.manifest_bytes != after.manifest_bytes
    assert before.lockfile_bytes != after.lockfile_bytes
    assert before.semantic_bytes == after.semantic_bytes


def test_semantic_state_reflects_deployment_and_config_changes(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "apm.yml").write_bytes(b"name: fixture\nversion: 0.1.0\n")
    _write_lock(workspace, ())
    before = LifecycleStateSnapshot.capture(workspace)

    record = _record(
        kind=LocatorKind.PROJECT_RELATIVE,
        target="copilot",
        value=".github/instructions/rules.instructions.md",
    )
    _write_lock(workspace, (record,), mcp_label="changed")
    after = LifecycleStateSnapshot.capture(workspace)

    assert before.deployment_records == ()
    assert after.deployment_records == (record,)
    assert before.mcp_state_bytes != after.mcp_state_bytes
    assert before.semantic_bytes != after.semantic_bytes


def test_missing_state_and_missing_deployment_are_explicit(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    missing_record = _record(
        kind=LocatorKind.PROJECT_RELATIVE,
        target="copilot",
        value=".github/instructions/missing.instructions.md",
    )
    _write_lock(workspace, (missing_record,))

    snapshot = LifecycleStateSnapshot.capture(workspace)

    assert snapshot.manifest_bytes is None
    assert snapshot.lockfile_bytes is not None
    missing = snapshot.file(".github/instructions/missing.instructions.md")
    assert missing.kind == "missing"
    assert missing.content is None
    assert missing.sha256 is None
    assert missing.roles == frozenset({"deployment"})

    empty = LifecycleStateSnapshot.capture(tmp_path / "absent-workspace")
    assert empty.manifest_bytes is None
    assert empty.lockfile_bytes is None
    assert empty.deployment_records == ()
    assert empty.files == ()


def test_capture_rejects_traversal_and_never_follows_workspace_symlinks(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"do-not-read")
    linked = workspace / "linked.bin"
    linked.symlink_to(outside)
    record = _record(
        kind=LocatorKind.PROJECT_RELATIVE,
        target="copilot",
        value="linked.bin",
    )
    _write_lock(workspace, (record,))

    with pytest.raises(ValueError, match="traversal sequence"):
        LifecycleStateSnapshot.capture(
            workspace,
            config_paths=(PurePosixPath("../outside.bin"),),
        )

    snapshot = LifecycleStateSnapshot.capture(workspace)
    state = snapshot.file("linked.bin")
    assert state.kind == "symlink"
    assert state.content is None
    assert state.link_target == str(outside)
    assert outside.read_bytes() == b"do-not-read"

    linked_manifest_workspace = tmp_path / "linked-manifest-workspace"
    linked_manifest_workspace.mkdir()
    (linked_manifest_workspace / "apm.yml").symlink_to(outside)
    with pytest.raises(ValueError, match="regular file"):
        LifecycleStateSnapshot.capture(linked_manifest_workspace)
    assert outside.read_bytes() == b"do-not-read"


def test_capture_rejects_symlinked_ancestor_and_workspace_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "settings.json").write_bytes(b'{"outside":true}\n')

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".claude").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="outside bounded root"):
        LifecycleStateSnapshot.capture(workspace, targets=("claude",))

    linked_workspace = tmp_path / "linked-workspace"
    linked_workspace.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="root must not be a symlink"):
        LifecycleStateSnapshot.capture(linked_workspace)


def test_capture_rejects_unknown_targets(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(KeyError, match="not-a-target"):
        LifecycleStateSnapshot.capture(workspace, targets=("not-a-target",))


def test_capture_rejects_target_relative_deployment_without_bounded_root(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    record = _record(
        kind=LocatorKind.TARGET_RELATIVE,
        target="claude",
        value="rules/external.md",
    )
    _write_lock(workspace, (record,))

    with pytest.raises(ValueError, match="target-relative"):
        LifecycleStateSnapshot.capture(workspace, targets=("claude",))


def test_capture_reads_two_bounded_roots_without_relative_path_collisions(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    claude_root = tmp_path / "claude-home"
    cursor_root = tmp_path / "cursor-home"
    claude_root.mkdir()
    cursor_root.mkdir()
    (claude_root / "settings.json").write_bytes(b'{"client":"claude"}\n')
    (cursor_root / "settings.json").write_bytes(b'{"client":"cursor"}\n')
    claude_script = claude_root / "hooks/fixture/check.py"
    claude_script.parent.mkdir(parents=True)
    claude_script.write_bytes(b"print('bounded')\n")
    records = (
        _record(
            kind=LocatorKind.TARGET_RELATIVE,
            target="claude",
            scope="user",
            value="hooks/fixture/check.py",
            content_hash="sha256:script",
        ),
        _record(
            kind=LocatorKind.TARGET_RELATIVE,
            target="cursor",
            scope="user",
            value="rules/missing.mdc",
        ),
        _record(
            kind=LocatorKind.URI,
            target="copilot-app",
            scope="user",
            value="copilot-app-db://workflows/external",
        ),
    )
    _write_lock(workspace, records)
    roots = (
        LifecycleStateRoot(
            root_id="claude-user",
            target="claude",
            scope="user",
            path=claude_root,
            config_paths=(PurePosixPath("settings.json"),),
        ),
        LifecycleStateRoot(
            root_id="cursor-user",
            target="cursor",
            scope="user",
            path=cursor_root,
            config_paths=(
                PurePosixPath("settings.json"),
                PurePosixPath("rules/missing.mdc"),
            ),
        ),
    )

    snapshot = LifecycleStateSnapshot.capture(workspace, external_roots=roots)

    claude_config = snapshot.file("settings.json", root_id="claude-user")
    cursor_config = snapshot.file("settings.json", root_id="cursor-user")
    script = snapshot.file("hooks/fixture/check.py", root_id="claude-user")
    missing = snapshot.file("rules/missing.mdc", root_id="cursor-user")
    assert claude_config.root_id == "claude-user"
    assert claude_config.root_path == claude_root
    assert claude_config.content == b'{"client":"claude"}\n'
    assert cursor_config.root_id == "cursor-user"
    assert cursor_config.root_path == cursor_root
    assert cursor_config.content == b'{"client":"cursor"}\n'
    assert script.roles == frozenset({"deployment"})
    assert script.content == b"print('bounded')\n"
    assert missing.kind == "missing"
    assert missing.roles == frozenset({"config", "deployment"})
    assert tuple((file.root_id, file.relative_path) for file in snapshot.files) == tuple(
        sorted((file.root_id, file.relative_path) for file in snapshot.files)
    )
    with pytest.raises(KeyError, match="unknown-root"):
        snapshot.file("settings.json", root_id="unknown-root")
    with pytest.raises(KeyError, match="not tracked"):
        snapshot.file("copilot-app-db://workflows/external", root_id="claude-user")


def test_bounded_roots_fail_closed_before_outside_reads(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "user-root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "secret.json"
    outside_file.write_bytes(b'{"secret":"unchanged"}\n')
    (root / "linked.json").symlink_to(outside_file)
    _write_lock(
        workspace,
        (
            _record(
                kind=LocatorKind.TARGET_RELATIVE,
                target="claude",
                scope="user",
                value="linked.json",
            ),
        ),
    )
    bounded = LifecycleStateRoot(
        root_id="claude-user",
        target="claude",
        scope="user",
        path=root,
        config_paths=(PurePosixPath("linked.json"),),
    )

    snapshot = LifecycleStateSnapshot.capture(workspace, external_roots=(bounded,))

    linked = snapshot.file("linked.json", root_id="claude-user")
    assert linked.kind == "symlink"
    assert linked.content is None
    assert linked.link_target == str(outside_file)
    assert outside_file.read_bytes() == b'{"secret":"unchanged"}\n'

    linked_directory = root / "escape"
    linked_directory.symlink_to(outside, target_is_directory=True)
    escaping = LifecycleStateRoot(
        root_id="claude-user",
        target="claude",
        scope="user",
        path=root,
        config_paths=(PurePosixPath("escape/secret.json"),),
    )
    with pytest.raises(ValueError, match="outside bounded root"):
        LifecycleStateSnapshot.capture(workspace, external_roots=(escaping,))
    assert outside_file.read_bytes() == b'{"secret":"unchanged"}\n'


def test_bounded_root_identity_and_path_collisions_are_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    nested = first / "nested"
    nested.mkdir()

    duplicate_id = (
        LifecycleStateRoot("same", "claude", "user", first),
        LifecycleStateRoot("same", "cursor", "user", second),
    )
    duplicate_owner = (
        LifecycleStateRoot("first", "claude", "user", first),
        LifecycleStateRoot("second", "claude", "user", second),
    )
    duplicate_path = (
        LifecycleStateRoot("first", "claude", "user", first),
        LifecycleStateRoot("second", "cursor", "user", first),
    )
    nested_path = (
        LifecycleStateRoot("first", "claude", "user", first),
        LifecycleStateRoot("second", "cursor", "user", nested),
    )
    for roots, message in (
        (duplicate_id, "root_id"),
        (duplicate_owner, "target/scope"),
        (duplicate_path, "overlap"),
        (nested_path, "overlap"),
    ):
        with pytest.raises(ValueError, match=message):
            LifecycleStateSnapshot.capture(workspace, external_roots=roots)

    with pytest.raises(ValueError, match="reserved"):
        LifecycleStateSnapshot.capture(
            workspace,
            external_roots=(LifecycleStateRoot("workspace", "claude", "user", first),),
        )
    with pytest.raises(ValueError, match="traversal sequence"):
        LifecycleStateSnapshot.capture(
            workspace,
            external_roots=(
                LifecycleStateRoot(
                    "claude-user",
                    "claude",
                    "user",
                    first,
                    config_paths=(PurePosixPath("../outside"),),
                ),
            ),
        )
    with pytest.raises(ValueError, match="overlap"):
        LifecycleStateSnapshot.capture(
            workspace,
            external_roots=(LifecycleStateRoot("claude-user", "claude", "user", workspace),),
        )

    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(first, target_is_directory=True)
    with pytest.raises(ValueError, match="must not be a symlink"):
        LifecycleStateSnapshot.capture(
            workspace,
            external_roots=(LifecycleStateRoot("claude-user", "claude", "user", linked_root),),
        )


def test_capture_accepts_catalog_target_without_file_profile(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = workspace / ".idea/mcp.json"
    config.parent.mkdir()
    config.write_bytes(b'{"servers":{}}\n')

    snapshot = LifecycleStateSnapshot.capture(
        workspace,
        targets=("intellij",),
        config_paths=(PurePosixPath(".idea/mcp.json"),),
    )

    assert snapshot.file(".idea/mcp.json").roles == frozenset({"config"})


def test_capture_marks_target_generated_file_as_compiled(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    generated = workspace / ".github/copilot-instructions.md"
    generated.parent.mkdir(parents=True)
    generated.write_bytes(b"# Generated instructions\n")

    snapshot = LifecycleStateSnapshot.capture(workspace, targets=("copilot",))

    assert snapshot.file(".github/copilot-instructions.md").roles == frozenset({"compiled"})


def test_compiled_discovery_requires_a_file_target(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_bytes(b"# Compiled\n")

    semantic_only = LifecycleStateSnapshot.capture(workspace)
    with pytest.raises(KeyError, match="not tracked"):
        semantic_only.file("AGENTS.md")

    with_compiled = LifecycleStateSnapshot.capture(workspace, targets=("claude",))
    assert with_compiled.file("AGENTS.md").roles == frozenset({"compiled"})


def test_capture_reports_directory_kind_for_role_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    config_directory = workspace / ".custom/config.json"
    config_directory.mkdir(parents=True)

    snapshot = LifecycleStateSnapshot.capture(
        workspace,
        config_paths=(PurePosixPath(".custom/config.json"),),
    )

    state = snapshot.file(".custom/config.json")
    assert state.kind == "directory"
    assert state.content is None
    assert state.sha256 is None


def test_capture_reads_legacy_lock_when_canonical_lockfile_is_absent(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    lock = LockFile(generated_at="2026-01-01T00:00:00+00:00")
    lock.write(workspace / "apm.lock")

    snapshot = LifecycleStateSnapshot.capture(workspace)

    assert not (workspace / "apm.lock.yaml").exists()
    assert snapshot.lockfile_bytes == (workspace / "apm.lock").read_bytes()


def test_capture_rejects_windows_drive_and_file_ancestor_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(ValueError, match="relative POSIX"):
        LifecycleStateSnapshot.capture(
            workspace,
            config_paths=(PurePosixPath("C:/outside/config.json"),),
        )

    (workspace / ".custom").write_bytes(b"not-a-directory")
    with pytest.raises(ValueError, match="not a directory"):
        LifecycleStateSnapshot.capture(
            workspace,
            config_paths=(PurePosixPath(".custom/config.json"),),
        )
