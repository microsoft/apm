"""Catalog-manifest and Agent-Plugin-contract marketplace analyzers.

Ports ``marketplace-integrations-catalog-manifest`` (``deps/_shared.py``
catalog-only manifest materialization) and
``marketplace-integrations-agent-plugin-contract`` (Agent Plugins v1
contract, component IR, and portable-manifest authority).
"""

from __future__ import annotations

import ast
import re

from scripts.architecture_linter.checks.marketplace_integration_shared import (
    _PLUGIN_PARSER,
    _PROJECTION,
    _VALIDATION,
    GROUP,
    _count_checks,
    _def_body_text,
    _definition,
    _definition_line,
    _forbid_scan,
    _load,
    _require_res,
    _require_subs,
    _src_python,
)
from scripts.architecture_linter.checks.python_semantics import (
    assignments_to,
    binding_nodes,
    direct_definitions,
    dotted_name,
    import_bound_name,
    is_statically_dead,
)
from scripts.architecture_linter.checks.tree_index import FUNCTION_NODES, TreeIndex
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import checked_facts, source_text, violation
from scripts.architecture_linter.models import FileFacts, Rule, Violation

_RID_CATALOG = "marketplace-integrations-catalog-manifest"


_CATALOG_OWNER = "src/apm_cli/deps/_shared.py"


def _check_catalog_manifest(provider: FactsProvider) -> tuple[Violation, ...]:
    inv = frozenset(provider.inventory)
    findings: list[Violation] = []

    findings.extend(
        _require_res(
            provider,
            inv,
            _RID_CATALOG,
            _CATALOG_OWNER,
            (re.compile(r"^def materialize_marketplace_manifest\("),),
            "deps/_shared.py must own materialize_marketplace_manifest",
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_CATALOG,
            "src/apm_cli/deps/apm_resolver.py",
            (
                "from ._shared import MarketplaceManifestMaterializationError, "
                "materialize_marketplace_manifest",
                "materialize_marketplace_manifest(dep_ref, install_path)",
            ),
            "apm_resolver must materialize catalog manifests through the owner",
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_CATALOG,
            "src/apm_cli/install/phases/local_content.py",
            ("has_marketplace_deployable_manifest(dep_ref)",),
            "local content phase must query the owner's deployable-manifest gate",
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_CATALOG,
            "src/apm_cli/install/sources.py",
            ("materialize_marketplace_manifest(dep_ref, install_path)",),
            "install sources must materialize catalog manifests through the owner",
        )
    )
    findings.extend(
        _require_res(
            provider,
            inv,
            _RID_CATALOG,
            _PLUGIN_PARSER,
            (re.compile(r"^def resolve_plugin_root_placeholders\("),),
            "plugin_parser must own resolve_plugin_root_placeholders",
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_CATALOG,
            "src/apm_cli/models/apm_package.py",
            ("resolve_plugin_root_placeholders(",),
            "apm_package must expand plugin-root placeholders through the owner",
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            _RID_CATALOG,
            _src_python(
                provider,
                exclude={
                    _CATALOG_OWNER,
                    "src/apm_cli/deps/apm_resolver.py",
                    "src/apm_cli/install/phases/local_content.py",
                    "src/apm_cli/install/sources.py",
                    "src/apm_cli/marketplace/models.py",
                    "src/apm_cli/models/dependency/reference.py",
                },
            ),
            re.compile(r"marketplace_manifest"),
            "Catalog-only marketplace manifests must route through deps/_shared.py",
            exempt=False,
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            _RID_CATALOG,
            _src_python(provider, exclude={_CATALOG_OWNER, _PLUGIN_PARSER}),
            re.compile(r"synthesize_apm_yml_from_plugin\("),
            "Plugin apm.yml synthesis must stay owned by deps/_shared.py and plugin_parser.py",
            exempt=False,
        )
    )
    return tuple(findings)


_RID_CONTRACT = "marketplace-integrations-agent-plugin-contract"
_RID_FORMAT_PRECEDENCE = "marketplace-integrations-package-format-precedence"


_LOADER = "src/apm_cli/agent_plugins/loader.py"


_IR = "src/apm_cli/agent_plugins/ir.py"


_ASSETS = "src/apm_cli/agent_plugins/assets.py"


_LOCAL_BUNDLE = "src/apm_cli/bundle/local_bundle.py"


