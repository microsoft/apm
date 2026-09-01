"""Bundle-format, audit, projection-boundary, and LF-writer marketplace
analyzers.

Ports the source-admission owner guard plus seven guard-less rules whose owner
is not one of the registry owners above: the generated-bundle LF writer guard,
the removed native Agent Plugin lifecycle tombstone, the local marketplace
audit path resolution, the bundle-format authority
(``check_bundle_format_authority.sh`` subchecks B1-B5, B8, B9, B17, B18), the
Agent Plugin projection/deployment boundary (subcheck B20), the marketplace
source-parsing authority (legacy AC10), and the hash-visible LF writer
contracts (legacy AC34 / ``check_hash_visible_lf_writes.py``).
"""

from __future__ import annotations

import re

from scripts.architecture_linter.checks.agent_plugin_projection import check_projection_boundary
from scripts.architecture_linter.checks.marketplace_integration_shared import (
    GROUP,
    _ast_definition_body,
    _count_calls_named,
    _definition_line,
    _forbid_scan,
    _load,
    _require_subs,
    _src_python,
)
from scripts.architecture_linter.checks.marketplace_legacy import (
    check_bundle_format_authority,
    check_hash_visible_lf_writers,
    check_marketplace_source_parsing,
)
from scripts.architecture_linter.checks.python_semantics import direct_definitions
from scripts.architecture_linter.checks.tree_index import FUNCTION_NODES
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import violation
from scripts.architecture_linter.models import Rule, Violation

_RID_LF = "marketplace-integrations-generated-bundle-lf-writers"


_LF_EXPECTED: tuple[tuple[str, int], ...] = (
    ("src/apm_cli/bundle/agent_plugin_exporter.py", 3),
    ("src/apm_cli/bundle/packer.py", 1),
    ("src/apm_cli/bundle/plugin_exporter.py", 4),
    ("src/apm_cli/core/plugin_manifest.py", 1),
)


def _check_generated_bundle_lf_writers(provider: FactsProvider) -> tuple[Violation, ...]:
    inv = frozenset(provider.inventory)
    findings: list[Violation] = []
    for path, expected in _LF_EXPECTED:
        facts, failures = _load(provider, inv, _RID_LF, path, parse=True)
        if failures:
            findings.extend(failures)
            continue
        if _count_calls_named(facts, "write_text") != 0:
            findings.append(
                violation(
                    _RID_LF, path, "generated bundle metadata must not call raw Path.write_text"
                )
            )
        found = _count_calls_named(facts, "write_text_lf")
        if found != expected:
            findings.append(
                violation(_RID_LF, path, f"expected {expected} write_text_lf calls, found {found}")
            )
    return tuple(findings)


_RID_TOMBSTONE = "marketplace-integrations-removed-plugin-lifecycle"


_REMOVED_PATHS: tuple[str, ...] = (
    "src/apm_cli/install/agent_plugin_runtime.py",
    "src/apm_cli/install/agent_plugin_state.py",
)


_REMOVED_SYMBOLS: tuple[str, ...] = (
    "AgentPluginRootLayout",
    "InstalledPluginComponentFact",
    "InstalledPluginRecord",
    "InstalledPluginRecordCodec",
    "PreparedAgentPluginRoot",
    "PreparedInstalledPluginState",
    "commit_agent_plugin_bundle",
    "discard_staged_agent_plugin_bundle",
    "installed_plugins",
    "materialize_agent_plugin_bundle",
    "prepare_agent_plugin_root",
    "prepare_installed_plugin_state",
    "project_installed_plugin_record",
    "remove_installed_plugin_root",
    "replace_installed_plugins",
    "resolve_agent_plugin_roots",
    "resolve_installed_plugin_record_roots",
    "stable_agent_plugin_id",
    "stage_agent_plugin_bundle",
)


_REMOVED_SYMBOL_RE = re.compile(
    r"\b(" + "|".join(re.escape(sym) for sym in _REMOVED_SYMBOLS) + r")\b"
)


_REMOVED_SCOPED: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("src/apm_cli/bundle/local_bundle.py", ("data_root", "retained_root", "source_identity")),
    ("src/apm_cli/install/local_bundle_handler.py", ("runtime_root",)),
)


