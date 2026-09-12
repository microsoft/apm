"""Tag-pattern, version, output-path, metadata, and diagnostics analyzers.

Ports five canonical owner decisions recorded in
``.apm/architecture/owners/marketplace-plugins.json``. RULES assembly lives
in the thin catalog module :mod:`marketplace_integration_analyzers`, which
imports each check function below.
"""

from __future__ import annotations

import re

from scripts.architecture_linter.checks.marketplace_integration_shared import (
    GROUP,
    _ast_definition_body,
    _count_checks,
    _definition_line,
    _forbid_scan,
    _load,
    _require_res,
    _require_subs,
    _src_python,
    _subdir_python,
)
from scripts.architecture_linter.checks.python_semantics import direct_definitions
from scripts.architecture_linter.checks.tree_index import FUNCTION_NODES
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import violation
from scripts.architecture_linter.models import FileFacts, Rule, Violation

_MARKETPLACE_PREFIX = "src/apm_cli/marketplace/"


def _window_after(facts: FileFacts, anchor: str, following: int) -> str | None:
    """Return the anchor line plus ``following`` lines (grep -A), else ``None``."""
    for index, line in enumerate(facts.lines):
        if anchor in line:
            return "\n".join(facts.lines[index : index + 1 + following])
    return None


_RID_TAG = "marketplace-integrations-tag-pattern"


_TAG_OWNER = "src/apm_cli/marketplace/tag_pattern.py"


_TAG_PARALLEL = re.compile(
    r"""["']\{version\}["']\s+(not\s+)?in\s+(pattern|tag_pattern)"""
    r"""|\.(count)\(["']\{version\}["']\)"""
)


def _check_tag_pattern(provider: FactsProvider) -> tuple[Violation, ...]:
    inv = frozenset(provider.inventory)
    findings: list[Violation] = []

    findings.extend(
        _require_res(
            provider,
            inv,
            _RID_TAG,
            _TAG_OWNER,
            (re.compile(r"^def validate_tag_pattern\("),),
            "tag_pattern.py must own validate_tag_pattern",
        )
    )
    findings.extend(
        _window_check(
            provider,
            inv,
            _RID_TAG,
            "src/apm_cli/marketplace/yml_schema.py",
            "def _validate_tag_pattern(",
            8,
            "validate_tag_pattern(pattern, context=context)",
            "yml_schema tag-pattern validation must call the canonical validator",
        )
    )
    findings.extend(
        _window_check(
            provider,
            inv,
            _RID_TAG,
            "src/apm_cli/marketplace/models.py",
            'raw_tp = source.get("tag_pattern")',
            12,
            "tag_pattern = validate_tag_pattern(",
            "marketplace models must validate tag patterns through the owner",
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_TAG,
            "src/apm_cli/marketplace/version_resolver.py",
            ("tag_pattern = validate_tag_pattern(tag_pattern)",),
            "version_resolver must validate tag patterns through the owner",
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            _RID_TAG,
            tuple(p for p in _subdir_python(provider, _MARKETPLACE_PREFIX) if p != _TAG_OWNER),
            _TAG_PARALLEL,
            "Marketplace tag patterns must route through marketplace/tag_pattern.py",
            exempt=True,
        )
    )
    return tuple(findings)


def _window_check(
    provider: FactsProvider,
    inv: frozenset[str],
    rule_id: str,
    path: str,
    anchor: str,
    following: int,
    needle: str,
    message: str,
) -> tuple[Violation, ...]:
    facts, failures = _load(provider, inv, rule_id, path, parse=False)
    if failures:
        return failures
    window = _window_after(facts, anchor, following)
    if window is None or needle not in window:
        return (violation(rule_id, path, f"{message}; expected {needle!r} near {anchor!r}"),)
    return ()


_RID_VERSION = "marketplace-integrations-version-precedence"


_VERSION_OWNER = "src/apm_cli/marketplace/version_check.py"


_VERSION_DUP = re.compile(r"^\s*def _read_(local|plugin).*version\(")


