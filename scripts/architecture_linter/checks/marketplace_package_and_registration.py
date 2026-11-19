"""Package-construction, projection, skill-membership, and registration
marketplace analyzers.

Ports marketplace-package decisions plus the local-bundle layout lowering
guard recorded in ``.apm/architecture/owners/install-deployment.json``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from scripts.architecture_linter.checks.marketplace_integration_shared import (
    _PLUGIN_PARSER,
    _PROJECTION,
    _VALIDATION,
    GROUP,
    _count_checks,
    _count_re,
    _def_body_text,
    _forbid_scan,
    _load,
    _require_subs,
    _src_python,
    _subdir_python,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import checked_facts, violation
from scripts.architecture_linter.models import Rule, Violation

_AGENT_PLUGINS_PREFIX = "src/apm_cli/agent_plugins/"


_COPILOT_PLUGINS_PREFIX = "src/apm_cli/copilot_plugins/"


def _count_across(
    provider: FactsProvider,
    inv: frozenset[str],
    rule_id: str,
    paths: Iterable[str],
    pattern: re.Pattern[str],
) -> int:
    """Total lines matching ``pattern`` across in-inventory ``paths``."""
    total = 0
    for path in paths:
        if path not in inv:
            continue
        facts, failures = checked_facts(provider, path, rule_id, require_python=True)
        if failures:
            continue
        total += _count_re(facts, pattern)
    return total


_RID_CONSTRUCTION = "marketplace-integrations-package-construction"


_APM_PACKAGE = "src/apm_cli/models/apm_package.py"


def _check_package_construction(provider: FactsProvider) -> tuple[Violation, ...]:
    inv = frozenset(provider.inventory)
    findings: list[Violation] = []

    findings.extend(
        _count_checks(
            provider,
            inv,
            _RID_CONSTRUCTION,
            _APM_PACKAGE,
            (("re", r"^    def from_mapping\(", 1, "eq"),),
            "apm_package must own a single APMPackage.from_mapping",
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_CONSTRUCTION,
            _APM_PACKAGE,
            ("result = cls.from_mapping(",),
            "APMPackage.from_apm_yml must route interpreted construction through from_mapping",
        )
    )
    return tuple(findings)


_RID_PROJECTION = "marketplace-integrations-package-projection"


_PROJECTION_DUP = re.compile(r"^def project_agent_plugin_package\(")


_NORMALIZE_CALLER = re.compile(r"normalize_plugin_directory\(")


_NORMALIZE_DEF = re.compile(r"def normalize_plugin_directory\(")


def _check_package_projection(provider: FactsProvider) -> tuple[Violation, ...]:
    inv = frozenset(provider.inventory)
    findings: list[Violation] = []

    findings.extend(
        _count_checks(
            provider,
            inv,
            _RID_PROJECTION,
            _PROJECTION,
            (("re", r"^def project_agent_plugin_package\(", 1, "eq"),),
            "projection.py must own a single project_agent_plugin_package",
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            _RID_PROJECTION,
            _src_python(provider, exclude={_PROJECTION}),
            _PROJECTION_DUP,
            "Agent Plugin projection must stay owned by agent_plugins/projection.py",
            exempt=False,
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            _RID_PROJECTION,
            (_PROJECTION,),
            re.compile(r"read_json_document|json\.load|yaml\."),
            "projection must consume the frozen IR, not re-read raw documents",
            exempt=False,
        )
    )

    # Validation routes interpreted packages through the projection owner.
    validation_facts, failures = _load(provider, inv, _RID_PROJECTION, _VALIDATION, parse=True)
    if failures:
        findings.extend(failures)
    else:
        body = _def_body_text(validation_facts, "_validate_agent_plugin")
        for needle in (
            "package = project_agent_plugin_package(plugin, create_config=create_config)",
            "result.package = package",
        ):
            if needle not in body:
                findings.append(
                    violation(
                        _RID_PROJECTION,
                        _VALIDATION,
                        f"Agent Plugin validation must route through the projection owner; missing {needle!r}",
                    )
                )

    # No raw APMPackage construction inside the agent_plugins package.
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            _RID_PROJECTION,
            _subdir_python(provider, _AGENT_PLUGINS_PREFIX),
            re.compile(r"APMPackage\("),
            "Agent Plugin compatibility packages must be built by the projection owner",
            exempt=False,
        )
    )

    # Directory normalization may not be re-invoked outside its owner and reuses.
    findings.extend(_scan_normalization_callers(provider, inv))
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_PROJECTION,
            "src/apm_cli/install/drift.py",
            (
                "native_detection = route_agent_plugin_package(install_path)",
                "agent_plugin_detection=native_detection,\n            read_only=True",
                "with native_registration_scope(targets):",
                'if result.get("native_plugin") and not result.get("native_claude_plugin"):',
                "from apm_cli.install.native_plugin_admission import verify_native_plugin_cache",
                "verify_native_plugin_cache(\n                            lock_dep,",
            ),
            "Native replay must use canonical read-only projection, admission and integrity",
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_PROJECTION,
            "src/apm_cli/install/native_plugin_admission.py",
            (
                "expected_hash = compute_package_hash(replay_path, framed=lock_dep.content_hash_framed)",
                "candidate, expected_hash, carried, framed=lock_dep.content_hash_framed",
            ),
            "Native cache verification must hash the canonical replay image",
        )
    )
    return tuple(findings)


def _scan_normalization_callers(
    provider: FactsProvider, inv: frozenset[str]
) -> tuple[Violation, ...]:
    findings: list[Violation] = []
    allowed_any = {_VALIDATION, "src/apm_cli/install/drift.py"}
    for path in _src_python(provider):
        facts, failures = checked_facts(provider, path, _RID_PROJECTION, require_python=True)
        if failures:
            findings.extend(failures)
            continue
        if path in allowed_any:
            continue
        for number, line in enumerate(facts.lines, start=1):
            if not _NORMALIZE_CALLER.search(line):
                continue
            if path == _PLUGIN_PARSER and _NORMALIZE_DEF.search(line):
                continue
            findings.append(
                violation(
                    _RID_PROJECTION,
                    path,
                    "normalize_plugin_directory must stay owned by plugin_parser and its reuses",
                    line=number,
                )
            )
    return tuple(findings)


_RID_SKILL = "marketplace-integrations-legacy-skill-membership"


_SKILL_INTEGRATOR = "src/apm_cli/integration/skill_integrator.py"


_SKILL_SUBSET_LEXICAL = re.compile(
    r"def _skill_subset_name_filter|set\(dep\.skill_subset\)|Path\(normalized_path\)\.name"
)


def _check_legacy_skill_membership(provider: FactsProvider) -> tuple[Violation, ...]:
    inv = frozenset(provider.inventory)
    findings: list[Violation] = []

    findings.extend(
        _count_checks(
            provider,
            inv,
            _RID_SKILL,
            _PLUGIN_PARSER,
            (
                ("re", r"^def normalized_plugin_skill_sources\(", 1, "eq"),
                ("re", r"^def _map_plugin_artifacts\(", 1, "eq"),
            ),
            "plugin_parser must own skill membership and artifact mapping",
        )
    )

    integrator_facts, failures = _load(provider, inv, _RID_SKILL, _SKILL_INTEGRATOR, parse=True)
    if failures:
        findings.extend(failures)
    else:
        body = _def_body_text(integrator_facts, "skill_source_paths")
        if "normalized_plugin_skill_sources(package_path)" not in body:
            findings.append(
                violation(
                    _RID_SKILL,
                    _SKILL_INTEGRATOR,
                    "skill routing must consume plugin_parser.normalized_plugin_skill_sources",
                )
            )
        if 'package_path / "skills"' in body or "manifest.get(" in body:
            findings.append(
                violation(
                    _RID_SKILL,
                    _SKILL_INTEGRATOR,
                    "skill routing must not re-derive membership from the plugin directory or manifest",
                )
            )

    findings.extend(
        _forbid_scan(
            provider,
            inv,
            _RID_SKILL,
            (_SKILL_INTEGRATOR, "src/apm_cli/bundle/plugin_exporter.py"),
            _SKILL_SUBSET_LEXICAL,
            "Skill subset filter tokens must come from models/dependency/subsets.py",
            exempt=True,
        )
    )
    return tuple(findings)


_RID_BUNDLE_LAYOUT = "install-deployment-bundle-native-layout"


_PLUGIN_LAYOUT = "src/apm_cli/bundle/plugin_layout.py"
_LOCAL_BUNDLE_PATHS = "src/apm_cli/install/local_bundle_paths.py"
_INSTALL_SERVICES = "src/apm_cli/install/services.py"
_TARGET_NAME_COMPARISON = re.compile(r"\btarget\.name\s*(?:==|!=)|(?:==|!=)\s*target\.name\b")


def _check_bundle_native_layout(provider: FactsProvider) -> tuple[Violation, ...]:
    """Local-bundle layout lowering must stay target-profile driven."""
    inv = frozenset(provider.inventory)
    findings: list[Violation] = []
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_BUNDLE_LAYOUT,
            _PLUGIN_LAYOUT,
            (
                "class PluginDirSpec:",
                "PLUGIN_LAYOUT: dict[str, PluginDirSpec]",
                '"commands": PluginDirSpec(',
                '("commands", "prompts")',
                "plugin_command_prompt_name",
            ),
            "plugin_layout must own plugin-native directory and filename lowering data",
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_BUNDLE_LAYOUT,
            _LOCAL_BUNDLE_PATHS,
            (
                "def _lower_to_target(",
                "PLUGIN_LAYOUT.get(head)",
                "for primitive_kind in spec.primitive_kinds:",
                "_retarget_basename(parts[-1], mapping, spec.apm_basename_fn)",
                '_UNSUPPORTED_PLUGIN_FORMAT_IDS = frozenset({"gemini_command", "codex_agent"})',
            ),
            "local bundle deployment must route through PLUGIN_LAYOUT and TargetProfile.primitives",
        )
    )
    for path in (_LOCAL_BUNDLE_PATHS, _INSTALL_SERVICES):
        facts, failures = checked_facts(provider, path, _RID_BUNDLE_LAYOUT, require_python=True)
        if failures:
            findings.extend(failures)
            continue
        for number, line in enumerate(facts.lines, start=1):
            if _TARGET_NAME_COMPARISON.search(line):
                findings.append(
                    violation(
                        _RID_BUNDLE_LAYOUT,
                        path,
                        "Local bundle lowering must not branch on target.name; use TargetProfile.primitives",
                        line=number,
                    )
                )
    return tuple(findings)


_RID_NATIVE = "marketplace-integrations-native-registration"


_NATIVE_CAPABILITY_DEF = re.compile(r"^def resolve_native_registration_capability\(")


_NATIVE_BINARY_COUPLING = re.compile(
    r"COPILOT_LIVE_PLUGIN_MIN_VERSION|probe_copilot_cli_version|APM_COPILOT_CLI_VERSION"
    r"|find_runtime_binary|is_qualified_client_version|normalize_client_version"
    r"|minimum_client_version|undetected_client_reason|unqualified_client_reason"
    r"|AgentPluginClientUnavailableError|SemVer|parse_semver"
    r"|subprocess\.(run|Popen|check_output|check_call)|shutil\.which"
)


def _native_coupling_paths(provider: FactsProvider) -> tuple[str, ...]:
    paths: list[str] = list(_subdir_python(provider, _COPILOT_PLUGINS_PREFIX))
    paths.extend(_subdir_python(provider, "src/apm_cli/commands/uninstall/"))
    paths.extend(
        (
            "src/apm_cli/install/phases/copilot_plugins.py",
            "src/apm_cli/install/template.py",
            "src/apm_cli/commands/prune.py",
        )
    )
    return tuple(paths)


def _check_native_registration(provider: FactsProvider) -> tuple[Violation, ...]:
    inv = frozenset(provider.inventory)
    findings: list[Violation] = []

    count = _count_across(provider, inv, _RID_NATIVE, _src_python(provider), _NATIVE_CAPABILITY_DEF)
    if count != 1:
        findings.append(
            violation(
                _RID_NATIVE,
                "src/apm_cli/copilot_plugins/capability.py",
                f"resolve_native_registration_capability must be defined exactly once, found {count}",
            )
        )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_NATIVE,
            "src/apm_cli/agent_plugins/errors.py",
            ("from apm_cli.copilot_plugins.capability import current_native_registration",),
            "native registration state must be read through copilot_plugins.capability",
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            _RID_NATIVE,
            _native_coupling_paths(provider),
            _NATIVE_BINARY_COUPLING,
            "Native Copilot plugin registration must not couple to client binary/version probing",
            exempt=False,
        )
    )
    return tuple(findings)


_RID_COPILOT = "marketplace-integrations-copilot-ownership"


_SYNC_DEF = re.compile(r"^def synchronize_copilot_plugins\(")


_SETTINGS_KEYS = re.compile(r"extraKnownMarketplaces|enabledPlugins")


def _check_copilot_ownership(provider: FactsProvider) -> tuple[Violation, ...]:
    inv = frozenset(provider.inventory)
    findings: list[Violation] = []

    count = _count_across(provider, inv, _RID_COPILOT, _src_python(provider), _SYNC_DEF)
    if count != 1:
        findings.append(
            violation(
                _RID_COPILOT,
                "src/apm_cli/copilot_plugins/registrar.py",
                f"synchronize_copilot_plugins must be defined exactly once, found {count}",
            )
        )
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            _RID_COPILOT,
            tuple(
                p
                for p in _src_python(provider)
                if not p.startswith(_COPILOT_PLUGINS_PREFIX)
                and p != "src/apm_cli/native_plugins/settings.py"
            ),
            _SETTINGS_KEYS,
            "Native settings keys must live only in copilot_plugins/ or the shared "
            "native_plugins/settings.py writer",
            exempt=False,
        )
    )
    return tuple(findings)


_RID_CLAUDE = "marketplace-integrations-claude-ownership"
_CLAUDE_OWNER = "src/apm_cli/claude_plugins/registrar.py"


def _check_claude_ownership(provider: FactsProvider) -> tuple[Violation, ...]:
    inv = frozenset(provider.inventory)
    findings: list[Violation] = []
    for path, symbol in (
        (_CLAUDE_OWNER, "reconcile_claude_plugins"),
        (_CLAUDE_OWNER, "verify_claude_native_cache"),
        ("src/apm_cli/claude_plugins/admission.py", "plan_native_claude_plugin"),
    ):
        count = _count_across(
            provider,
            inv,
            _RID_CLAUDE,
            _src_python(provider),
            re.compile(rf"^def {symbol}\("),
        )
        if count != 1:
            findings.append(
                violation(
                    _RID_CLAUDE,
                    path,
                    f"{symbol} must have exactly one owner, found {count}",
                )
            )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_CLAUDE,
            _CLAUDE_OWNER,
            (
                "None if previous is None else frozenset(prior_names),",
                "following = native_ownership(lockfile) if lockfile is not None else {}",
                "if fingerprint_native_claude_source(source) != own.record.fingerprint:",
                "and prior.pin == own.pin",
                "name not in prior_names\n            and existing.exists()",
                "write_declaration(",
            ),
            "Claude declarations must derive ownership from the lockfile and verify approved bytes",
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_CLAUDE,
            "src/apm_cli/native_plugins/settings.py",
            (
                "if enabled.get(key) is True:",
                "if current is not None and current != declaration.marketplace_value:",
                "for plugin in sorted((owned or frozenset()) - declaration.plugins):",
            ),
            "Native settings writes must keep explicit user false and unowned marketplaces",
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_CLAUDE,
            "src/apm_cli/install/phases/claude_plugins.py",
            ("previous.write(lock_path)",),
            "A failed Claude declaration must restore the previous lock (one rollback unit)",
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            _RID_CLAUDE,
            tuple(
                p
                for p in _src_python(provider)
                if p.startswith(("src/apm_cli/claude_plugins/", "src/apm_cli/native_plugins/"))
            ),
            re.compile(r"^\s*(import subprocess|from subprocess import)|shutil\.which\("),
            "Native Claude declaration never runs a vendor CLI",
            exempt=False,
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_CLAUDE,
            "src/apm_cli/claude_plugins/admission.py",
            ("return compute_native_plugin_hash(root)",),
            "Claude fingerprinting must consume the canonical complete native hash",
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_CLAUDE,
            "src/apm_cli/install/drift.py",
            (
                'if result.get("native_plugin") and not result.get("native_claude_plugin"):',
                'if result.get("native_claude_plugin"):',
                "verify_claude_native_cache(",
                'local_modules_dir=scratch_root / "apm_modules",',
            ),
            "Claude replay must verify owned fingerprints without normalizing into the live cache",
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            _RID_CLAUDE,
            tuple(
                p for p in _src_python(provider) if not p.startswith("src/apm_cli/claude_plugins/")
            ),
            re.compile(r'["\']apm-claude["\']|^class ClaudePluginCLI'),
            "Claude native marketplace and CLI ownership must stay in claude_plugins/",
            exempt=False,
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_CLAUDE,
            "src/apm_cli/install/deployment_plan.py",
            (
                "plan_native_claude_plugin(",
                "native_plugin_root=native_plugin.source_root if native_plugin else None,",
                "return [target for target in targets if target.name not in native_target_names]",
                "claude_native=native_claude_plugin",
            ),
            "Native Claude admission and projection must share the batch deployment plan",
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_CLAUDE,
            "src/apm_cli/integration/mcp_config_view.py",
            (
                "elif _is_local_claude_plugin_source(dependency, package_dir):",
                "if not package_dir.is_dir() or manifest_file(package_dir).exists():",
            ),
            "Local Claude MCP inspection must read the install-synthesized manifest, "
            "never the writable source",
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_CLAUDE,
            "src/apm_cli/install/services.py",
            (
                "native_claude_plugin = deployment_plan.claude_native",
                "projection_targets = deployment_plan.projection_targets",
                'result["native_claude_plugin"] = True',
                "if _target not in projection_targets:",
                "targets=projection_targets,",
                "_check_native_claude_transition(\n            reconcile_package_targets,",
                "dry_run=True,",
                'if reconcile_stats.get("errors") or reconcile_stats.get("files_removed"):',
            ),
            "Native Claude routing must use shared admission/source scan and suppress loose projection",
        )
    )
    return tuple(findings)


_RID_CODEX = "marketplace-integrations-codex-split-install"
_CODEX_PREFIX = "src/apm_cli/codex_plugins/"


def _check_codex_split_install(provider: FactsProvider) -> tuple[Violation, ...]:
    """Codex is split-installed and never spawned; legacy retirement has one owner."""
    inv = frozenset(provider.inventory)
    findings: list[Violation] = []
    for name, owner in (
        ("note_codex_split_install", "src/apm_cli/install/target_warnings.py"),
        ("split_install_message", "src/apm_cli/install/phases/codex_plugins.py"),
        ("retire_legacy_codex_registrations", _CODEX_PREFIX + "legacy_registrations.py"),
        ("retire_plugin_registrations", "src/apm_cli/adapters/client/codex.py"),
    ):
        count = _count_across(
            provider,
            inv,
            _RID_CODEX,
            _src_python(provider),
            re.compile(rf"^\s*def {name}\("),
        )
        if count != 1:
            findings.append(violation(_RID_CODEX, owner, f"{name} must have exactly one owner"))
    required = {
        "src/apm_cli/install/target_warnings.py": (
            "note_codex_split_install(package_info, targets, ctx)",
            "PackageType.MARKETPLACE_PLUGIN",
        ),
        "src/apm_cli/install/phases/codex_plugins.py": (
            "split_install_message(ctx.codex_split_plugins, ctx.scope)",
            "retire_legacy_codex_registrations(ctx.apm_modules_dir, ctx.scope)",
            "criterion A3",
        ),
        "src/apm_cli/commands/uninstall/cli.py": (
            "retire_legacy_codex_registrations(modules_dir, scope)",
        ),
        _CODEX_PREFIX + "legacy_registrations.py": (
            "adapter.retire_plugin_registrations(registrations)",
            "safe_rmtree(path, snapshot_root)",
        ),
        "src/apm_cli/adapters/client/codex.py": ('plain(entry) != {"enabled": False}',),
    }
    for path, fragments in required.items():
        findings.extend(
            _require_subs(
                provider,
                inv,
                _RID_CODEX,
                path,
                fragments,
                "Codex split install and legacy retirement must keep their canonical owners",
            )
        )
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            _RID_CODEX,
            _src_python(provider),
            re.compile(r"""["']app-server["']|which\(\s*["']codex["']\s*\)"""),
            "APM must never locate or spawn the codex binary (#3920)",
            exempt=False,
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            _RID_CODEX,
            _subdir_python(provider, _CODEX_PREFIX),
            re.compile(r"plugins/cache|plugin_registry\.json|subprocess"),
            "Codex retirement must not address private native state or run a process",
            exempt=False,
        )
    )
    return tuple(findings)


