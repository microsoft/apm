"""Registry-completeness guard for ``KNOWN_TARGETS``.

When a new target is added (or an existing one gains a primitive), the
subsystems that depend on per-target metadata -- pack-time file filtering,
MCP conflict detection, compile family routing, and install hooks display --
must all be updated.  Historically each of those lived in a module-local
dict or ``if/elif`` chain, and adding a target meant updating N files.

This file turns "forgot to wire up new target" from a silent runtime bug
into a hard CI failure.  Each test is parametrised over ``KNOWN_TARGETS``
so that failures pinpoint the exact entry that drifted.
"""

from __future__ import annotations

import pytest

from apm_cli.adapters.client.base import MCPClientAdapter
from apm_cli.adapters.client.claude import ClaudeClientAdapter
from apm_cli.adapters.client.codex import CodexClientAdapter
from apm_cli.adapters.client.copilot import CopilotClientAdapter
from apm_cli.adapters.client.cursor import CursorClientAdapter
from apm_cli.adapters.client.gemini import GeminiClientAdapter
from apm_cli.adapters.client.opencode import OpenCodeClientAdapter
from apm_cli.adapters.client.vscode import VSCodeClientAdapter
from apm_cli.adapters.client.windsurf import WindsurfClientAdapter
from apm_cli.integration.targets import KNOWN_TARGETS, TargetProfile

# Recognised values for ``TargetProfile.compile_family``.  Adding a new family
# requires touching ``apm_cli.commands.compile.cli._resolve_compile_target``
# AND this set.  Any other value would make the compile router silently
# misroute the target.
_KNOWN_COMPILE_FAMILIES = {"vscode", "claude", "gemini", "agents"}

# Recognised values for ``MCPClientAdapter.mcp_servers_key``.  Adding a new
# key means a new MCP config schema; ``MCPConflictDetector`` must learn how
# to parse it (today only ``mcp_servers`` needs the codex-style flattened-
# key fallback -- the others are plain top-level dicts).
_KNOWN_MCP_KEYS = {"mcpServers", "mcp_servers", "servers"}

# Adapter target_names that are MCP-only pseudo-targets (no entry in
# KNOWN_TARGETS).  Code that joins adapter -> profile must tolerate misses
# for these.
_MCP_ONLY_ADAPTER_NAMES = {"vscode"}

# All adapter subclasses that ship in the repo.  The ``target_name`` on each
# must round-trip to a ``KNOWN_TARGETS`` entry so ``MCPConflictDetector``
# can resolve config metadata without sniffing class names.
_ADAPTER_CLASSES = (
    CopilotClientAdapter,
    ClaudeClientAdapter,
    CursorClientAdapter,
    CodexClientAdapter,
    GeminiClientAdapter,
    OpenCodeClientAdapter,
    VSCodeClientAdapter,
    WindsurfClientAdapter,
)


@pytest.mark.parametrize("name,profile", sorted(KNOWN_TARGETS.items()))
def test_pack_prefixes_are_resolvable(name: str, profile: TargetProfile) -> None:
    """Every target must yield a non-empty pack-prefix tuple.

    ``effective_pack_prefixes`` falls back to ``(profile.prefix,)`` when
    ``pack_prefixes`` is empty, so this test fails only when both the
    field AND the fallback are degenerate.
    """
    prefixes = profile.effective_pack_prefixes
    assert prefixes, f"target {name!r} has no pack prefixes"
    for p in prefixes:
        assert p.endswith("/"), (
            f"target {name!r} pack prefix {p!r} must end with '/' so startswith() filtering works"
        )
        assert "\\" not in p, (
            f"target {name!r} pack prefix {p!r} contains a backslash; "
            "pack prefixes are POSIX-style and must use forward slashes"
        )


@pytest.mark.parametrize("name,profile", sorted(KNOWN_TARGETS.items()))
def test_compile_family_is_recognised(name: str, profile: TargetProfile) -> None:
    """A target's ``compile_family`` must be ``None`` or a recognised family.

    ``None`` means the target produces no compile output (e.g. agent-skills,
    copilot-cowork).  Any other value is routed by
    ``_resolve_compile_target`` and must be in ``_KNOWN_COMPILE_FAMILIES``;
    otherwise the router would silently fall through.
    """
    if profile.compile_family is None:
        return
    assert profile.compile_family in _KNOWN_COMPILE_FAMILIES, (
        f"target {name!r} declares unknown compile_family "
        f"{profile.compile_family!r}; expected one of {_KNOWN_COMPILE_FAMILIES}"
    )