def _check_removed_plugin_lifecycle(provider: FactsProvider) -> tuple[Violation, ...]:
    inv = frozenset(provider.inventory)
    findings: list[Violation] = []

    for path in _REMOVED_PATHS:
        if path in inv:
            findings.append(
                violation(
                    _RID_TOMBSTONE,
                    path,
                    "removed native Agent Plugin lifecycle module reintroduced",
                )
            )

    findings.extend(
        _forbid_scan(
            provider,
            inv,
            _RID_TOMBSTONE,
            _src_python(provider),
            _REMOVED_SYMBOL_RE,
            "removed native Agent Plugin lifecycle symbol reintroduced",
            exempt=False,
        )
    )

    for path, tokens in _REMOVED_SCOPED:
        pattern = re.compile(r"\b(" + "|".join(re.escape(token) for token in tokens) + r")\b")
        findings.extend(
            _forbid_scan(
                provider,
                inv,
                _RID_TOMBSTONE,
                (path,),
                pattern,
                "removed native Agent Plugin lifecycle field reintroduced",
                exempt=False,
            )
        )
    return tuple(findings)


_RID_AUDIT = "marketplace-integrations-local-audit-resolution"


_AUDIT = "src/apm_cli/marketplace/audit.py"


_RESOLVER = "src/apm_cli/marketplace/resolver.py"


def _check_local_audit_resolution(provider: FactsProvider) -> tuple[Violation, ...]:
    inv = frozenset(provider.inventory)
    findings: list[Violation] = []

    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_AUDIT,
            _AUDIT,
            ("resolve_local_plugin_path(", 'relative_target="apm.yml"'),
            "Local marketplace audit paths must use resolve_local_plugin_path",
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            _RID_AUDIT,
            (_AUDIT,),
            re.compile(r"_resolve_local_relative_source"),
            "Local audit resolution must not reintroduce _resolve_local_relative_source",
            exempt=False,
        )
    )
    resolver_facts, failures = _load(provider, inv, _RID_AUDIT, _RESOLVER, parse=True)
    if failures:
        findings.extend(failures)
    else:
        index = provider.tree_index(_RESOLVER)
        definitions = (
            ()
            if index is None
            else direct_definitions(
                index,
                "resolve_local_plugin_path",
                kinds=FUNCTION_NODES,
            )
        )
        if len(definitions) != 1:
            findings.append(
                violation(
                    _RID_AUDIT,
                    _RESOLVER,
                    "resolve_local_plugin_path must have exactly one module-level definition, "
                    f"found {len(definitions)}",
                    line=_definition_line(definitions[-1] if definitions else None),
                )
            )
        effective = definitions[-1] if definitions else None
        body = "" if effective is None else _ast_definition_body(resolver_facts, effective)
        if "ensure_path_within(" not in body:
            findings.append(
                violation(
                    _RID_AUDIT,
                    _RESOLVER,
                    "effective resolve_local_plugin_path must contain the "
                    "ensure_path_within containment check",
                    line=_definition_line(effective),
                )
            )
    return tuple(findings)


_RID_BUNDLE_FORMAT = "marketplace-integrations-bundle-format-authority"


def _check_bundle_format_authority(provider: FactsProvider) -> tuple[Violation, ...]:
    """Bundle format owner, frozen seams, streaming archives, and boundary order."""
    return check_bundle_format_authority(provider, _RID_BUNDLE_FORMAT)


_RID_BOUNDARY = "marketplace-integrations-projection-boundary"


def _check_projection_boundary(provider: FactsProvider) -> tuple[Violation, ...]:
    """Native deployment boundary and projection purity across seventeen owners."""
    return check_projection_boundary(provider, _RID_BOUNDARY)


_RID_SOURCE_PARSING = "marketplace-integrations-source-parsing"


def _check_marketplace_source_parsing(provider: FactsProvider) -> tuple[Violation, ...]:
    """Packed sources and check coordinates parse through DependencyReference."""
    return check_marketplace_source_parsing(provider, _RID_SOURCE_PARSING)


