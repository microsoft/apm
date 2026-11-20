"""Exercise Claude cleanup containment and retained-target hook preservation."""

import json
from pathlib import Path

import pytest

from apm_cli.claude_plugins.admission import ClaudeNativePluginError
from apm_cli.core.scope import InstallScope
from apm_cli.install.context import InstallContext
from apm_cli.integration.hook_integrator import HookIntegrator
from apm_cli.integration.mcp_integrator import MCPIntegrator
from apm_cli.integration.targets import KNOWN_TARGETS
from tests.unit.install.test_claude_native_hooks import _fixture, _integrate

pytestmark = pytest.mark.component


@pytest.mark.req("req-tg-002")
def test_claude_cleanup_writes_only_to_selected_registered_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An active Claude root override excludes default and other-profile writes."""
    home = tmp_path / "home"
    selected = tmp_path / "selected"
    other = tmp_path / "other"
    payload = {
        "theme": "dark",
        "mcpServers": {
            "managed-stale": {"type": "stdio", "command": "old", "args": []},
            "user-owned": {"command": "keep"},
        },
    }
    paths = [root / ".claude.json" for root in (home, selected, other)]
    for path in paths:
        path.parent.mkdir()
        path.write_text(json.dumps(payload), encoding="utf-8")
    outside_before = {path: path.read_bytes() for path in (paths[0], paths[2])}
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(selected))

    MCPIntegrator.remove_stale(
        stale_names={"managed-stale"},
        runtime="claude",
        project_root=tmp_path,
        scope=InstallScope.USER,
        fail_on_write_error=True,
        stored_configs={
            "managed-stale": {
                "name": "managed-stale",
                "registry": False,
                "transport": "stdio",
                "command": "old",
            }
        },
    )

    assert {path: path.read_bytes() for path in outside_before} == outside_before
    assert json.loads(paths[1].read_text(encoding="utf-8")) == {
        "theme": "dark",
        "mcpServers": {"user-owned": {"command": "keep"}},
    }


@pytest.mark.req("req-lk-021")
@pytest.mark.parametrize(
    "state", ["owned", "orphaned", "malformed-config", "malformed-sidecar", "non-object-sidecar"]
)
def test_native_transition_preserves_retained_target_hook_state(tmp_path: Path, state: str) -> None:
    """A retained Claude target is not a deletion grant for native migration."""
    package = _fixture(tmp_path / ".apm/apm_modules/owned")
    native = tmp_path / ".claude/settings.json"
    sidecar = native.with_name("apm-hooks.json")
    native.parent.mkdir()
    native.write_text(
        '{"hooks":{"SessionStart":[{"hooks":[{"type":"command","command":"echo USER"}]}]},'
        '"alwaysThinkingEnabled":false}',
        encoding="utf-8",
    )
    installed = HookIntegrator().integrate_package_hooks_claude(package, tmp_path, user_scope=True)
    assert installed.hooks_integrated == 1
    if state == "orphaned":
        native.unlink()
    elif state == "malformed-config":
        native.write_text("not-json", encoding="utf-8")
    elif state == "malformed-sidecar":
        sidecar.write_text("not-json", encoding="utf-8")
    elif state == "non-object-sidecar":
        sidecar.write_text("[]", encoding="utf-8")
    before = {path: path.read_bytes() for path in (native, sidecar) if path.exists()}
    stats = HookIntegrator().reconcile_package_target_restriction(
        package,
        tmp_path,
        [KNOWN_TARGETS["claude"]],
        dry_run=True,
    )
    assert stats["files_removed"] == {"owned": 2, "orphaned": 1}.get(state, 0)
    assert stats["errors"] == int(state not in {"owned", "orphaned"})
    ctx = InstallContext(project_root=tmp_path, apm_dir=tmp_path / ".apm", scope=InstallScope.USER)

    with pytest.raises(ClaudeNativePluginError, match="Hook settings were preserved") as error:
        _integrate(tmp_path, package, ctx=ctx)

    assert {path: path.read_bytes() for path in (native, sidecar) if path.exists()} == before
    assert not ctx.claude_native_plugins
    if state in {"owned", "orphaned"}:
        assert "apm uninstall --global" in str(error.value)
    else:
        failed_path = native if state == "malformed-config" else sidecar
        assert failed_path.name in str(error.value)
        assert "Repair unreadable hook settings or ownership" in str(error.value)
