"""Codex native output must not expose APM's registry identity as a setting."""

from pathlib import Path

import pytest
import tomlkit

from apm_cli.adapters.client.codex import CodexClientAdapter
from apm_cli.core.conflict_detector import MCPConflictDetector
from apm_cli.core.safe_installer import SafeMCPInstaller
from apm_cli.install.mcp.ownership import resolve_mcp_target_servers
from apm_cli.integration.mcp_integrator import MCPIntegrator
from apm_cli.models.dependency.mcp import MCPDependency
from apm_cli.registry.operations import MCPServerOperations

pytestmark = pytest.mark.component


@pytest.fixture(params=["stdio", "http"])
def dependency(request: pytest.FixtureRequest) -> MCPDependency:
    """Use transports from #3087 without registry or server network access."""
    if request.param == "stdio":
        return MCPDependency.from_dict(
            {
                "name": "managed",
                "registry": False,
                "transport": "stdio",
                "command": "echo",
                "args": ["hello"],
            }
        )
    return MCPDependency.from_dict(
        {
            "name": "managed",
            "registry": False,
            "transport": "http",
            "url": "https://example.test/mcp",
        }
    )


def test_native_output_preserves_uuid_conflicts_after_reparse(
    tmp_path: Path, dependency: MCPDependency
) -> None:
    """Removing native id must not cause duplicate installations under aliases."""
    adapter = CodexClientAdapter(project_root=tmp_path)
    info = MCPIntegrator._build_self_defined_info(dependency)
    info["id"] = 'registry-uuid"\\\n# still data'
    assert adapter.configure_mcp_server("managed", server_info_cache={"managed": info})
    native_path = Path(adapter.get_config_path())
    native = tomlkit.parse(native_path.read_text(encoding="utf-8"))
    assert "id" not in native["mcp_servers"]["managed"]

    # Another target write must preserve the first entry's identity annotation.
    assert adapter.update_config({"other": {"command": "echo", "args": []}})
    fresh_adapter = CodexClientAdapter(project_root=tmp_path)
    detector = MCPConflictDetector(fresh_adapter)
    assert detector.check_server_exists("alias", server_info=info)
    assert not detector.check_server_exists("different", server_info={"id": "different-id"})
    before = native_path.read_bytes()
    summary = SafeMCPInstaller("codex", project_root=tmp_path).install_servers(
        ["alias"], server_info_cache={"alias": info}
    )
    assert summary.skipped == [{"server": "alias", "reason": "already configured"}]
    assert not summary.installed and not summary.failed
    assert native_path.read_bytes() == before
    operations = MCPServerOperations()
    assert (
        operations.check_servers_needing_installation(
            ["codex"], ["alias"], project_root=tmp_path, server_info_cache={"alias": info}
        )
        == []
    )


@pytest.mark.parametrize("owned", [True, False])
def test_install_corrects_only_recorded_managed_entries(
    tmp_path: Path, dependency: MCPDependency, owned: bool
) -> None:
    """An unchanged manifest still repairs owned legacy output, not user entries."""
    adapter = CodexClientAdapter(project_root=tmp_path)
    # Write the old output independently of the formatter being repaired.
    legacy = (
        'command = "echo"\nargs = ["hello"]\nid = "legacy-uuid"\n'
        if dependency.command
        else 'url = "https://example.test/mcp"\nid = "legacy-uuid"\n'
    )
    user_settings = '\n[mcp_servers.personal] # user note\ncommand = "custom"\nid = "personal-id"\n'
    native_path = Path(adapter.get_config_path())
    native_path.parent.mkdir()
    original = (
        '# user config\nmodel = "gpt-5"\n[mcp_servers.managed] # keep note\n'
        + legacy
        + user_settings
    )
    native_path.write_text(original, encoding="utf-8")
    owners = {"codex": {"managed"}} if owned else {}

    MCPIntegrator.install(
        [dependency],
        project_root=tmp_path,
        explicit_target="codex",
        stored_mcp_configs={"managed": dependency.to_dict()},
        managed_target_servers=owners,
    )

    written = native_path.read_text(encoding="utf-8")
    native = tomlkit.parse(written)
    assert ("id" not in native["mcp_servers"]["managed"]) is owned
    assert native["mcp_servers"]["personal"]["id"] == "personal-id"
    assert user_settings in written
    assert native["model"] == "gpt-5"
    assert "# keep note" in written
    assert owners == ({"codex": {"managed"}} if owned else {})
    if owned:
        assert MCPConflictDetector(adapter).check_server_exists(
            "alias", server_info={"id": "legacy-uuid"}
        )
        MCPIntegrator.install(
            [dependency],
            project_root=tmp_path,
            explicit_target="codex",
            stored_mcp_configs={"managed": dependency.to_dict()},
            managed_target_servers=owners,
        )
        assert native_path.read_text(encoding="utf-8") == written
    else:
        assert written == original