def _check_version_precedence(provider: FactsProvider) -> tuple[Violation, ...]:
    inv = frozenset(provider.inventory)
    findings: list[Violation] = []

    findings.extend(
        _count_checks(
            provider,
            inv,
            _RID_VERSION,
            _VERSION_OWNER,
            (("re", r"^def _read_local_version\(|^def _read_plugin_json_version\(", 2, "eq"),),
            "version_check must own _read_local_version and _read_plugin_json_version",
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_VERSION,
            _VERSION_OWNER,
            (
                "return _read_plugin_json_version(package_root)",
                "plugin_json = find_plugin_json(package_root)",
            ),
            "Local marketplace package versions must route through version_check.py",
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            _RID_VERSION,
            tuple(p for p in _subdir_python(provider, _MARKETPLACE_PREFIX) if p != _VERSION_OWNER),
            _VERSION_DUP,
            "Local package-version readers must stay owned by marketplace/version_check.py",
            exempt=True,
        )
    )
    return tuple(findings)


_RID_OUTPUT = "marketplace-integrations-output-path"


_OUTPUT_OWNER = "src/apm_cli/marketplace/output_profiles.py"


_OUTPUT_CONSUMERS: tuple[str, ...] = (
    "src/apm_cli/core/build_orchestrator.py",
    "src/apm_cli/marketplace/builder.py",
    "src/apm_cli/marketplace/drift_check.py",
)


_OUTPUT_PARALLEL = re.compile(
    r"project_root\s*/\s*config\.[A-Za-z_]+\.output"
    r"|getattr\(config,\s*profile\.config_attr"
    r"|config\.output_specs"
)


def _check_output_path(provider: FactsProvider) -> tuple[Violation, ...]:
    inv = frozenset(provider.inventory)
    findings: list[Violation] = []

    findings.extend(
        _count_checks(
            provider,
            inv,
            _RID_OUTPUT,
            _OUTPUT_OWNER,
            (("re", r"^def resolve_effective_output_path\(", 1, "eq"),),
            "output_profiles must own resolve_effective_output_path",
        )
    )
    owner_facts, failures = _load(provider, inv, _RID_OUTPUT, _OUTPUT_OWNER, parse=True)
    if failures:
        findings.extend(failures)
    else:
        index = provider.tree_index(_OUTPUT_OWNER)
        definitions = (
            ()
            if index is None
            else direct_definitions(
                index,
                "resolve_effective_output_path",
                kinds=FUNCTION_NODES,
            )
        )
        if len(definitions) != 1:
            findings.append(
                violation(
                    _RID_OUTPUT,
                    _OUTPUT_OWNER,
                    "resolve_effective_output_path must have exactly one module-level definition, "
                    f"found {len(definitions)}",
                    line=_definition_line(definitions[-1] if definitions else None),
                )
            )
        effective = definitions[-1] if definitions else None
        body = "" if effective is None else _ast_definition_body(owner_facts, effective)
        missing = tuple(
            needle
            for needle in (
                "output_path = Path(configured_path)",
                "ensure_path_within(output_path, project_root)",
                "return output_path",
            )
            if needle not in body
        )
        if missing:
            findings.append(
                violation(
                    _RID_OUTPUT,
                    _OUTPUT_OWNER,
                    "effective resolve_effective_output_path definition lost path containment "
                    f"or return semantics; missing: {', '.join(repr(item) for item in missing)}",
                    line=_definition_line(effective),
                )
            )
    for consumer in _OUTPUT_CONSUMERS:
        findings.extend(
            _require_subs(
                provider,
                inv,
                _RID_OUTPUT,
                consumer,
                ("resolve_effective_output_path(",),
                "Marketplace output paths must route through marketplace/output_profiles.py",
            )
        )
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            _RID_OUTPUT,
            _OUTPUT_CONSUMERS,
            _OUTPUT_PARALLEL,
            "Marketplace consumers must not re-derive output paths from config",
            exempt=False,
        )
    )
    return tuple(findings)


_RID_METADATA = "marketplace-integrations-metadata-enrichment"
_METADATA_OWNER = "src/apm_cli/marketplace/builder.py"
_METADATA_DRIFT = "src/apm_cli/marketplace/drift_check.py"
_METADATA_PARALLEL = re.compile(
    r"^class MetadataEnrichment(?:Outcome|Result)(?:\(|:)"
    r"|^\s*def _prefetch_metadata\("
)


