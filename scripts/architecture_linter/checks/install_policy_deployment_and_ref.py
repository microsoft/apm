"""Deployment-frame, ref-recheck, queue-dedup, and registry-intent install
policy analyzers.

Ports five guard-less semantic rules (AC4 declared intent / AC7 mutation
locking).
"""

from __future__ import annotations

import re

from scripts.architecture_linter.checks.install_policy_shared import (
    _APM_RESOLVER,
    _DEPS_LOCKFILE,
    _TESTS_TREE,
    _after_context,
    _banned,
    _configured,
    _first_line,
    _has_re,
    _has_text,
    _matches,
    _report,
    _tree_python_paths,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.models import Violation

RULE_DEPLOYMENT_FRAME = "install-deployment-deployment-frame-projection"


RULE_REF_RECHECK = "install-deployment-ref-recheck-ownership"


RULE_QUEUE_DEDUP = "install-deployment-resolver-queue-dedup"


RULE_LOCAL_ANCHOR = "install-deployment-local-identity-anchor"


RULE_REGISTRY_INTENT = "install-deployment-registry-dependency-intent"


RULE_MCP_REGISTRY_RESOLUTION = "install-deployment-mcp-registry-resolution"


_LINK_RESOLVER = "src/apm_cli/compilation/link_resolver.py"


_APM_CLI_TREE = "src/apm_cli/"


_DEPLOYMENT_FRAME_OWNERS = (
    "src/apm_cli/models/apm_package.py",
    "src/apm_cli/integration/base_integrator.py",
    _LINK_RESOLVER,
    "src/apm_cli/install/drift.py",
)


_DEPLOYMENT_PACKAGE_ROOT = re.compile(r"deployment_package_root")


_DEPLOYMENT_FRAME_PROJECTION = (
    "candidate_in_deployment = ctx.deployment_package_root / package_relative"
)


def check_deployment_frame_projection(provider: FactsProvider) -> tuple[Violation, ...]:
    """Dependency deployment-frame mapping belongs to UnifiedLinkResolver."""
    rule_id = RULE_DEPLOYMENT_FRAME
    findings = _banned(
        provider,
        rule_id=rule_id,
        paths=_tree_python_paths(provider, _APM_CLI_TREE, excluded=_DEPLOYMENT_FRAME_OWNERS),
        pattern=_DEPLOYMENT_PACKAGE_ROOT,
        message=(
            "Dependency deployment-frame mapping belongs to UnifiedLinkResolver; "
            "deployment_package_root must not be re-derived here"
        ),
        configured=False,
        respect_exempt=True,
    )
    resolver, resolver_fail = _configured(provider, _LINK_RESOLVER, rule_id)
    if resolver_fail:
        findings.extend(resolver_fail)
        return tuple(findings)
    if not _has_text(resolver, _DEPLOYMENT_FRAME_PROJECTION):
        findings.append(
            _report(
                rule_id,
                _LINK_RESOLVER,
                "UnifiedLinkResolver must project source assets into the deployment "
                f"frame; missing: {_DEPLOYMENT_FRAME_PROJECTION}",
            )
        )
    return tuple(findings)


_REF_RECHECK_OWNER = "src/apm_cli/drift.py"


_RESOLVE_PHASE = "src/apm_cli/install/phases/resolve.py"


_REF_RECHECK_CONSUMERS = (_APM_RESOLVER, _RESOLVE_PHASE)


_REF_RECHECK_OWNER_DEF = re.compile(r"^def should_force_ref_recheck\(")


_REF_RECHECK_CALL = "should_force_ref_recheck("


_REF_RECHECK_PARALLEL = re.compile(r"_force_semver_resolve|def should_force_ref_recheck")


_REF_RECHECK_TEST_DEFS = re.compile(r"def _force_semver_resolve|def should_force_ref_recheck")


def check_ref_recheck_ownership(provider: FactsProvider) -> tuple[Violation, ...]:
    """Existing-path ref rechecks must use drift.py::should_force_ref_recheck.

    Covers the whole legacy semantic in one place -- owner definition,
    both consumer call sites, the in-consumer parallel-implementation ban,
    and the test-tree redefinition ban -- so no half of it can drift away
    from the other.
    """
    rule_id = RULE_REF_RECHECK
    owner, owner_fail = _configured(provider, _REF_RECHECK_OWNER, rule_id)
    findings: list[Violation] = list(owner_fail)
    if not owner_fail and not _has_re(owner, _REF_RECHECK_OWNER_DEF):
        findings.append(
            _report(
                rule_id,
                _REF_RECHECK_OWNER,
                "Existing-path ref rechecks must be owned by drift.py::should_force_ref_recheck",
            )
        )

    for consumer in _REF_RECHECK_CONSUMERS:
        lines, failures = _configured(provider, consumer, rule_id)
        findings.extend(failures)
        if failures:
            continue
        if not _has_text(lines, _REF_RECHECK_CALL):
            findings.append(
                _report(
                    rule_id,
                    consumer,
                    "Ref recheck consumers must call drift.py::should_force_ref_recheck; "
                    f"missing: {_REF_RECHECK_CALL}",
                )
            )
        findings.extend(
            _report(
                rule_id,
                consumer,
                "Ref recheck consumers must not reimplement the recheck decision "
                "locally; call drift.py::should_force_ref_recheck",
                line,
                column,
            )
            for line, column in _matches(lines, _REF_RECHECK_PARALLEL, respect_exempt=False)
        )

    test_paths = _tree_python_paths(provider, _TESTS_TREE)
    findings.extend(
        _banned(
            provider,
            rule_id=rule_id,
            paths=test_paths,
            pattern=_REF_RECHECK_TEST_DEFS,
            message=(
                "Tests must exercise drift.py::should_force_ref_recheck rather than "
                "redefine the recheck decision"
            ),
            configured=False,
            respect_exempt=False,
        )
    )
    return tuple(findings)


_QUEUE_DEDUP = re.compile(r"queued_keys.*get_unique_key|get_unique_key.*queued_keys")


def check_resolver_queue_dedup(provider: FactsProvider) -> tuple[Violation, ...]:
    """Resolver queue dedup must preserve ref constraints."""
    return tuple(
        _banned(
            provider,
            rule_id=RULE_QUEUE_DEDUP,
            paths=(_APM_RESOLVER,),
            pattern=_QUEUE_DEDUP,
            message=(
                "Resolver queue dedup must preserve ref constraints; do not key the "
                "queue on the identity-only unique key"
            ),
            respect_exempt=True,
        )
    )


_DEPENDENCY_IDENTITY = "src/apm_cli/models/dependency/identity.py"


_LOCAL_SOURCE_ANCHOR = 'if source == "local"'


_LOCAL_SOURCE_CONTEXT = 12


_ANCHORED_LOCAL_PATH = "anchored_local_path"


_DECLARING_PARENT = "declaring_parent"


def check_local_identity_anchor(provider: FactsProvider) -> tuple[Violation, ...]:
    """Local identity must use its anchor and persist declaring-parent provenance."""
    rule_id = RULE_LOCAL_ANCHOR
    identity, identity_fail = _configured(provider, _DEPENDENCY_IDENTITY, rule_id)
    lockfile, lockfile_fail = _configured(provider, _DEPS_LOCKFILE, rule_id)
    failures = [*identity_fail, *lockfile_fail]
    if failures:
        return tuple(failures)

    findings: list[Violation] = []
    branch = _after_context(identity, _LOCAL_SOURCE_ANCHOR, _LOCAL_SOURCE_CONTEXT)
    if not _has_text(branch, _ANCHORED_LOCAL_PATH):
        findings.append(
            _report(
                rule_id,
                _DEPENDENCY_IDENTITY,
                "Local dependency identity must be derived from its anchor; "
                f"missing: {_ANCHORED_LOCAL_PATH}",
                _first_line(identity, _LOCAL_SOURCE_ANCHOR),
            )
        )
    if not _has_text(lockfile, _DECLARING_PARENT):
        findings.append(
            _report(
                rule_id,
                _DEPS_LOCKFILE,
                f"Lockfile must persist declaring-parent provenance; missing: {_DECLARING_PARENT}",
            )
        )
    return tuple(findings)


_MCP_COMMAND = "src/apm_cli/commands/mcp.py"


_MCP_REGISTRY_CLIENT = "src/apm_cli/registry/client.py"


_MCP_INSTALL_REGISTRY = "src/apm_cli/install/mcp/registry.py"


_MARKETPLACE_RESOLVER = "src/apm_cli/marketplace/resolver.py"


# ``RegistryIntegration(<anything>)``. SimpleRegistryClient owns the registry
# URL precedence chain and records which layer supplied the URL; a
# caller-supplied URL is by definition the "explicit" layer, so pre-resolving
# in the command silently relabels env / apm config / default and breaks the
# source-aware diagnostics built on it.
_PRERESOLVED_REGISTRY_INTEGRATION = re.compile(r"RegistryIntegration\(\s*[^)\s]")


_PLUGIN_REGISTRY_ANCHOR = "if plugin.registry:"


_PLUGIN_REGISTRY_CONTEXT = 25


_REGISTRY_SOURCE = 'source="registry"'


_MCP_REGISTRY_OWNER_DEF = re.compile(r"^def resolve_mcp_registry_url\(")


_MCP_REGISTRY_CONFIG_LOOKUP = "get_mcp_registry_url()"


_MCP_REGISTRY_DELEGATION = "resolve_mcp_registry_url("


def check_mcp_registry_resolution(provider: FactsProvider) -> tuple[Violation, ...]:
    """MCP registry URL precedence must route through the registry client."""
    rule_id = RULE_MCP_REGISTRY_RESOLUTION
    findings = _banned(
        provider,
        rule_id=rule_id,
        paths=(_MCP_COMMAND,),
        pattern=_PRERESOLVED_REGISTRY_INTEGRATION,
        message=(
            "MCP commands must let SimpleRegistryClient resolve the registry URL; "
            "passing one into RegistryIntegration records the 'explicit' layer"
        ),
        respect_exempt=True,
    )
    owner, owner_fail = _configured(provider, _MCP_REGISTRY_CLIENT, rule_id)
    findings.extend(owner_fail)
    if not owner_fail:
        if not _has_re(owner, _MCP_REGISTRY_OWNER_DEF):
            findings.append(
                _report(
                    rule_id,
                    _MCP_REGISTRY_CLIENT,
                    "MCP registry precedence must be owned by resolve_mcp_registry_url",
                )
            )
        if not _has_text(owner, _MCP_REGISTRY_CONFIG_LOOKUP):
            findings.append(
                _report(
                    rule_id,
                    _MCP_REGISTRY_CLIENT,
                    "MCP registry precedence must include the persisted config layer",
                )
            )

    install_registry, install_fail = _configured(provider, _MCP_INSTALL_REGISTRY, rule_id)
    findings.extend(install_fail)
    if not install_fail and not _has_text(install_registry, _MCP_REGISTRY_DELEGATION):
        findings.append(
            _report(
                rule_id,
                _MCP_INSTALL_REGISTRY,
                "Install registry resolution must delegate to resolve_mcp_registry_url",
            )
        )
    return tuple(findings)


def check_registry_dependency_intent(provider: FactsProvider) -> tuple[Violation, ...]:
    """Resolved registry URLs and registry-sourced dependencies must survive."""
    rule_id = RULE_REGISTRY_INTENT
    findings: list[Violation] = []
    resolver, resolver_fail = _configured(provider, _MARKETPLACE_RESOLVER, rule_id)
    if resolver_fail:
        findings.extend(resolver_fail)
        return tuple(findings)

    branch = _after_context(resolver, _PLUGIN_REGISTRY_ANCHOR, _PLUGIN_REGISTRY_CONTEXT)
    if not _has_text(branch, _REGISTRY_SOURCE):
        findings.append(
            _report(
                rule_id,
                _MARKETPLACE_RESOLVER,
                "Marketplace registry intent must create a registry dependency; "
                f"missing: {_REGISTRY_SOURCE}",
                _first_line(resolver, _PLUGIN_REGISTRY_ANCHOR),
            )
        )
    return tuple(findings)