@pytest.mark.parametrize("user_edited", [False, True])
def test_legacy_ownership_adoption_still_requires_exact_baseline(
    tmp_path: Path, dependency: MCPDependency, user_edited: bool
) -> None:
    """Without per-target ownership, id alone cannot claim a user's entry."""
    adapter = CodexClientAdapter(project_root=tmp_path)
    legacy = (
        {"command": "echo", "args": ["hello"], "env": {}, "id": ""}
        if dependency.command
        else {"url": "https://example.test/mcp", "id": ""}
    )
    if user_edited:
        legacy["startup_timeout_sec"] = 90
    native_path = Path(adapter.get_config_path())
    native_path.parent.mkdir()
    native_path.write_text(tomlkit.dumps({"mcp_servers": {"managed": legacy}}), encoding="utf-8")
    ownership = resolve_mcp_target_servers(
        recorded_target_servers={},
        ownership_present=False,
        server_names={"managed"},
        stored_configs={"managed": dependency.to_dict()},
        project_root=tmp_path,
        user_scope=False,
    )
    assert ownership == ({} if user_edited else {"codex": {"managed"}})


def _write_out_of_order_config(
    adapter: CodexClientAdapter,
    dependency: MCPDependency,
    *,
    legacy_id: bool,
    out_of_order: bool = True,
) -> str:
    """Place another server before the first server's subtable, valid TOML."""
    native_path = Path(adapter.get_config_path())
    native_path.parent.mkdir()
    header = "[mcp_servers.managed] # keep note\n"
    if legacy_id:
        header += 'id = "registry-uuid"\n'
    transport = (
        'command = "echo"\nargs = ["hello"]\n'
        if dependency.command
        else 'url = "https://example.test/mcp"\n'
    )
    if not legacy_id:
        transport = transport.replace("\n", ' # apm-registry-id: "registry-uuid"\n', 1)
    subtable = "env" if dependency.command else "http_headers"
    personal = '\n[mcp_servers.personal] # personal note\ncommand = "custom"\nid = "personal-id"\n'
    nested = f'\n[mcp_servers.managed.{subtable}] # late subtable\nTEST = "value"\n'
    raw = header + transport + (personal + nested if out_of_order else nested + personal)
    native_path.write_text(raw, encoding="utf-8")
    return raw


def test_out_of_order_subtable_preserves_uuid_conflicts(
    tmp_path: Path, dependency: MCPDependency
) -> None:
    """Reordering valid TOML subtables must not cause alias installations."""
    adapter = CodexClientAdapter(project_root=tmp_path)
    original = _write_out_of_order_config(adapter, dependency, legacy_id=False)
    info = {**MCPIntegrator._build_self_defined_info(dependency), "id": "registry-uuid"}
    assert MCPConflictDetector(adapter).check_server_exists("alias", server_info=info)
    assert (
        MCPServerOperations().check_servers_needing_installation(
            ["codex"], ["alias"], project_root=tmp_path, server_info_cache={"alias": info}
        )
        == []
    )
    summary = SafeMCPInstaller("codex", project_root=tmp_path).install_servers(
        ["alias"], server_info_cache={"alias": info}
    )
    assert summary.skipped == [{"server": "alias", "reason": "already configured"}]
    assert not summary.installed and not summary.failed
    assert Path(adapter.get_config_path()).read_text(encoding="utf-8") == original


@pytest.mark.parametrize("owned", [True, False])
def test_out_of_order_subtable_migrates_only_owned_legacy_entry(
    tmp_path: Path, dependency: MCPDependency, owned: bool
) -> None:
    """ID removal must preserve an owned entry split across concrete tables."""
    adapter = CodexClientAdapter(project_root=tmp_path)
    original = _write_out_of_order_config(adapter, dependency, legacy_id=True)
    migrated = adapter.migrate_legacy_managed_servers({"managed"} if owned else set())
    assert migrated == ({"managed"} if owned else set())
    written = Path(adapter.get_config_path()).read_text(encoding="utf-8")
    expected = original
    if owned:
        expected = expected.replace('id = "registry-uuid"\n', "")
        anchor = 'command = "echo"' if dependency.command else 'url = "https://example.test/mcp"'
        expected = expected.replace(anchor, anchor + ' # apm-registry-id: "registry-uuid"')
    assert written == expected
    assert MCPConflictDetector(adapter).check_server_exists(
        "alias", server_info={"id": "registry-uuid"}
    )
    assert adapter.migrate_legacy_managed_servers({"managed"} if owned else set()) == set()
    assert Path(adapter.get_config_path()).read_text(encoding="utf-8") == written


