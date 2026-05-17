from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True, slots=True)
class MCPInvokeParams:
    mcp_name: str
    transport: str | None = None
    url: str | None = None
    env_pairs: tuple = ()
    header_pairs: tuple = ()
    mcp_version: str | None = None
    command_argv: tuple = ()
    dev: bool = False
    force: bool = False
    runtime: str | None = None
    exclude: str | None = None
    verbose: bool = False
    dry_run: bool = False
    no_policy: bool = False
    validated_registry_url: str | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class InstallDependencyParams:
    apm_package: object
    update_refs: bool = False
    verbose: bool = False
    only_packages: list | None = None
    force: bool = False
    parallel_downloads: int = 4
    logger: object = None
    scope: object = None
    auth_resolver: object = None
    target: str | None = None
    allow_insecure: bool = False
    allow_insecure_hosts: tuple = ()
    marketplace_provenance: dict | None = None
    protocol_pref: object = None
    allow_protocol_fallback: bool | None = None
    no_policy: bool = False
    skill_subset: tuple | None = None
    skill_subset_from_cli: bool = False
    legacy_skill_paths: bool = False
    frozen: bool = False
    plan_callback: object = None