_FORMAT_DETECTION = "src/apm_cli/models/format_detection.py"


_SCAN_METHODS: frozenset[str] = frozenset({"glob", "iterdir", "read_bytes", "read_text", "rglob"})


_AGENT_PLUGIN_DUP = re.compile(r"^(class AgentPlugin:|def (detect|load)_agent_plugin\()")


_DETECT_LOAD_DEF = re.compile(r"def (detect|load)_agent_plugin\(")


_INGRESS_MINIMUMS: tuple[tuple[str, int], ...] = (
    ("src/apm_cli/install/sources.py", 4),
    ("src/apm_cli/install/template.py", 2),
    ("src/apm_cli/deps/apm_resolver.py", 2),
    ("src/apm_cli/deps/_shared.py", 2),
    ("src/apm_cli/deps/github_downloader.py", 2),
    ("src/apm_cli/deps/registry/resolver.py", 3),
)


def _canonical_hashlib_import(index: TreeIndex) -> tuple[ast.Import, ...]:
    """Return module-scope ``import hashlib`` statements with no alias."""
    return tuple(
        node
        for node in index.module_children()
        if isinstance(node, ast.Import)
        and any(alias.name == "hashlib" and alias.asname is None for alias in node.names)
    )


def _digest_assignment_is_sha256(index: TreeIndex, function: ast.AST) -> bool:
    """Require one live ``digest = hashlib.sha256()`` in the owning function."""
    assignments = assignments_to(index, function, "digest")
    if len(assignments) != 1 or is_statically_dead(index, assignments[0].node):
        return False
    value = assignments[0].value
    return (
        isinstance(value, ast.Call)
        and dotted_name(value.func) == "hashlib.sha256"
        and not value.args
        and not value.keywords
    )


def _check_asset_digest_contract(
    provider: FactsProvider,
    inv: frozenset[str],
) -> tuple[Violation, ...]:
    """Pin hashlib provenance and both inventory/verification SHA-256 sites."""
    _facts, failures = _load(provider, inv, _RID_CONTRACT, _ASSETS, parse=True)
    if failures:
        return failures
    index = provider.tree_index(_ASSETS)
    if index is None:
        return (
            violation(
                _RID_CONTRACT,
                _ASSETS,
                "asset digest source could not be inspected",
            ),
        )

    findings: list[Violation] = []
    canonical_imports = _canonical_hashlib_import(index)
    all_hashlib_imports = tuple(
        node
        for node in index.nodes
        if isinstance(node, (ast.Import, ast.ImportFrom))
        and any(import_bound_name(alias) == "hashlib" for alias in node.names)
    )
    if len(canonical_imports) != 1 or all_hashlib_imports != canonical_imports:
        findings.append(
            violation(
                _RID_CONTRACT,
                _ASSETS,
                "asset digests must use the module-scope standard-library import 'import hashlib'",
                line=_definition_line((all_hashlib_imports or canonical_imports or (None,))[-1]),
            )
        )

    canonical_ids = {id(node) for node in canonical_imports}
    rebindings = tuple(
        node for node in binding_nodes(index, "hashlib") if id(node) not in canonical_ids
    )
    setattr_rebindings = tuple(
        node
        for node in index.nodes
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "setattr"
        and len(node.args) >= 2
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "hashlib"
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value == "sha256"
    )
    if rebindings or setattr_rebindings:
        offender = (*rebindings, *setattr_rebindings)[0]
        findings.append(
            violation(
                _RID_CONTRACT,
                _ASSETS,
                "hashlib or hashlib.sha256 must not be rebound or shadowed",
                line=_definition_line(offender),
            )
        )

    asset_classes = direct_definitions(index, "AssetInventory", kinds=(ast.ClassDef,))
    if len(asset_classes) != 1:
        findings.append(
            violation(
                _RID_CONTRACT,
                _ASSETS,
                f"AssetInventory must be defined exactly once, found {len(asset_classes)}",
                line=_definition_line(asset_classes[-1] if asset_classes else None),
            )
        )
    asset_class = asset_classes[-1] if asset_classes else None
    inventory_methods = (
        ()
        if asset_class is None
        else direct_definitions(
            index,
            "_inventory_regular_file",
            parent=asset_class,
            kinds=FUNCTION_NODES,
        )
    )
    verification_functions = direct_definitions(
        index,
        "_open_verified_asset",
        kinds=FUNCTION_NODES,
    )
    for label, definitions in (
        ("AssetInventory._inventory_regular_file", inventory_methods),
        ("_open_verified_asset", verification_functions),
    ):
        if len(definitions) != 1:
            findings.append(
                violation(
                    _RID_CONTRACT,
                    _ASSETS,
                    f"{label} must be defined exactly once, found {len(definitions)}",
                    line=_definition_line(definitions[-1] if definitions else None),
                )
            )
        effective = definitions[-1] if definitions else None
        if effective is None or not _digest_assignment_is_sha256(index, effective):
            findings.append(
                violation(
                    _RID_CONTRACT,
                    _ASSETS,
                    f"{label} must initialize its load-bearing digest with hashlib.sha256()",
                    line=_definition_line(effective),
                )
            )
    return tuple(findings)