RULES: tuple[Rule, ...] = (
    Rule(
        id=_RID_CONSTRUCTION,
        group=GROUP,
        guard_ids=(_RID_CONSTRUCTION,),
        description="APMPackage interpreted-manifest construction stays owned by from_mapping.",
        check=_check_package_construction,
    ),
    Rule(
        id=_RID_PROJECTION,
        group=GROUP,
        guard_ids=(_RID_PROJECTION,),
        description="Agent Plugin compatibility projection stays owned by agent_plugins/projection.py.",
        check=_check_package_projection,
    ),
    Rule(
        id=_RID_SKILL,
        group=GROUP,
        guard_ids=(_RID_SKILL,),
        description="Legacy plugin skill membership stays owned by deps/plugin_parser.py.",
        check=_check_legacy_skill_membership,
    ),
    Rule(
        id=_RID_BUNDLE_LAYOUT,
        group=GROUP,
        guard_ids=(_RID_BUNDLE_LAYOUT,),
        description="Local bundle layout lowering stays owned by bundle/plugin_layout.py.",
        check=_check_bundle_native_layout,
    ),
    Rule(
        id=_RID_NATIVE,
        group=GROUP,
        guard_ids=(_RID_NATIVE,),
        description="Native Agent Plugin registration admission stays free of client binary coupling.",
        check=_check_native_registration,
    ),
    Rule(
        id=_RID_CODEX,
        group=GROUP,
        guard_ids=(_RID_CODEX,),
        description="Codex is split-installed, never spawned, and legacy registrations retire once.",
        check=_check_codex_split_install,
    ),
    Rule(
        id=_RID_COPILOT,
        group=GROUP,
        guard_ids=(_RID_COPILOT,),
        description="APM-owned Copilot marketplace catalog and settings stay in copilot_plugins/.",
        check=_check_copilot_ownership,
    ),
    Rule(
        id=_RID_CLAUDE,
        group=GROUP,
        guard_ids=(_RID_CLAUDE,),
        description="Native Claude registration has one owner and never auto-enables plugins.",
        check=_check_claude_ownership,
    ),
)


COLLECTORS: tuple[object, ...] = ()


__all__ = ["COLLECTORS", "RULES"]