@pytest.mark.parametrize("adapter_cls", _ADAPTER_CLASSES, ids=lambda c: c.__name__)
def test_adapter_mcp_servers_key_is_recognised(
    adapter_cls: type[MCPClientAdapter],
) -> None:
    """Every shipped adapter must declare a known ``mcp_servers_key``.

    The conflict detector reads this directly off the adapter to pull
    existing servers out of the on-disk config.  An empty value would
    silently make ``get_existing_server_configs`` return ``{}``.
    """
    key = adapter_cls.mcp_servers_key
    assert key, (
        f"{adapter_cls.__name__} does not override mcp_servers_key; "
        "MCPConflictDetector would silently return no existing servers"
    )
    assert key in _KNOWN_MCP_KEYS, (
        f"{adapter_cls.__name__} declares mcp_servers_key={key!r}; "
        f"expected one of {_KNOWN_MCP_KEYS}"
    )


@pytest.mark.parametrize("name,profile", sorted(KNOWN_TARGETS.items()))
def test_hooks_display_matches_root(name: str, profile: TargetProfile) -> None:
    """When a target sets ``hooks_config_display``, it must live under its root.

    Catches typos such as ``.codex/hooks.json`` on the windsurf entry.
    Targets without ``hooks_config_display`` fall back to the generic
    ``"{root}/{subdir}/"`` install-log formula and are exempt.
    """
    if profile.hooks_config_display is None:
        return
    display = profile.hooks_config_display
    assert display.startswith(profile.prefix) or display.startswith(profile.root_dir), (
        f"target {name!r} hooks_config_display {display!r} must live under "
        f"its root_dir {profile.root_dir!r}"
    )


def test_every_target_with_hooks_primitive_has_explicit_or_generic_display() -> None:
    """Every target whose primitives include 'hooks' has a coherent display path.

    Either an explicit ``hooks_config_display`` (Claude/Cursor/Codex/Windsurf
    style: hooks land in a single config file) OR a primitive mapping with
    a non-empty ``subdir`` so the generic ``{root}/{subdir}/`` formula is
    not degenerate.
    """
    offenders: list[str] = []
    for name, profile in KNOWN_TARGETS.items():
        hooks_pm = profile.primitives.get("hooks")
        if hooks_pm is None:
            continue
        if profile.hooks_config_display is not None:
            continue
        # No explicit display -- the generic formula must not be degenerate
        if not hooks_pm.subdir:
            offenders.append(
                f"{name}: hooks subdir is empty AND hooks_config_display is "
                f"None; install log will print just {profile.prefix!r}"
            )
    assert not offenders, "\n".join(offenders)


@pytest.mark.parametrize("adapter_cls", _ADAPTER_CLASSES, ids=lambda c: c.__name__)
def test_adapter_target_name_is_set(adapter_cls: type[MCPClientAdapter]) -> None:
    """Every shipped adapter must declare a non-empty ``target_name``.

    The base class provides ``target_name = ""`` purely as a typing default;
    every concrete subclass must override it so
    ``MCPConflictDetector`` can resolve per-target config metadata.
    """
    assert adapter_cls.target_name, (
        f"{adapter_cls.__name__} does not override target_name; "
        "MCPConflictDetector cannot route its config without it"
    )


@pytest.mark.parametrize("adapter_cls", _ADAPTER_CLASSES, ids=lambda c: c.__name__)
def test_adapter_target_name_resolves_to_known_target(
    adapter_cls: type[MCPClientAdapter],
) -> None:
    """Each adapter's ``target_name`` must map to a ``KNOWN_TARGETS`` entry,
    except for documented MCP-only pseudo-targets (``vscode``).

    This prevents Gap 2-style silent breakage when a new adapter is added:
    target-aware code that joins on ``adapter.target_name -> profile`` will
    raise here if the registry entry is missing.
    """
    name = adapter_cls.target_name
    if name in _MCP_ONLY_ADAPTER_NAMES:
        return
    assert name in KNOWN_TARGETS, (
        f"{adapter_cls.__name__} declares target_name={name!r} but no such "
        f"entry exists in KNOWN_TARGETS (and {name!r} is not in the documented "
        f"MCP-only allowlist {_MCP_ONLY_ADAPTER_NAMES})"
    )


def test_client_factory_supported_clients_matches_adapter_set() -> None:
    """``ClientFactory.supported_clients()`` must enumerate exactly the
    adapter classes registered in ``_MCP_CLIENT_REGISTRY`` and exposed
    through the ``MCPClientAdapter`` subclass list.

    Closes the N+1 site at ``mcp_integrator.py`` runtime loops:
    callers iterate this set instead of hand-maintaining parallel lists.
    A missing adapter here means a freshly-added MCP target would be
    silently skipped by cleanup loops and availability probes.
    """
    from apm_cli.factory import ClientFactory

    supported = ClientFactory.supported_clients()
    expected = {cls.target_name for cls in _ADAPTER_CLASSES}
    assert supported == expected, (
        f"ClientFactory.supported_clients() drift: registered={expected}, "
        f"factory={supported}.  Update _MCP_CLIENT_REGISTRY in factory.py."
    )