def _class_ann_fields(facts: FileFacts, class_name: str) -> tuple[str, ...] | None:
    if _definition(facts, class_name) is None:
        return None
    return tuple(
        assignment.target
        for assignment in facts.assignments
        if assignment.scope == class_name and assignment.kind == "ann_assign"
    )


def _check_agent_plugin_contract(provider: FactsProvider) -> tuple[Violation, ...]:
    inv = frozenset(provider.inventory)
    findings: list[Violation] = []

    findings.extend(_check_component_ir(provider, inv))
    findings.extend(_check_loader_ownership(provider, inv))
    return tuple(findings)


def _check_component_ir(provider: FactsProvider, inv: frozenset[str]) -> tuple[Violation, ...]:
    findings: list[Violation] = []

    ir_facts, failures = _load(provider, inv, _RID_CONTRACT, _IR, parse=True)
    if failures:
        findings.extend(failures)
    else:
        components = _class_ann_fields(ir_facts, "AgentPluginComponents")
        if components != ("skills", "mcp_servers"):
            findings.append(
                violation(
                    _RID_CONTRACT,
                    _IR,
                    f"portable AgentPluginComponents fields changed: {components}",
                )
            )
        asset = _class_ann_fields(ir_facts, "AgentPluginAsset")
        if asset != ("path", "source", "sha256", "size", "executable_mode"):
            findings.append(
                violation(_RID_CONTRACT, _IR, f"asset integrity facts changed: {asset}")
            )

    assets_facts, failures = _load(provider, inv, _RID_CONTRACT, _ASSETS, parse=True)
    if failures:
        findings.extend(failures)
    else:
        assets_text = source_text(assets_facts)
        required_assets = (
            "if stat.S_ISLNK",
            "ensure_path_within",
            "self._reserve_bytes(len(chunk))",
            "for entry in directory.iterdir():",
            "self._reserve_entry()\n                entries.append(entry)",
            "cached = self._assets.get(relative)",
            "ensure_path_within_resolved(path, self._root)",
            "ensure_path_within_resolved(path, root)",
            "cached_payload = self._payloads.get(relative)",
        )
        for needle in required_assets:
            if needle not in assets_text:
                findings.append(
                    violation(
                        _RID_CONTRACT,
                        _ASSETS,
                        f"asset integrity contract changed; missing {needle!r}",
                    )
                )
        forbidden_assets = (
            "entry_count = self._entry_count",
            "set(self._assets)",
            "sorted(directory.iterdir()",
        )
        for needle in forbidden_assets:
            if needle in assets_text:
                findings.append(
                    violation(
                        _RID_CONTRACT,
                        _ASSETS,
                        f"asset inventory work may be refunded or quadratic: {needle!r}",
                    )
                )

    findings.extend(_check_asset_digest_contract(provider, inv))

    loader_facts, failures = _load(provider, inv, _RID_CONTRACT, _LOADER, parse=True)
    if failures:
        findings.extend(failures)
    else:
        loader_text = source_text(loader_facts)
        for needle in (
            "root_entries = asset_inventory.list_component_candidates(root)",
            "primary.disposition is _CandidateDisposition.ABSENT",
            "disposition=_CandidateDisposition.REJECTED",
        ):
            if needle not in loader_text:
                findings.append(
                    violation(
                        _RID_CONTRACT,
                        _LOADER,
                        f"component candidate contract changed; missing {needle!r}",
                    )
                )
        if _calls_in_scope_match(loader_facts, "_has_exact_entry", _SCAN_METHODS):
            findings.append(
                violation(
                    _RID_CONTRACT,
                    _LOADER,
                    "component candidates may be rescanned outside the work budget",
                )
            )

    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_CONTRACT,
            "src/apm_cli/hook_contract.py",
            ("HOOK_COMMAND_KEYS: tuple[str, ...]",),
            "neutral hook command grammar must stay owned by hook_contract",
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            _RID_CONTRACT,
            ("src/apm_cli/integration/hook_ir.py",),
            re.compile(r"HOOK_COMMAND_KEYS: tuple\[str, \.\.\.\]"),
            "hook command grammar must not be redefined outside hook_contract",
            exempt=False,
        )
    )

    projection_facts, failures = _load(provider, inv, _RID_CONTRACT, _PROJECTION, parse=True)
    if failures:
        findings.extend(failures)
    elif _any_call_terminal(projection_facts, _SCAN_METHODS):
        findings.append(
            violation(
                _RID_CONTRACT,
                _PROJECTION,
                "projection rescans source files instead of consuming IR",
            )
        )

    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_CONTRACT,
            _VALIDATION,
            (
                "agent_plugin_detection: AgentPluginDetection | None = None",
                "pkg_type, plugin_json_path = detect_package_type(",
                "agent_plugin_detection=native_detection",
                "result.agent_plugin = plugin",
                "detection.manifest_path.parent.resolve() != package_root",
            ),
            "validation must reuse detection while routing precedence through the planner",
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_CONTRACT,
            "src/apm_cli/install/sources.py",
            ("agent_plugin_detection=native_detection",),
            "install sources must reuse the native agent-plugin detection",
        )
    )
    return tuple(findings)