@pytest.mark.parametrize("out_of_order", [False, True])
def test_legacy_adoption_does_not_claim_registry_annotated_entry(
    tmp_path: Path, dependency: MCPDependency, out_of_order: bool
) -> None:
    """A nonempty UUID still disqualifies a self-defined legacy baseline match."""
    if dependency.command:
        dependency.env = {"TEST": "value"}
    else:
        dependency.headers = {"TEST": "value"}
    adapter = CodexClientAdapter(project_root=tmp_path)
    original = _write_out_of_order_config(
        adapter, dependency, legacy_id=False, out_of_order=out_of_order
    )
    ownership = resolve_mcp_target_servers(
        recorded_target_servers={},
        ownership_present=False,
        server_names={"managed"},
        stored_configs={"managed": dependency.to_dict()},
        project_root=tmp_path,
        user_scope=False,
    )
    assert ownership == {}
    assert Path(adapter.get_config_path()).read_text(encoding="utf-8") == original


def _write_inline_config(
    adapter: CodexClientAdapter,
    dependency: MCPDependency,
    *,
    registry_id: str | None,
    comment: str = "# keep my comment",
) -> str:
    """Write an inline server without converting it to a standard table."""
    fields = (
        'command="echo", args=["hello"], env={}'
        if dependency.command
        else 'url="https://example.test/mcp"'
    )
    if registry_id is not None:
        fields += f', id="{registry_id}"'
    original = (
        f"[mcp_servers]\nmanaged = {{{fields}}} {comment}\n"
        'personal = {command="custom", id="personal-id"} # user comment\n'
    )
    path = Path(adapter.get_config_path())
    path.parent.mkdir()
    path.write_text(original, encoding="utf-8")
    return original


def test_inline_legacy_baseline_is_corrected_on_install(
    tmp_path: Path, dependency: MCPDependency
) -> None:
    """Exact legacy inline baselines must not retain the unsupported empty id."""
    adapter = CodexClientAdapter(project_root=tmp_path)
    original = _write_inline_config(adapter, dependency, registry_id="")
    stored_configs = {"managed": dependency.to_dict()}
    ownership = resolve_mcp_target_servers(
        recorded_target_servers={},
        ownership_present=False,
        server_names={"managed"},
        stored_configs=stored_configs,
        project_root=tmp_path,
        user_scope=False,
    )
    assert ownership == {"codex": {"managed"}}
    MCPIntegrator.install(
        [dependency],
        project_root=tmp_path,
        explicit_target="codex",
        stored_mcp_configs=stored_configs,
        managed_target_servers=ownership,
    )
    written = Path(adapter.get_config_path()).read_text(encoding="utf-8")
    native = tomlkit.parse(written)["mcp_servers"]
    assert "id" not in native["managed"]
    assert native["managed"].trivia.comment == "# keep my comment"
    assert original.splitlines()[-1] in written
    assert ownership == {"codex": {"managed"}}


@pytest.mark.parametrize("owned", [False, True])
def test_inline_uuid_migration_preserves_conflicts_and_unowned_entries(
    tmp_path: Path, dependency: MCPDependency, owned: bool
) -> None:
    """Inline UUIDs remain usable for conflicts, and only owners authorize repair."""
    adapter = CodexClientAdapter(project_root=tmp_path)
    original = _write_inline_config(adapter, dependency, registry_id="registry-uuid")
    managed = {"managed"} if owned else set()
    assert adapter.migrate_legacy_managed_servers(managed) == managed
    path = Path(adapter.get_config_path())
    written = path.read_text(encoding="utf-8")
    native = tomlkit.parse(written)["mcp_servers"]
    assert ("id" not in native["managed"]) is owned
    assert "# keep my comment" in written
    assert original.splitlines()[-1] in written
    if not owned:
        assert written == original
    info = {**MCPIntegrator._build_self_defined_info(dependency), "id": "registry-uuid"}
    assert MCPConflictDetector(adapter).check_server_exists("alias", server_info=info)
    assert (
        MCPServerOperations().check_servers_needing_installation(
            ["codex"], ["alias"], project_root=tmp_path, server_info_cache={"alias": info}
        )
        == []
    )
    summary = SafeMCPInstaller("codex", project_root=tmp_path).install_servers(
        ["alias"], server_info_cache={"alias": info}
    )
    assert summary.skipped == [{"server": "alias", "reason": "already configured"}]
    assert not summary.installed and not summary.failed
    assert adapter.migrate_legacy_managed_servers(managed) == set()
    assert path.read_text(encoding="utf-8") == written


