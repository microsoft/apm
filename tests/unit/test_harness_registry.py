from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

import apm_cli.harnesses.registry as registry_module
from apm_cli.harnesses import HarnessAdapter, PrimitiveMapping, TargetCapability, TargetProfile


def _capability(
    name: str = "x-vendor-agent",
    *,
    aliases: tuple[str, ...] = (),
    runtimes: tuple[str, ...] = (),
    primitive_profile: str | None = "x-vendor-agent",
    explicit_only: bool = False,
) -> TargetCapability:
    return TargetCapability(
        name=name,
        aliases=aliases,
        description="Vendor agent integration",
        in_all=False,
        explicit_only=explicit_only,
        experimental_flag=None,
        mcp_only=False,
        primitive_profile=primitive_profile,
        compile_family=None,
        runtimes=runtimes,
        commands=frozenset({"compile", "install", "update"}),
    )


def _adapter(
    *,
    capability: TargetCapability | None = None,
    signals: tuple[tuple[str, str], ...] = (("dir", ".vendor-agent"),),
    integrators=None,
    profile: TargetProfile | None = None,
    mcp_clients=None,
) -> HarnessAdapter:
    cap = capability or _capability()
    selected_profile = profile or TargetProfile(
        capability=cap,
        root_dir=".vendor-agent",
        primitives={"agents": PrimitiveMapping("agents", ".md", "vendor_agent")},
        detect_by_dir=False,
    )
    return HarnessAdapter(
        capability=cap,
        profile=selected_profile,
        signals=signals,
        integrators=integrators or {},
        mcp_clients=mcp_clients or {},
    )


def _entry_point(factory, *, load=None):
    return SimpleNamespace(
        name="vendor_factory",
        value="vendor_plugin:factory",
        dist=SimpleNamespace(name="sample-vendor"),
        load=load or (lambda: factory),
    )


def _isolate_registry(monkeypatch, entry_points):
    monkeypatch.setattr(registry_module, "_REGISTRY", None)
    monkeypatch.setattr(registry_module, "_REGISTRY_ERROR", None)
    monkeypatch.setattr(registry_module, "_iter_entry_points", lambda: entry_points)


def test_plugin_registers_capability_profile_and_detection_signal(tmp_path, monkeypatch):
    adapter = _adapter()
    _isolate_registry(monkeypatch, [_entry_point(lambda: adapter)])

    registry = registry_module.get_harness_registry()
    assert registry.adapters["x-vendor-agent"] is adapter
    assert registry.capabilities["x-vendor-agent"] is adapter.capability
    assert registry.profiles["x-vendor-agent"] is adapter.profile
    assert registry.external_names == frozenset({"x-vendor-agent"})

    (tmp_path / ".vendor-agent").mkdir()
    from apm_cli.integration.targets import active_targets

    assert [profile.name for profile in active_targets(tmp_path)] == ["x-vendor-agent"]


def test_colliding_alias_is_rejected_with_provider_context(monkeypatch):
    adapter = _adapter(
        capability=_capability(aliases=("claude",)),
    )
    _isolate_registry(monkeypatch, [_entry_point(lambda: adapter)])

    with pytest.raises(
        registry_module.HarnessAdapterError, match=r"sample-vendor:vendor_factory.*claude"
    ):
        registry_module.get_harness_registry()


def test_unresolvable_mcp_runtime_is_rejected(monkeypatch):
    runtime = "vendor-runtime-not-an-alias"
    capability = _capability(runtimes=(runtime,))
    adapter = _adapter(
        capability=capability,
        mcp_clients={runtime: lambda: object()},
    )
    _isolate_registry(monkeypatch, [_entry_point(lambda: adapter)])

    with pytest.raises(
        registry_module.HarnessAdapterError, match="canonical name or declared aliases"
    ):
        registry_module.get_harness_registry()


def test_unknown_profile_primitive_and_incomplete_integrator_are_rejected(monkeypatch):
    capability = _capability()
    invalid_profile = TargetProfile(
        capability=capability,
        root_dir=".vendor-agent",
        primitives={"unknown": PrimitiveMapping("x", ".md", "vendor")},
    )
    _isolate_registry(monkeypatch, [_entry_point(lambda: _adapter(profile=invalid_profile))])

    with pytest.raises(registry_module.HarnessAdapterError, match="unknown primitive keys"):
        registry_module.get_harness_registry()

    class IncompleteIntegrator:
        def sync_for_target(self):
            pass

    capability = _capability()
    invalid_integrator = _adapter(
        capability=capability,
        integrators={"agents": IncompleteIntegrator},
    )
    _isolate_registry(monkeypatch, [_entry_point(lambda: invalid_integrator)])

    with pytest.raises(registry_module.HarnessAdapterError, match="integrate_agents_for_target"):
        registry_module.get_harness_registry()