def _check_loader_ownership(provider: FactsProvider, inv: frozenset[str]) -> tuple[Violation, ...]:
    findings: list[Violation] = []

    findings.extend(
        _require_res(
            provider,
            inv,
            _RID_CONTRACT,
            _LOCAL_BUNDLE,
            (
                re.compile(r"^class PluginSchemaRoute\(Enum\):"),
                re.compile(r"^def classify_plugin_manifest_schema\("),
                re.compile(r"^def route_agent_plugin_package\("),
            ),
            "bundle/local_bundle.py must own plugin schema routing",
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_CONTRACT,
            _LOCAL_BUNDLE,
            (
                "if schema_id == PLUGIN_SCHEMA_ID:",
                "package_type, _ = detect_package_type(",
                "if package_type != PackageType.AGENT_PLUGIN:",
            ),
            "plugin schema routing must select exact schema IDs",
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            _RID_CONTRACT,
            (_LOCAL_BUNDLE,),
            re.compile(
                r"is_agent_plugin_schema_id|supports_plugin_schema_id|validate_plugin_manifest_document"
            ),
            "plugin schema routing must not use permissive schema predicates",
            exempt=False,
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            _RID_CONTRACT,
            (_LOCAL_BUNDLE,),
            re.compile(
                r"agent_plugin_(runtime|state)|install\.mcp|security\.executables|lockfile.*v3"
            ),
            "plugin schema routing must not depend on deployment or runtime state",
            exempt=False,
        )
    )
    findings.extend(
        _count_checks(
            provider,
            inv,
            _RID_CONTRACT,
            _LOADER,
            (("sub", "classify_plugin_manifest_schema", 4, "ge"),),
            "Agent Plugin loading and legacy admission must share the schema router",
        )
    )
    findings.extend(
        _require_res(
            provider,
            inv,
            _RID_CONTRACT,
            _LOADER,
            (
                re.compile(r"^def detect_agent_plugin\("),
                re.compile(r"^def load_agent_plugin\("),
                re.compile(r"^def _read_admissible_root_manifest\("),
                re.compile(r"^def _load_apm_configuration\("),
            ),
            "loader must own admissibility, detection, loading, and manifest authority",
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_CONTRACT,
            _LOADER,
            ("read_json_document(manifest_path, reject_duplicate_schema=True)",),
            "loader must read the root manifest with duplicate-schema rejection",
        )
    )
    findings.extend(_scan_agent_plugin_duplicates(provider, inv))

    validation_facts, failures = _load(provider, inv, _RID_CONTRACT, _VALIDATION, parse=True)
    if failures:
        findings.extend(failures)
    else:
        body = _def_body_text(validation_facts, "_validate_agent_plugin")
        if re.search(r"normalize_plugin_directory|synthesize_apm_yml_from_plugin", body):
            findings.append(
                violation(
                    _RID_CONTRACT,
                    _VALIDATION,
                    "Agent Plugin classification must route through the loader, not Claude normalization",
                )
            )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_CONTRACT,
            _FORMAT_DETECTION,
            ("detect_agent_plugin(package_path)", "admit_legacy_plugin_manifest(package_path)"),
            "format detection must route plugin admission through the loader",
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_CONTRACT,
            _PLUGIN_PARSER,
            (
                "admit_legacy_plugin_manifest(plugin_path)",
                "classify_plugin_manifest_schema(manifest)",
            ),
            "legacy parser must admit and classify plugin manifests through the router",
        )
    )
    for path, minimum in _INGRESS_MINIMUMS:
        findings.extend(
            _count_checks(
                provider,
                inv,
                _RID_CONTRACT,
                path,
                (("sub", "route_agent_plugin_package", minimum, "ge"),),
                "Package ingress must converge through route_agent_plugin_package",
            )
        )
    return tuple(findings)