_RID_SOURCE_ADMISSION = "marketplace-integrations-source-admission"
_SOURCE_IDENTITY = "src/apm_cli/marketplace/source_identity.py"
_MARKETPLACE_COMMAND = "src/apm_cli/commands/marketplace/__init__.py"
_MARKETPLACE_MODEL = "src/apm_cli/marketplace/models.py"
_MARKETPLACE_CLIENT = "src/apm_cli/marketplace/client.py"


def _check_marketplace_source_admission(
    provider: FactsProvider,
) -> tuple[Violation, ...]:
    """Marketplace consumers must route source identity through one parser."""
    inventory = frozenset(provider.inventory)
    findings: list[Violation] = []
    required = (
        (_SOURCE_IDENTITY, ("def parse_marketplace_source(",)),
        (
            _MARKETPLACE_COMMAND,
            ("identity = parse_marketplace_source(source, host_flag)",),
        ),
        (
            _MARKETPLACE_MODEL,
            ("identity = parse_marketplace_source(self.url)",),
        ),
        (_MARKETPLACE_CLIENT, ("host = source.host",)),
    )
    for path, literals in required:
        findings.extend(
            _require_subs(
                provider,
                inventory,
                _RID_SOURCE_ADMISSION,
                path,
                literals,
                "Marketplace source admission must route through source_identity.py",
            )
        )
    findings.extend(
        _forbid_scan(
            provider,
            inventory,
            _RID_SOURCE_ADMISSION,
            (_MARKETPLACE_CLIENT,),
            re.compile(r"\b_host_from_url\("),
            "Marketplace client must consume the canonical source host",
            exempt=False,
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inventory,
            _RID_SOURCE_ADMISSION,
            (_MARKETPLACE_COMMAND,),
            re.compile(r"SCP_LIKE_RE|AuthResolver\.classify_host|is_valid_fqdn"),
            "Marketplace command must not reimplement source classification",
            exempt=False,
        )
    )
    return tuple(findings)


_RID_HASH_LF = "marketplace-integrations-hash-visible-lf-writers"


def _check_hash_visible_lf_writers(provider: FactsProvider) -> tuple[Violation, ...]:
    """Generated files inside hashed package trees route through write_text_lf."""
    return check_hash_visible_lf_writers(provider, _RID_HASH_LF)


RULES: tuple[Rule, ...] = (
    Rule(
        id=_RID_LF,
        group=GROUP,
        guard_ids=(),
        description="Generated bundle metadata uses deterministic LF writers.",
        check=_check_generated_bundle_lf_writers,
    ),
    Rule(
        id=_RID_TOMBSTONE,
        group=GROUP,
        guard_ids=(),
        description="Removed native Agent Plugin lifecycle state stays removed.",
        check=_check_removed_plugin_lifecycle,
    ),
    Rule(
        id=_RID_AUDIT,
        group=GROUP,
        guard_ids=(),
        description="Local marketplace audit path resolution uses resolve_local_plugin_path.",
        check=_check_local_audit_resolution,
    ),
    Rule(
        id=_RID_BUNDLE_FORMAT,
        group=GROUP,
        guard_ids=(),
        description="Bundle format authority, frozen seams, and native boundary ordering.",
        check=_check_bundle_format_authority,
    ),
    Rule(
        id=_RID_BOUNDARY,
        group=GROUP,
        guard_ids=(),
        description="Agent Plugin projection and native deployment boundary fail closed.",
        check=_check_projection_boundary,
    ),
    Rule(
        id=_RID_SOURCE_PARSING,
        group=GROUP,
        guard_ids=(),
        description="Marketplace source coordinates parse through DependencyReference.",
        check=_check_marketplace_source_parsing,
    ),
    Rule(
        id=_RID_SOURCE_ADMISSION,
        group=GROUP,
        guard_ids=(_RID_SOURCE_ADMISSION,),
        description="Marketplace source admission stays owned by marketplace/source_identity.py.",
        check=_check_marketplace_source_admission,
    ),
    Rule(
        id=_RID_HASH_LF,
        group=GROUP,
        guard_ids=(),
        description="Hash-visible generated files route through canonical LF writers.",
        check=_check_hash_visible_lf_writers,
    ),
)


COLLECTORS: tuple[object, ...] = ()


__all__ = ["COLLECTORS", "RULES"]