def test_inline_registry_comment_blocks_legacy_ownership_adoption(
    tmp_path: Path, dependency: MCPDependency
) -> None:
    """Moving a UUID into an inline comment cannot turn a user's server into APM's."""
    adapter = CodexClientAdapter(project_root=tmp_path)
    original = _write_inline_config(
        adapter,
        dependency,
        registry_id=None,
        comment='# apm-registry-id: "user-registry-uuid" # keep my comment',
    )
    ownership = resolve_mcp_target_servers(
        recorded_target_servers={},
        ownership_present=False,
        server_names={"managed"},
        stored_configs={"managed": dependency.to_dict()},
        project_root=tmp_path,
        user_scope=False,
    )
    assert ownership == {}
    assert Path(adapter.get_config_path()).read_text(encoding="utf-8") == original


@pytest.mark.parametrize("full_root_keys", [False, True])
@pytest.mark.parametrize("registry_id", ["", "registry-uuid"])
def test_dotted_legacy_config_preserves_identity_and_layout(
    tmp_path: Path, dependency: MCPDependency, full_root_keys: bool, registry_id: str
) -> None:
    """Implicit dotted server mappings migrate without materializing new tables."""
    prefix = "mcp_servers." if full_root_keys else ""
    header = "" if full_root_keys else "[mcp_servers]\n"
    transport = (
        f'{prefix}managed.command = "echo" # anchor note\n'
        f'{prefix}managed.args = ["hello"]\n'
        f"{prefix}managed.env = {{}}\n"
        if dependency.command
        else f'{prefix}managed.url = "https://example.test/mcp" # anchor note\n'
    )
    identity_line = f'{prefix}managed.id = "{registry_id}"\n'
    original = (
        header
        + transport
        + identity_line
        + f'{prefix}personal.command = "custom" # user note\n'
        + f'{prefix}personal.id = "personal-id"\n'
    )
    adapter = CodexClientAdapter(project_root=tmp_path)
    path = Path(adapter.get_config_path())
    path.parent.mkdir()
    path.write_text(original, encoding="utf-8")
    stored = {"managed": dependency.to_dict()}
    ownership = resolve_mcp_target_servers(
        recorded_target_servers={"codex": {"managed"}} if registry_id else {},
        ownership_present=bool(registry_id),
        server_names={"managed"},
        stored_configs=stored,
        project_root=tmp_path,
        user_scope=False,
    )
    assert ownership == {"codex": {"managed"}}
    MCPIntegrator.install(
        [dependency],
        project_root=tmp_path,
        explicit_target="codex",
        stored_mcp_configs=stored,
        managed_target_servers=ownership,
    )
    written = path.read_text(encoding="utf-8")
    expected = original.replace(identity_line, "")
    if registry_id:
        expected = expected.replace(
            "# anchor note", '# apm-registry-id: "registry-uuid" # anchor note'
        )
    assert written == expected
    assert "id" not in tomlkit.parse(written)["mcp_servers"]["managed"]
    assert adapter.migrate_legacy_managed_servers({"managed"}) == set()
    assert path.read_text(encoding="utf-8") == written
    if registry_id:
        info = {**MCPIntegrator._build_self_defined_info(dependency), "id": registry_id}
        assert MCPConflictDetector(adapter).check_server_exists("alias", server_info=info)
        assert (
            MCPServerOperations().check_servers_needing_installation(
                ["codex"], ["alias"], project_root=tmp_path, server_info_cache={"alias": info}
            )
            == []
        )
        assert (
            resolve_mcp_target_servers(
                recorded_target_servers={},
                ownership_present=False,
                server_names={"managed"},
                stored_configs=stored,
                project_root=tmp_path,
                user_scope=False,
            )
            == {}
        )