def _check_package_format_precedence(provider: FactsProvider) -> tuple[Violation, ...]:
    inv = frozenset(provider.inventory)
    findings: list[Violation] = []
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_FORMAT_PRECEDENCE,
            _FORMAT_DETECTION,
            ("class NormalizationPlanner:", "if has_eligible_apm_yml:"),
            "NormalizationPlanner must own eligible-manifest precedence",
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_FORMAT_PRECEDENCE,
            _VALIDATION,
            (
                "pkg_type, plugin_json_path = detect_package_type(",
                "agent_plugin_detection=native_detection",
            ),
            "validation must delegate package-format precedence to the planner",
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_FORMAT_PRECEDENCE,
            _LOCAL_BUNDLE,
            (
                "package_type, _ = detect_package_type(",
                "if package_type != PackageType.AGENT_PLUGIN:",
            ),
            "Agent Plugin ingress must delegate package-format precedence to the planner",
        )
    )
    return tuple(findings)


def _calls_in_scope_match(facts: FileFacts, scope: str, terminals: frozenset[str]) -> bool:
    return any(
        call.scope == scope
        and call.qualname.rsplit(".", 1)[-1] in terminals
        and "." in call.qualname
        for call in facts.calls
    )


def _any_call_terminal(facts: FileFacts, terminals: frozenset[str]) -> bool:
    return any(
        call.qualname.rsplit(".", 1)[-1] in terminals and "." in call.qualname
        for call in facts.calls
    )


def _scan_agent_plugin_duplicates(
    provider: FactsProvider, inv: frozenset[str]
) -> tuple[Violation, ...]:
    findings: list[Violation] = []
    for path in _src_python(provider):
        facts, failures = checked_facts(provider, path, _RID_CONTRACT, require_python=True)
        if failures:
            findings.extend(failures)
            continue
        for number, line in enumerate(facts.lines, start=1):
            if not _AGENT_PLUGIN_DUP.search(line):
                continue
            if path == _LOADER and _DETECT_LOAD_DEF.search(line):
                continue
            if path == _IR and "class AgentPlugin:" in line:
                continue
            findings.append(
                violation(
                    _RID_CONTRACT,
                    path,
                    "Agent Plugin interpretation must live in agent_plugins/loader.py and ir.py",
                    line=number,
                )
            )
    return tuple(findings)


RULES: tuple[Rule, ...] = (
    Rule(
        id=_RID_CATALOG,
        group=GROUP,
        guard_ids=(_RID_CATALOG,),
        description="Catalog-only marketplace manifests materialize through deps/_shared.py.",
        check=_check_catalog_manifest,
    ),
    Rule(
        id=_RID_CONTRACT,
        group=GROUP,
        guard_ids=(_RID_CONTRACT,),
        description="Agent Plugins v1 contract and component IR stay owned by agent_plugins/loader.py.",
        check=_check_agent_plugin_contract,
    ),
    Rule(
        id=_RID_FORMAT_PRECEDENCE,
        group=GROUP,
        guard_ids=(_RID_FORMAT_PRECEDENCE,),
        description="Package ingress delegates format precedence to NormalizationPlanner.",
        check=_check_package_format_precedence,
    ),
)


COLLECTORS: tuple[object, ...] = ()


__all__ = ["COLLECTORS", "RULES"]
