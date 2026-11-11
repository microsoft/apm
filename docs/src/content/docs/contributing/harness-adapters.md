---
title: "Harness adapters"
description: "Extend APM's target catalogue using installed Python harness adapters."
sidebar:
  order: 4
---

APM's built-in harness adapters retain the existing Codex, Claude Code, Copilot and other target profiles, content transformers and MCP clients. A Python distribution can contribute another adapter through the `apm.harnesses` entry-point group without adding a target to APM's source.

This extension is different from an **agent plugin** containing skills, hooks and MCP definitions. A harness adapter is executable Python code installed in APM's own environment. Installing or resolving an `apm.yml` dependency never installs a Python adapter.

## Install and trust

Review an adapter's source and dependencies, then install its pinned Python distribution into the same environment as the Python APM installation. Restart APM after installing, upgrading or removing it. Discovery is process-scoped; catalogue snapshots are consistent for the lifetime of a command.

Only use trusted distributions. Importing an entry point and constructing an adapter executes code with APM's operating-system permissions, including access to project files and the user's environment. Neither APM's package executable approvals nor a harness's sandbox isolates Python adapter code. Do not install adapters from an untrusted project's instructions. A standalone APM binary does not gain access to packages installed in an unrelated Python environment.

Discovery is deterministic. Duplicate target identifiers, aliases, MCP runtime identifiers and invalid descriptors fail rather than override built-ins or silently select a different adapter. A broken installed adapter is an error, not an instruction to skip it and continue with another target. Standalone `apm --version` does not require adapter discovery.

## A complete shared-format adapter

The following provider adds a fictional `example` harness whose documented contract uses `AGENTS.md` and project/user `.example/skills/<name>/SKILL.md`. It illustrates the registration API, not verified support for a real harness.

```toml
[project.entry-points."apm.harnesses"]
example = "example_adapter:adapter"
```

```python
from apm_cli.harnesses import (
    HarnessAdapter,
    PrimitiveMapping,
    TargetCapability,
    TargetProfile,
)


def adapter() -> HarnessAdapter:
    capability = TargetCapability(
        name="example",
        aliases=(),
        description="Example harness skills and AGENTS.md",
        in_all=False,
        explicit_only=True,
        experimental_flag=None,
        mcp_only=False,
        primitive_profile="example",
        compile_family="agents",
        runtimes=(),
        commands=frozenset({"compile", "install", "update"}),
    )
    return HarnessAdapter(
        capability=capability,
        profile=TargetProfile(
            capability=capability,
            root_dir=".example",
            primitives={
                "skills": PrimitiveMapping("skills", "/SKILL.md", "skill_standard"),
            },
            detect_by_dir=False,
            user_supported=True,
        ),
    )
```

Select it with `apm install --target example` or `target: example` in the consumer manifest. `apm compile --target example` uses the existing `agents` compiler family. Root-context compilation can reuse `agents`, `vscode`, `claude` or `gemini`; it is not a registration API for arbitrary compiler families. Use `compile_family=None` for a target with no root-context output.

Keep `explicit_only=True` for targets requiring individual opt-in. Existing `all` expansion, experimental gates and scope resolution still apply. For a target with verified automatic detection, supply `signals=(("dir", ".example"),)` or a documented file marker and choose consistent capability flags. External detection uses these signals, not the built-in profile's `detect_by_dir` switch; explicit-only targets are never auto-detected. Signal paths are relative to the project or user-scope root. Optional `display_signal` describes the marker in `apm targets`.

## Native primitive behavior

`TargetProfile.primitives` selects deployed primitive types and paths. An adapter can reuse existing format IDs and their original integrators. Do not invent a format ID and expect a built-in transformer to understand it.

For a distinct native format, `HarnessAdapter.integrators` maps a primitive name to a no-argument subclass of its existing integrator class. For example, an `agents` override subclasses `AgentIntegrator` and implements its target-driven rendering/deployment behavior. The original integrators remain the fallback for every unoverridden primitive. This deliberately reuses the existing source-plan, result and lifecycle contracts rather than introducing a second deployment engine.

An override must preserve the entire relevant contract, not just produce a file:

- Perform validation and collision checks in preflight before any primitive writes.
- Consume the authorized source plan and scope-resolved target; respect dependency narrowing and executable approvals.
- Return the existing integration result with actual written/adopted paths and counts.
- Implement compatible sync/removal and survivor reintegration; remove only APM-owned content and preserve user edits.
- Honor scratch deployment roots during audit/drift replay. Python adapter code remains trusted; scratch roots are not a security sandbox.
- Skill overrides subclass `SkillIntegrator` and preserve ownership snapshots, materialization receipts, completion and shared-target behavior. Hook overrides preserve native preflight and managed hook reconciliation.

Native whole-agent-plugin registration remains a separate lifecycle. Registering a harness adapter does not qualify native bundle loading, bypass native-plugin admission or authorize decomposition of a whole plugin.

## MCP clients

`HarnessAdapter.mcp_clients` maps runtime identifiers declared by the capability to constructors accepting `project_root` and `user_scope`. They return `MCPClientAdapter` subclasses implementing the existing native configuration/rendering interface. The adapter owns the native schema, configuration paths, atomic writes, scope support and safe handling of malformed files and symlinks.

External MCP clients must additionally implement `remove_managed_servers(names: set[str]) -> set[str]`. The names passed to this method are **native config keys**, verified against durable APM ownership baselines before removal. Preserve unrelated entries and all other settings; raise on unsafe reads or failed writes. APM performs native ownership comparison on install/update too, so an adapter must implement truthful rendering and comparison, not silently adopt matching foreign entries.

Runtime selection, explicit exclusions, executable approvals and scope filtering remain owned by APM. Detection can use the contributed signals or a runtime binary; installing an adapter alone is not a signal that its harness is present. Built-in MCP clients retain their existing cleanup implementations.

## Qualify an adapter

Use synthetic projects and a pinned APM version. Exercise real entry-point discovery and actual CLI install, reinstall/frozen restore, compile, audit/drift, update and uninstall. Check project and supported user scopes, native bytes, recorded paths, a pre-existing foreign file, user-edited managed content, missing dependencies and rejected configurations. Confirm another target's files and unrelated MCP state are unchanged. Tests of a descriptor or copied mock result do not establish native harness compatibility.

Install the real harness separately and demonstrate discovery/loading on a documented client version before claiming support. Keep the adapter installed while restoring, auditing or removing its recorded target. Pin and review adapter upgrades alongside APM; APM's package lockfile does not pin Python distributions. This API exposes APM integrator contracts, so adapter maintainers must qualify against their declared APM dependency range.