def test_signal_less_explicit_target_is_not_directory_detected(tmp_path, monkeypatch):
    capability = _capability(explicit_only=True)
    profile = TargetProfile(
        capability=capability,
        root_dir=".vendor-agent",
        primitives={"agents": PrimitiveMapping("agents", ".md", "vendor_agent")},
        detect_by_dir=True,
    )
    adapter = _adapter(capability=capability, profile=profile, signals=())
    _isolate_registry(monkeypatch, [_entry_point(lambda: adapter)])
    (tmp_path / profile.root_dir).mkdir()

    from apm_cli.integration.targets import active_targets

    assert all(target.name != capability.name for target in active_targets(tmp_path))
    assert [target.name for target in active_targets(tmp_path, capability.name)] == [
        capability.name
    ]


def test_signal_less_target_must_be_explicit_only(monkeypatch):
    adapter = _adapter(capability=_capability(), signals=())
    _isolate_registry(monkeypatch, [_entry_point(lambda: adapter)])

    with pytest.raises(registry_module.HarnessAdapterError, match="must be explicit-only"):
        registry_module.get_harness_registry()


def test_profile_must_belong_to_adapter_capability(monkeypatch):
    capability = _capability()
    foreign_profile = TargetProfile(
        capability=_capability("x-vendor-other"),
        root_dir=".other",
        primitives={"agents": PrimitiveMapping("agents", ".md", "vendor_agent")},
    )
    adapter = _adapter(capability=capability, profile=foreign_profile)
    _isolate_registry(monkeypatch, [_entry_point(lambda: adapter)])

    with pytest.raises(
        registry_module.HarnessAdapterError, match="profile capability must be its own canonical"
    ):
        registry_module.get_harness_registry()


def test_builtin_mcp_constructors_cover_original_runtime_set(monkeypatch):
    _isolate_registry(monkeypatch, [])

    assert set(registry_module.get_harness_registry().mcp_clients) == {
        "antigravity",
        "claude",
        "codex",
        "copilot",
        "cursor",
        "gemini",
        "hermes",
        "intellij",
        "kiro",
        "opencode",
        "vscode",
        "windsurf",
    }


def test_entrypoint_failure_has_context_and_is_sticky(monkeypatch):
    calls = 0

    def fail_loading():
        nonlocal calls
        calls += 1
        raise ImportError("provider import failed")

    _isolate_registry(monkeypatch, [_entry_point(None, load=fail_loading)])

    with pytest.raises(
        registry_module.HarnessAdapterError,
        match=r"sample-vendor:vendor_factory.*provider import failed",
    ):
        registry_module.get_harness_registry()
    with pytest.raises(
        registry_module.HarnessAdapterError,
        match=r"sample-vendor:vendor_factory.*provider import failed",
    ):
        registry_module.get_harness_registry()
    assert calls == 1


def test_explicit_only_signals_do_not_activate_project_or_user_targets(tmp_path, monkeypatch):
    from apm_cli.integration.targets import active_targets, active_targets_user_scope

    capability = _capability(explicit_only=True)
    adapter = _adapter(capability=capability)
    adapter = replace(adapter, profile=replace(adapter.profile, user_supported=True))
    _isolate_registry(monkeypatch, [_entry_point(lambda: adapter)])
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    (tmp_path / ".vendor-agent").mkdir()
    assert [target.name for target in active_targets(tmp_path)] == ["copilot"]
    assert [target.name for target in active_targets_user_scope()] == ["copilot"]
    assert [target.name for target in active_targets_user_scope("x-vendor-agent")] == [
        "x-vendor-agent"
    ]


def test_mcp_only_adapter_cannot_reference_an_unregistered_profile(monkeypatch):
    from apm_cli.adapters.client.gemini import GeminiClientAdapter

    name = "x-vendor-mcp"
    capability = replace(
        _capability(name=name, explicit_only=True),
        mcp_only=True,
        primitive_profile="missing-profile",
        runtimes=(name,),
    )
    adapter = HarnessAdapter(capability=capability, mcp_clients={name: GeminiClientAdapter})
    _isolate_registry(monkeypatch, [_entry_point(lambda: adapter)])
    with pytest.raises(registry_module.HarnessAdapterError, match="unknown primitive profile"):
        registry_module.get_harness_registry()