def _check_metadata_enrichment(provider: FactsProvider) -> tuple[Violation, ...]:
    inv = frozenset(provider.inventory)
    findings: list[Violation] = []

    findings.extend(
        _count_checks(
            provider,
            inv,
            _RID_METADATA,
            _METADATA_OWNER,
            (
                ("re", r"^class MetadataEnrichmentOutcome(?:\(|:)", 1, "eq"),
                ("re", r"^class MetadataEnrichmentResult\(", 1, "eq"),
                ("re", r"^\s+def _prefetch_metadata\(", 1, "eq"),
                ("re", r"^\s+def remote_metadata_for_profile\(", 1, "eq"),
            ),
            "marketplace/builder.py must own metadata enrichment and certification",
            parse=True,
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_METADATA,
            _METADATA_DRIFT,
            ("not remote_metadata.certifiable",),
            "drift checks must consume builder-owned metadata certification",
            parse=True,
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            _RID_METADATA,
            _src_python(provider, exclude=(_METADATA_OWNER,)),
            _METADATA_PARALLEL,
            "Marketplace metadata certifiability must remain owned by marketplace/builder.py",
            exempt=False,
        )
    )
    return tuple(findings)


_RID_RAW = "marketplace-integrations-raw-diagnostics"


_RAW_OWNER = "src/apm_cli/marketplace/models.py"


_RAW_VALIDATOR = "src/apm_cli/marketplace/validator.py"


_RAW_PARALLEL = re.compile(r"structural_errors(\s*:[^=]+)?\s*=")


def _check_raw_diagnostics(provider: FactsProvider) -> tuple[Violation, ...]:
    inv = frozenset(provider.inventory)
    findings: list[Violation] = []

    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_RAW,
            _RAW_OWNER,
            (
                "structural_errors: tuple[str, ...] = ()",
                'structural_errors.append("plugins: expected a list")',
            ),
            "marketplace/models.py must originate raw structural diagnostics",
        )
    )
    findings.extend(
        _require_res(
            provider,
            inv,
            _RID_RAW,
            _RAW_VALIDATOR,
            (re.compile(r"^def validate_marketplace_structure\("),),
            "validator must own validate_marketplace_structure",
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_RAW,
            _RAW_VALIDATOR,
            ("errors=list(manifest.structural_errors)",),
            "validator must consume the model's structural diagnostics",
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            _RID_RAW,
            tuple(p for p in _subdir_python(provider, _MARKETPLACE_PREFIX) if p != _RAW_OWNER),
            _RAW_PARALLEL,
            "Marketplace structural diagnostics must originate in marketplace/models.py",
            exempt=False,
        )
    )
    return tuple(findings)


RULES: tuple[Rule, ...] = (
    Rule(
        id=_RID_TAG,
        group=GROUP,
        guard_ids=(_RID_TAG,),
        description="Marketplace tag-pattern validation stays owned by marketplace/tag_pattern.py.",
        check=_check_tag_pattern,
    ),
    Rule(
        id=_RID_VERSION,
        group=GROUP,
        guard_ids=(_RID_VERSION,),
        description="Local package-version precedence stays owned by marketplace/version_check.py.",
        check=_check_version_precedence,
    ),
    Rule(
        id=_RID_OUTPUT,
        group=GROUP,
        guard_ids=(_RID_OUTPUT,),
        description="Effective marketplace output path stays owned by marketplace/output_profiles.py.",
        check=_check_output_path,
    ),
    Rule(
        id=_RID_METADATA,
        group=GROUP,
        guard_ids=(_RID_METADATA,),
        description="Marketplace metadata certifiability stays owned by marketplace/builder.py.",
        check=_check_metadata_enrichment,
    ),
    Rule(
        id=_RID_RAW,
        group=GROUP,
        guard_ids=(_RID_RAW,),
        description="Marketplace raw-structure diagnostics originate in marketplace/models.py.",
        check=_check_raw_diagnostics,
    ),
)


COLLECTORS: tuple[object, ...] = ()


__all__ = ["COLLECTORS", "RULES"]
