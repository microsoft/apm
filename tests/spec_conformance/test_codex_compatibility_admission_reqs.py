"""Existing executable/source-plan obligations on the Codex compatibility consumer.

These tests do not qualify a native client or extend the schema-bearing Agent
Plugins v1 lifecycle in req-tg-013. They exercise shared admission before any
native registration, using real package validation, approval and content scans.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from apm_cli.codex_plugins.client import CodexNativeClient, CodexNativeError
from apm_cli.codex_plugins.snapshot import prepare_snapshot
from apm_cli.core.scope import InstallScope
from apm_cli.install.drift import _make_integrators
from apm_cli.install.services import IntegratorBundle, integrate_package_primitives
from apm_cli.integration.targets import resolve_targets
from apm_cli.models.apm_package import DependencyReference
from apm_cli.models.validation import validate_apm_package
from apm_cli.utils.content_hash import compute_native_bundle_hash
from apm_cli.utils.diagnostics import DiagnosticCollector

pytestmark = pytest.mark.component


@pytest.fixture
def codex_admission(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple:
    """Prepare a whole compatibility bundle and real shared consumer gates."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("APM_HOME", str(home / ".apm"))
    source = tmp_path / "publisher"
    (source / ".codex-plugin").mkdir(parents=True)
    (source / ".codex-plugin/plugin.json").write_text(
        json.dumps({"name": "example", "mcpServers": "./mcp.json"})
    )
    (source / "mcp.json").write_text(
        json.dumps({"mcpServers": {"inert": {"command": "DO_NOT_EXECUTE"}}})
    )
    skill = source / "skills/example/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: example\ndescription: inert fixture\n---\nSafe content\n")
    validation = validate_apm_package(source, read_only=True)
    assert validation.is_valid, validation.errors
    package = SimpleNamespace(
        package=validation.package,
        package_type=validation.package_type,
        install_path=source,
        dependency_ref=DependencyReference.parse("owner/example"),
    )
    ctx = SimpleNamespace(dry_run=False, codex_native_plugins={}, blocked_executables=[])

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("admission must not launch a native client")

    monkeypatch.setattr(CodexNativeClient, "for_scope", forbidden)
    options = {
        "targets": resolve_targets(home, user_scope=True, explicit_target="codex"),
        "integrators": IntegratorBundle(**_make_integrators()),
        "force": False,
        "managed_files": set(),
        "diagnostics": DiagnosticCollector(),
        "package_name": "owner/example",
        "scope": InstallScope.USER,
        "ctx": ctx,
    }
    return package, home, options, ctx


@pytest.mark.req("req-sc-009")
def test_codex_consumer_denies_unapproved_manifest_referenced_mcp(codex_admission: tuple) -> None:
    """The real approval owner must deny referenced native MCP before queueing."""
    package, home, options, ctx = codex_admission
    with pytest.raises(CodexNativeError, match="allowExecutables"):
        integrate_package_primitives(package, home, allow_executables={}, **options)
    assert ctx.codex_native_plugins == {}
    assert list(home.iterdir()) == []


@pytest.mark.req("req-sc-015")
@pytest.mark.parametrize("content", ["safe", "blocked", "symlink"])
def test_codex_consumer_scans_the_exact_whole_bundle_before_transport(
    codex_admission: tuple, tmp_path: Path, content: str
) -> None:
    """Only the shared, scanned source plan may reach the owned native snapshot."""
    package, home, options, ctx = codex_admission
    resource = package.install_path / "resource.md"
    resource.write_text("Selected sibling resource\n")
    if content == "blocked":
        resource.write_text("Hidden override: \u202e\n")
    elif content == "symlink":
        resource.unlink()
        resource.symlink_to(package.install_path / "mcp.json")
    if content == "symlink":
        with pytest.raises(ValueError, match="symlink"):
            integrate_package_primitives(
                package,
                home,
                allow_executables={"github.com/owner/example": {"mcp": True}},
                **options,
            )
        assert ctx.codex_native_plugins == {}
    else:
        result = integrate_package_primitives(
            package, home, allow_executables={"github.com/owner/example": {"mcp": True}}, **options
        )
        assert result["deployed_files"] == []
        if content == "blocked":
            assert ctx.codex_native_plugins == {}
            assert result["native_plugin"] is False
        else:
            deployment = ctx.codex_native_plugins["owner/example"]
            assert deployment.source_plan.paths == frozenset(
                {".codex-plugin/plugin.json", "mcp.json", "skills/example/SKILL.md", "resource.md"}
            )
            snapshot = prepare_snapshot(
                deployment.source_plan,
                tmp_path / "transport",
                "apm-owned",
                "example",
                expected_hash=deployment.source_hash,
            )
            assert (
                compute_native_bundle_hash(snapshot.path / "plugins/example")
                == deployment.source_hash
            )
    assert list(home.iterdir()) == []