@pytest.mark.parametrize("with_personal", [False, True])
def test_fresh_registry_server_in_inline_parent_remains_valid_toml(
    tmp_path: Path, dependency: MCPDependency, with_personal: bool
) -> None:
    """Adding annotated output to an inline mcp_servers parent must stay parseable."""
    adapter = CodexClientAdapter(project_root=tmp_path)
    personal = 'personal={command="custom",args=["one","two"]}' if with_personal else ""
    path = Path(adapter.get_config_path())
    path.parent.mkdir()
    path.write_text(
        f'mcp_servers = {{{personal}}} # parent note\nmodel = "gpt-5" # model note\n',
        encoding="utf-8",
    )
    info = {**MCPIntegrator._build_self_defined_info(dependency), "id": "registry-uuid"}
    assert adapter.configure_mcp_server("managed", server_info_cache={"managed": info})
    written = path.read_text(encoding="utf-8")
    native = tomlkit.parse(written)
    assert "id" not in native["mcp_servers"]["managed"]
    assert adapter.get_registry_id(native["mcp_servers"]["managed"]) == "registry-uuid"
    assert native["model"] == "gpt-5"
    assert "# parent note" in written and "# model note" in written
    if with_personal:
        assert native["mcp_servers"]["personal"] == {"command": "custom", "args": ["one", "two"]}
    else:
        assert set(native["mcp_servers"]) == {"managed"}


@pytest.mark.parametrize("inline_parent", [False, True])
@pytest.mark.parametrize("id_position", [0, 1, 3], ids=["first", "middle", "last"])
@pytest.mark.parametrize("registry_id", ["", "registry-uuid"])
def test_inline_id_positions_migrate_without_invalid_commas_or_nested_comments(
    tmp_path: Path,
    dependency: MCPDependency,
    inline_parent: bool,
    id_position: int,
    registry_id: str,
) -> None:
    """Deleting any inline id preserves both the owned server and its neighbors."""
    fields = (
        ['command="echo"', 'args=["hello"]', "enabled=false"]
        if dependency.command
        else ['url="https://example.test/mcp"', 'http_headers={TEST="value"}', "enabled=false"]
    )
    fields.insert(id_position, f'id="{registry_id}"')
    managed = "managed={" + ",".join(fields) + "}"
    personal = 'personal={command="custom",id="personal-id",env={TEST="keep"}}'
    original = (
        f"mcp_servers={{{managed},{personal}}} # parent note\n"
        if inline_parent
        else f"[mcp_servers] # parent note\n{managed} # managed note\n{personal} # personal note\n"
    )
    adapter = CodexClientAdapter(project_root=tmp_path)
    path = Path(adapter.get_config_path())
    path.parent.mkdir()
    path.write_text(original, encoding="utf-8")
    original_native = tomlkit.parse(original)
    # No ownership means no layout conversion and no field removal.
    assert adapter.migrate_legacy_managed_servers(set()) == set()
    assert path.read_text(encoding="utf-8") == original

    assert adapter.migrate_legacy_managed_servers({"managed"}) == {"managed"}
    written = path.read_text(encoding="utf-8")
    native = tomlkit.parse(written)["mcp_servers"]
    assert "id" not in native["managed"]
    assert native["managed"]["enabled"] is False
    assert native["personal"] == original_native["mcp_servers"]["personal"]
    assert native["managed"] == {
        key: value
        for key, value in original_native["mcp_servers"]["managed"].items()
        if key != "id"
    }
    assert "# parent note" in written
    if not inline_parent:
        assert "# managed note" in written and "# personal note" in written
    if registry_id:
        assert adapter.get_registry_id(native["managed"]) == registry_id
        assert MCPConflictDetector(adapter).check_server_exists(
            "alias", server_info={"id": registry_id}
        )
    assert adapter.migrate_legacy_managed_servers({"managed"}) == set()
    assert path.read_text(encoding="utf-8") == written


def test_invalid_serialization_never_replaces_native_config(
    tmp_path: Path, dependency: MCPDependency, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A serializer failure must leave the previous configuration byte-for-byte intact."""
    adapter = CodexClientAdapter(project_root=tmp_path)
    path = Path(adapter.get_config_path())
    path.parent.mkdir()
    original = '[mcp_servers.personal]\ncommand = "custom" # preserve\n'
    path.write_text(original, encoding="utf-8")
    info = {**MCPIntegrator._build_self_defined_info(dependency), "id": "registry-uuid"}
    monkeypatch.setattr("apm_cli.adapters.client.codex.tomlkit.dumps", lambda _config: "[invalid")
    assert not adapter.configure_mcp_server("managed", server_info_cache={"managed": info})
    assert path.read_text(encoding="utf-8") == original
