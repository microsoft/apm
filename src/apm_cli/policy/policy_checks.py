"""Policy checks for organisational governance enforcement.

These checks run WITH a policy file and validate that the project's manifest,
lockfile, and on-disk state comply with the organisation's declared policies.
They are always run in addition to the baseline checks in ``ci_checks``.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from apm_cli.core.scope import manifest_file

from ..core.remediation import (
    BUNDLE_PACKAGES_UNRECORDED,
    CONTENT_HASHES_MISSING,
    REQUIRED_PACKAGE_UNLOCKED,
    UNMANAGED_FILE_TRACK,
)
from .allowlist_remedy import with_allowlist_next_step as _advise
from .models import (
    CheckResult,
    CIAuditResult,
    LocalBundleSubjects,
    PolicyRemedy,
    PolicySubject,
)

if TYPE_CHECKING:
    from ..integration.mcp_config_view import CurrentMcpConfigView

_logger = logging.getLogger(__name__)
FAIL_CLOSED_POLICY_CHECKS = frozenset({"dependency-content-hashes"})


# -- Helpers -------------------------------------------------------


def _load_raw_apm_yml(project_root: Path) -> dict | None:
    """Load raw apm.yml as a dict for policy checks that inspect raw fields.

    This helper is called **after** :pymethod:`APMPackage.from_apm_yml` has
    already succeeded in :func:`run_policy_checks`.  The primary security
    gate is ``from_apm_yml()`` -- if it fails, the audit aborts with a
    ``manifest-parse`` check result and this function is never reached.

    Returning ``None`` here is therefore **defence-in-depth**: it covers
    edge cases (TOCTOU race, transient I/O error) where the file becomes
    unreadable between the two calls.  Callers that receive ``None``
    gracefully skip supplementary raw-field checks (e.g.
    ``compilation-target``, ``extensions-present``) rather than hard-failing.

    Returns ``None`` when the file is absent, unreadable, malformed YAML,
    or not a mapping -- but logs a warning so the failure is visible
    rather than silently swallowed.
    """
    import yaml

    apm_yml_path = manifest_file(project_root)
    if not apm_yml_path.exists():
        return None
    try:
        with open(apm_yml_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        # TOCTOU: file disappeared between exists() check and open(); normal condition.
        return None
    except yaml.YAMLError as exc:
        _logger.warning("Malformed YAML in %s: %s", apm_yml_path, exc)
        return None
    except OSError as exc:
        _logger.warning("Cannot read %s: %s", apm_yml_path, exc)
        return None
    except UnicodeDecodeError as exc:
        _logger.warning("Cannot decode %s as UTF-8: %s", apm_yml_path, exc)
        return None
    if not isinstance(data, dict):
        _logger.warning(
            "apm.yml is not a YAML mapping (got %s) -- skipping raw-field checks",
            type(data).__name__,
        )
        return None
    return data


# -- Individual policy checks --------------------------------------


def _subjects_result(
    name: str,
    message: str,
    subjects: Sequence[PolicySubject],
    *,
    passed: bool = False,
) -> CheckResult:
    """Build a check result whose ``details`` mirror its structured subjects.

    ``details`` keeps the ``"{ref}: {reason}"`` report contract that
    ``apm audit`` renders; ``subjects`` carries the same violations for the
    install gate.
    """
    return CheckResult(
        name=name,
        passed=passed,
        message=message,
        details=[f"{s.ref}: {s.reason}" for s in subjects],
        subjects=tuple(subjects),
    )


def _with_mcp_declarers(
    checks: list[CheckResult],
    mcp_deps: list,
    brought_in_by: Mapping[str, str] | None,
) -> list[CheckResult]:
    """Point each transitive MCP server's removal hint at an apm.yml entry.

    A transitive server is not in apm.yml, so the hint names the direct
    dependency that brings it in (*brought_in_by*, keyed by server name).
    ``resolved_by`` is only the declaring package's display name, never an
    apm.yml entry, so without a known direct dependency no apm.yml edit is
    suggested.
    """
    root_declared = {m.name for m in mcp_deps if not getattr(m, "resolved_by", None)}
    transitive = {m.name for m in mcp_deps if getattr(m, "resolved_by", None)} - root_declared
    if not transitive:
        return checks
    brought_in_by = brought_in_by or {}

    def _hint(subject: PolicySubject) -> PolicySubject:
        if subject.ref not in transitive or subject.remedy != PolicyRemedy.REMOVE:
            return subject
        declarer = brought_in_by.get(subject.ref)
        if declarer:
            return replace(subject, declared_by=declarer)
        return replace(subject, remedy=None)

    return [
        replace(check, subjects=tuple(_hint(subject) for subject in check.subjects))
        for check in checks
    ]


def _required_policy_index(
    deps: list[DependencyReference],
    dep_names: set[str] | DependencyPolicyIndex | None,
) -> DependencyPolicyIndex:
    """Reuse a rich snapshot or honor the legacy supplied-name contract."""
    from .matcher import DependencyPolicyIndex

    if isinstance(dep_names, DependencyPolicyIndex):
        return dep_names
    if dep_names is not None:
        return DependencyPolicyIndex.from_names(dep_names)
    return DependencyPolicyIndex.from_dependencies(deps)


def _declared_then_bundled(
    deps: Sequence[DependencyReference], bundled: Sequence[DependencyReference]
) -> list[tuple[DependencyReference, PolicyRemedy | None]]:
    """Each declared dependency, then each bundled one not declared, with its remedy.

    A declared dependency's violation suggests removing it from apm.yml; a
    bundled one has no apm.yml entry, so it suggests no edit.
    """
    keys = {dep.get_unique_key() for dep in deps}
    return [(dep, PolicyRemedy.REMOVE) for dep in deps] + [
        (dep, None) for dep in bundled if dep.get_unique_key() not in keys
    ]


def _check_dependency_allowlist(
    deps: list[DependencyReference],
    policy: DependencyPolicy,
    bundle: LocalBundleSubjects | None = None,
) -> CheckResult:
    """Check 1: every dependency matches policy allow list.

    A local *bundle* is judged by the packages it records, never by its
    author-chosen name; one that records none is refused (#4646).
    """
    from .matcher import check_dependency_allowed, dependency_policy_subject

    if policy.allow is None:
        return CheckResult(
            name="dependency-allowlist",
            passed=True,
            message="No dependency allow list configured",
        )

    subjects: list[PolicySubject] = []
    for dep, remedy in _declared_then_bundled(deps, bundle.packages if bundle else ()):
        allowed, reason = check_dependency_allowed(dep, policy)
        if not allowed and "not in allowed" in reason:
            subjects.append(PolicySubject(dependency_policy_subject(dep), reason, remedy))
    if bundle is not None and not bundle.packages:
        subjects.append(_unmatchable_bundle_subject(bundle))

    if not subjects:
        return CheckResult(
            name="dependency-allowlist",
            passed=True,
            message="All dependencies match allow list",
        )
    return _subjects_result(
        "dependency-allowlist",
        f"{len(subjects)} dependency(ies) not in allow list",
        subjects,
    )


def _unmatchable_bundle_subject(bundle: LocalBundleSubjects) -> PolicySubject:
    """The allow-list refusal of a local bundle that records no packages (#4646).

    Passing it would let any content through an allow list under an
    author-chosen name, so it fails closed.
    """
    from .matcher import dependency_policy_subject

    ref = dependency_policy_subject(bundle.names[0]) if bundle.names else "local bundle"
    return PolicySubject(
        ref,
        "the bundle records no packages for dependencies.allow to match, and a bundle "
        f"name never satisfies the allow list. {BUNDLE_PACKAGES_UNRECORDED.sentence()}",
        None,
    )


def _check_dependency_denylist(
    deps: list[DependencyReference],
    policy: DependencyPolicy,
    bundled: Sequence[DependencyReference] = (),
) -> CheckResult:
    """Check 2: no dependency matches policy deny list.

    *bundled* are packages a local bundle or a pack ships without an apm.yml
    entry in this project; they meet the deny list only, and their
    violations suggest no apm.yml edit. One declared in *deps* is judged once.
    """
    from .matcher import check_dependency_allowed, dependency_policy_subject

    if not policy.effective_deny:
        return CheckResult(
            name="dependency-denylist",
            passed=True,
            message="No dependency deny list configured",
        )

    subjects: list[PolicySubject] = []
    for dep, remedy in _declared_then_bundled(deps, bundled):
        allowed, reason = check_dependency_allowed(dep, policy)
        if not allowed and "denied by pattern" in reason:
            subjects.append(PolicySubject(dependency_policy_subject(dep), reason, remedy))

    if not subjects:
        return CheckResult(
            name="dependency-denylist",
            passed=True,
            message="No dependencies match deny list",
        )
    return _subjects_result(
        "dependency-denylist",
        f"{len(subjects)} dependency(ies) match deny list",
        subjects,
    )


def _check_required_packages(
    deps: list[DependencyReference],
    policy: DependencyPolicy,
    *,
    dep_names: set[str] | DependencyPolicyIndex | None = None,
) -> CheckResult:
    """Check 3: every required package is in manifest deps."""
    if not policy.effective_require:
        return CheckResult(
            name="required-packages",
            passed=True,
            message="No required packages configured",
        )

    index = _required_policy_index(deps, dep_names)
    subjects: list[PolicySubject] = []
    for req in policy.effective_require:
        pkg_name = req.split("#", 1)[0]
        if index.find_name(pkg_name) is None:
            subjects.append(_unresolved_required_subject(index, pkg_name))

    if not subjects:
        return CheckResult(
            name="required-packages",
            passed=True,
            message="All required packages present in manifest",
        )
    return CheckResult(
        name="required-packages",
        passed=False,
        message=f"{len(subjects)} required package(s) missing from manifest."
        + _wrong_host_hint(index, subjects),
        details=[subject.ref for subject in subjects],
        subjects=tuple(subjects),
    )


def _unresolved_required_subject(index: DependencyPolicyIndex, pkg_name: str) -> PolicySubject:
    """Explain a ``require`` entry that identifies no declared dependency.

    When the same package is declared on another host, name both fixes;
    otherwise the package must be added to apm.yml.
    """
    fix = _wrong_host_fix(index, pkg_name)
    if fix is not None:
        return PolicySubject(pkg_name, f"required by org policy, but {fix}", remedy=None)
    return PolicySubject(pkg_name, "required by org policy but not declared", PolicyRemedy.ADD)


def _wrong_host_fix(index: DependencyPolicyIndex, pkg_name: str) -> str | None:
    """Describe a same-path package declared on another host, manifest fix first.

    The declared host is not trusted by default: it may be a typosquat, so the
    policy rewrite is offered only for a host the reader trusts.
    """
    mismatch = index.host_mismatch(pkg_name)
    if mismatch is None:
        return None
    wanted, declared = mismatch
    where = declared.host or "a local path"
    return (
        f"'{pkg_name}' is declared on {where}, not {wanted} -- declare it on {wanted}, "
        f"or, if {where} is trusted, change the policy require entry to "
        f"'{declared.require_entry}'"
    )


def _wrong_host_hint(index: DependencyPolicyIndex, subjects: Iterable[PolicySubject]) -> str:
    """Append the wrong-host fixes to a check message.

    ``apm audit --ci`` prints the message and details, not subject reasons, so
    the fix must be in the message to reach the user.
    """
    fixes = [fix for subject in subjects if (fix := _wrong_host_fix(index, subject.ref))]
    return f" {'; '.join(fixes)}." if fixes else ""


def _check_required_packages_deployed(
    deps: list[DependencyReference],
    lock: LockFile | None,
    policy: DependencyPolicy,
    *,
    dep_names: set[str] | DependencyPolicyIndex | None = None,
    installing: bool = False,
) -> CheckResult:
    """Check 4: required packages are PRESENT in the lockfile (issue #1873, Gap B).

    *installing* means the caller is the install that locks *deps*: a declared
    required package it is about to record is not absent, or the check would
    block the very install that satisfies it.

    Asserts package PRESENCE, not materialised ``deployed_files``. A package
    can be legitimately present-but-parked -- resolved and locked, with its
    executable primitives gated pending approval (``exec_status =
    gated_pending_approval``) and therefore zero deployed files yet. The old
    ``not locked.deployed_files`` test mis-fired on exactly that healthy state
    and blocked installs that should succeed. Executable trust is now audited
    separately by ``required-executable-untrusted``.
    """
    if not policy.effective_require or lock is None:
        return CheckResult(
            name="required-packages-deployed",
            passed=True,
            message="No required packages to verify deployment",
        )

    index = _required_policy_index(deps, dep_names)
    unresolved: list[PolicySubject] = []
    absent: list[PolicySubject] = []
    for req in policy.effective_require:
        pkg_name = req.split("#", 1)[0]
        match = index.find(pkg_name)
        if match is None:
            # Never skip: the entry cannot locate its lock row by identity.
            unresolved.append(_unresolved_required_subject(index, pkg_name))
        elif match.locked_in(lock) is None and not installing:
            # PRESENCE, not deployment: the package must appear in the lockfile.
            absent.append(
                PolicySubject(
                    pkg_name,
                    "required package absent from the lockfile -- "
                    f"{REQUIRED_PACKAGE_UNLOCKED.clause()}",
                    remedy=None,
                )
            )

    if not unresolved and not absent:
        return CheckResult(
            name="required-packages-deployed",
            passed=True,
            message="All required packages present in lockfile",
        )
    messages: list[str] = []
    if absent:
        messages.append(
            f"{len(absent)} required package(s) absent from the lockfile. "
            f"Hint: {REQUIRED_PACKAGE_UNLOCKED.sentence()}"
        )
    if unresolved:
        messages.append(
            f"{len(unresolved)} require entry(ies) match no declared package."
            + _wrong_host_hint(index, unresolved)
        )
    subjects = absent + unresolved
    return CheckResult(
        name="required-packages-deployed",
        passed=False,
        message=" ".join(messages),
        details=[subject.ref for subject in subjects],
        subjects=tuple(subjects),
    )


def _check_required_executable_untrusted(
    deps: list[DependencyReference],
    lock: LockFile | None,
    exec_policy: ExecutablesPolicy,
    *,
    dep_names: set[str] | DependencyPolicyIndex | None = None,
) -> CheckResult:
    """Check 4b: required-executable packages must be TRUSTED, not parked.

    For every package in the org ``executables.require`` set, the lockfile's
    ``exec_status`` must be ``deployed``. A present-but-parked package
    (``gated_pending_approval``) or a denied one is a hard CI failure here --
    the install itself SUCCEEDS so a developer can self-approve, but a fleet
    that mandates the executable cannot ship it untrusted (issue #1873).
    """
    required = tuple(exec_policy.require or ())
    if not required or lock is None:
        return CheckResult(
            name="required-executable-untrusted",
            passed=True,
            message="No required executables to verify",
        )

    from ..security.executables import TRUST_DEPLOYED

    index = _required_policy_index(deps, dep_names)
    untrusted: list[str] = []
    misnamed: list[PolicySubject] = []
    for req in required:
        pkg_name = req.split("#", 1)[0]
        match = index.find(pkg_name)
        if match is None:
            if index.host_mismatch(pkg_name) is not None:
                misnamed.append(_unresolved_required_subject(index, pkg_name))
            continue  # undeclared: presence is audited by required-packages / -deployed
        locked = match.locked_in(lock)
        # Trusted when exec_status is absent (no executables declared) or when
        # the executable gate recorded a deployed state. Gated or denied
        # required executables are untrusted.
        if locked is None or (
            locked.exec_status is not None and locked.exec_status != TRUST_DEPLOYED
        ):
            untrusted.append(pkg_name)

    if not untrusted and not misnamed:
        return CheckResult(
            name="required-executable-untrusted",
            passed=True,
            message="All required executables are trusted",
        )
    messages: list[str] = []
    if untrusted:
        recommend = set(exec_policy.recommend or ())
        remedy = "Trust them to deploy: `apm approve <package>` per package."
        if any(pkg in recommend for pkg in untrusted):
            remedy += (
                " Org-recommended packages can be bulk-trusted with `apm approve --recommended`."
            )
        messages.append(f"{len(untrusted)} required executable(s) present but untrusted. {remedy}")
    if misnamed:
        messages.append(
            f"{len(misnamed)} required executable(s) name another host."
            + _wrong_host_hint(index, misnamed)
        )
    subjects = [
        PolicySubject(
            pkg,
            f"required executable present but untrusted -- trust it with `apm approve {pkg}`",
            remedy=None,
        )
        for pkg in untrusted
    ] + misnamed
    return CheckResult(
        name="required-executable-untrusted",
        passed=False,
        message=" ".join(messages),
        details=[subject.ref for subject in subjects],
        subjects=tuple(subjects),
    )


def _check_required_package_version(
    deps: list[DependencyReference],
    lock: LockFile | None,
    policy: DependencyPolicy,
    *,
    dep_names: set[str] | DependencyPolicyIndex | None = None,
) -> CheckResult:
    """Check 5: required packages with version pins match per resolution strategy."""
    pinned = [(r, r.split("#", 1)) for r in policy.effective_require if "#" in r]
    if not pinned or lock is None:
        return CheckResult(
            name="required-package-version",
            passed=True,
            message="No version-pinned required packages",
        )

    resolution = policy.require_resolution
    violations: list[PolicySubject] = []
    warnings: list[PolicySubject] = []

    index = _required_policy_index(deps, dep_names)

    for _req, parts in pinned:
        pkg_name, expected_ref = parts[0], parts[1]

        # Without a manifest dependency there is no host/source authority from
        # which to derive casing: an undeclared name keeps the legacy raw lock
        # lookup (checks 3 and 4 report it unmatched). A same-path package
        # declared on another host is never compared; check 3 names the fix.
        match = index.find_or_undeclared(pkg_name)
        locked = match.locked_in(lock) if match is not None else None
        if locked is not None:
            actual_ref = locked.resolved_ref or ""
            if actual_ref != expected_ref:
                detail = PolicySubject(
                    pkg_name,
                    f"expected ref '{expected_ref}', got '{actual_ref}'",
                    remedy=None,
                )
                if resolution == "block" or resolution == "policy-wins":  # noqa: PLR1714
                    violations.append(detail)
                else:  # project-wins
                    warnings.append(detail)

    if not violations:
        return _subjects_result(
            "required-package-version",
            "Required package versions match"
            + (f" (warnings: {len(warnings)})" if warnings else ""),
            warnings,
            passed=True,
        )
    return _subjects_result(
        "required-package-version",
        f"{len(violations)} version mismatch(es)",
        violations,
    )


def _check_transitive_depth(
    lock: LockFile | None,
    policy: DependencyPolicy,
) -> CheckResult:
    """Check 6: no lockfile dep exceeds max_depth."""
    if lock is None or policy.max_depth >= 50:
        return CheckResult(
            name="transitive-depth",
            passed=True,
            message="No transitive depth limit configured"
            if policy.max_depth >= 50
            else "No lockfile to check",
        )

    violations = [
        PolicySubject(key, f"depth {dep.depth} exceeds limit {policy.max_depth}", remedy=None)
        for key, dep in lock.dependencies.items()
        if dep.depth > policy.max_depth
    ]

    if not violations:
        return CheckResult(
            name="transitive-depth",
            passed=True,
            message=f"All dependencies within depth limit ({policy.max_depth})",
        )
    return _subjects_result(
        "transitive-depth",
        f"{len(violations)} dependency(ies) exceed max depth {policy.max_depth}",
        violations,
    )


def _check_mcp_allowlist(
    mcp_deps: list,
    policy: McpPolicy,
) -> CheckResult:
    """Check 7: MCP server names match allow list."""
    from .matcher import check_mcp_allowed

    if policy.allow is None:
        return CheckResult(
            name="mcp-allowlist",
            passed=True,
            message="No MCP allow list configured",
        )

    violations: list[PolicySubject] = []
    for mcp in mcp_deps:
        allowed, reason = check_mcp_allowed(mcp.name, policy)
        if not allowed and "not in allowed" in reason:
            violations.append(PolicySubject(mcp.name, reason))

    if not violations:
        return CheckResult(
            name="mcp-allowlist",
            passed=True,
            message="All MCP servers match allow list",
        )
    return _subjects_result(
        "mcp-allowlist",
        f"{len(violations)} MCP server(s) not in allow list",
        violations,
    )


def _check_mcp_denylist(
    mcp_deps: list,
    policy: McpPolicy,
) -> CheckResult:
    """Check 8: no MCP server matches deny list."""
    from .matcher import check_mcp_allowed

    if not policy.deny:
        return CheckResult(
            name="mcp-denylist",
            passed=True,
            message="No MCP deny list configured",
        )

    violations: list[PolicySubject] = []
    for mcp in mcp_deps:
        allowed, reason = check_mcp_allowed(mcp.name, policy)
        if not allowed and "denied by pattern" in reason:
            violations.append(PolicySubject(mcp.name, reason))

    if not violations:
        return CheckResult(
            name="mcp-denylist",
            passed=True,
            message="No MCP servers match deny list",
        )
    return _subjects_result(
        "mcp-denylist",
        f"{len(violations)} MCP server(s) match deny list",
        violations,
    )


def _effective_mcp_transports(
    mcp_deps: list,
    policy: McpPolicy,
) -> Mapping[str, EffectiveTransport]:
    """Resolve each server's effective canonical transports for check 9.

    Resolution queries the MCP registry for every registry server, with or
    without a declared ``transport:`` (install writes what the registry
    serves), so it runs only when ``mcp.transport.allow`` is configured.
    """
    if policy.transport.allow is None or not mcp_deps:
        return {}
    from ..integration.mcp_transport_resolution import resolve_effective_mcp_transports

    return resolve_effective_mcp_transports(mcp_deps)


def _check_mcp_transport(
    effective_transports: Mapping[str, EffectiveTransport],
    policy: McpPolicy,
) -> CheckResult:
    """Check 9: effective MCP transports match the policy allow list.

    *effective_transports* maps each server name to the canonical
    transports APM may write for it, or an ``UnresolvedTransport`` reason.
    ``policy.transport.allow`` is canonical from the parser.
    """
    from ..mcp_transport import UnresolvedTransport

    allowed_transports = policy.transport.allow
    if allowed_transports is None:
        return CheckResult(
            name="mcp-transport",
            passed=True,
            message="No MCP transport restrictions configured",
        )

    allowed_label = f"[{', '.join(allowed_transports)}]"
    violations: list[PolicySubject] = []
    for name, transports in effective_transports.items():
        if isinstance(transports, UnresolvedTransport):
            violations.append(
                PolicySubject(
                    name,
                    f"effective transport unknown ({transports.reason}); "
                    f"cannot check it against allowed {allowed_label}. Check the server "
                    "name and registry URL, or make the MCP registry reachable, then "
                    "retry. A declared 'transport:' cannot stand in: install writes the "
                    "transport the registry serves",
                )
            )
            continue
        violations.extend(
            PolicySubject(
                name,
                f"transport '{transport}' not in allowed {allowed_label}",
            )
            for transport in sorted(transports)
            if transport not in allowed_transports
        )

    if not violations:
        return CheckResult(
            name="mcp-transport",
            passed=True,
            message="All MCP transports comply with policy",
        )
    return _subjects_result(
        "mcp-transport",
        f"{len(violations)} MCP transport violation(s)",
        violations,
    )


def _check_mcp_self_defined(
    mcp_deps: list,
    policy: McpPolicy,
) -> CheckResult:
    """Check 10: self-defined MCP servers comply with policy."""
    self_defined_policy = policy.self_defined
    if self_defined_policy == "allow":
        return CheckResult(
            name="mcp-self-defined",
            passed=True,
            message="Self-defined MCP servers allowed",
        )

    self_defined = [m for m in mcp_deps if m.registry is False]
    if not self_defined:
        return CheckResult(
            name="mcp-self-defined",
            passed=True,
            message="No self-defined MCP servers found",
        )

    subjects = [PolicySubject(m.name, "self-defined server") for m in self_defined]
    if self_defined_policy == "deny":
        return _subjects_result(
            "mcp-self-defined",
            f"{len(self_defined)} self-defined MCP server(s) denied by policy",
            subjects,
        )
    # warn -- pass but with details
    return _subjects_result(
        "mcp-self-defined",
        f"{len(self_defined)} self-defined MCP server(s) (warn)",
        subjects,
        passed=True,
    )


_TRUST_TRANSITIVE = "mcp-trust-transitive"
_TRUST_TRANSITIVE_REASON = "transitive server not in mcp.allow (mcp.trust_transitive: false)"


def _trust_transitive_result(names: Sequence[str]) -> CheckResult:
    if not names:
        return CheckResult(
            name=_TRUST_TRANSITIVE,
            passed=True,
            message="No transitive MCP server outside mcp.allow",
        )
    return _subjects_result(
        _TRUST_TRANSITIVE,
        f"{len(names)} transitive MCP server(s) not in mcp.allow while "
        "mcp.trust_transitive is false",
        [PolicySubject(name, _TRUST_TRANSITIVE_REASON) for name in names],
    )


def _check_mcp_trust_transitive(mcp_deps: list, policy: McpPolicy) -> CheckResult:
    """Check 10b: with ``mcp.trust_transitive`` false, transitive servers need mcp.allow.

    A transitive server (declared by a package at lock depth > 1, and not
    re-declared by the root apm.yml) passes only when ``mcp.allow`` lists it
    (req-sc-016). A direct dependency's server is not transitive.
    """
    from ..security.trust import transitive_server_listed

    if policy.trust_transitive:
        return _trust_transitive_result(())
    root = {m.name for m in mcp_deps if not getattr(m, "resolved_by", None)}
    transitive = sorted({m.name for m in mcp_deps if getattr(m, "declared_depth", 0) > 1} - root)
    return _trust_transitive_result(
        [name for name in transitive if not transitive_server_listed(name, policy)]
    )


def _parked_trust_transitive(
    view: CurrentMcpConfigView, native: Sequence[str] = ()
) -> CheckResult | None:
    """Audit: transitive servers withheld under ``mcp.trust_transitive``.

    *view* carries the servers the trust owner parked; *native* names those
    of locked transitive native plugins, which Claude loads itself.
    """
    from ..security.trust import REASON_POLICY_TRANSITIVE

    parked = sorted(
        {
            name
            for name, evidence in view.omissions.items()
            if isinstance(evidence, Mapping) and evidence.get("reason") == REASON_POLICY_TRANSITIVE
        }
        | set(native)
    )
    return _trust_transitive_result(parked) if parked else None


def mcp_policy_checks(mcp_list: list, policy: ApmPolicy) -> list[CheckResult]:
    """Run org MCP checks 7-10b (allow, deny, transport, self-defined, trust_transitive).

    The one composition of the MCP checks: the install gate and the native
    Claude plugin route (which vets the servers a plugin declares) both use it.
    """
    return [
        _check_mcp_allowlist(mcp_list, policy.mcp),
        _check_mcp_denylist(mcp_list, policy.mcp),
        _check_mcp_transport(_effective_mcp_transports(mcp_list, policy.mcp), policy.mcp),
        _check_mcp_self_defined(mcp_list, policy.mcp),
        _check_mcp_trust_transitive(mcp_list, policy.mcp),
    ]


def mcp_policy_restricts(policy: ApmPolicy) -> bool:
    """Whether checks 7-10 can reject some self-defined server.

    A server APM cannot read can only be shown to comply with a policy that
    restricts nothing; any allow, deny, transport or self-defined rule makes
    it fail closed.
    """
    mcp = policy.mcp
    return bool(
        mcp.allow is not None
        or mcp.deny
        or mcp.transport.allow is not None
        or mcp.self_defined == "deny"
    )


def _check_compilation_target(
    targets: tuple[str, ...] | None,
    policy: CompilationPolicy,
    *,
    source: str = "apm.yml",
) -> CheckResult:
    """Check 11: the selected target profiles match compilation-target policy.

    *targets* must be an :attr:`EffectiveTargetDecision.canonical_targets`
    projection: aliases resolved, ``all`` expanded, MCP-only targets mapped to
    their primitive profile. ``None`` or empty means nothing is selected.
    """
    from ..core.target_detection import (
        EffectiveTargetDecision,
        canonical_policy_target_name,
        canonical_policy_target_names,
    )

    enforce = canonical_policy_target_name(policy.target.enforce)
    allow = canonical_policy_target_names(policy.target.allow)

    if not enforce and allow is None:
        return CheckResult(
            name="compilation-target",
            passed=True,
            message="No compilation target restrictions configured",
        )

    if not targets:
        return CheckResult(
            name="compilation-target",
            passed=True,
            message="No compilation target set in manifest",
        )

    target_list = list(targets)
    if enforce:
        required = (
            EffectiveTargetDecision("all", "policy").canonical_targets or ()
            if enforce == "all"
            else (enforce,)
        )
        missing = [name for name in required if name not in target_list]
        if missing:
            return CheckResult(
                name="compilation-target",
                passed=False,
                message=f"Enforced target '{enforce}' not present in {target_list}",
                details=[f"targets: {target_list} (source: {source}), enforced: {enforce}"],
            )
    elif allow is not None:
        disallowed = [name for name in target_list if name not in allow]
        if disallowed:
            return CheckResult(
                name="compilation-target",
                passed=False,
                message=f"Target(s) {disallowed} not in allowed list {sorted(allow)}",
                details=[f"targets: {target_list} (source: {source}), allowed: {sorted(allow)}"],
            )

    return CheckResult(
        name="compilation-target",
        passed=True,
        message="Compilation target compliant",
    )


def _check_compilation_strategy(
    raw_yml: dict | None,
    policy: CompilationPolicy,
) -> CheckResult:
    """Check 12: compilation strategy matches policy."""
    enforce = policy.strategy.enforce
    if not enforce:
        return CheckResult(
            name="compilation-strategy",
            passed=True,
            message="No compilation strategy enforced",
        )

    compilation = (raw_yml or {}).get("compilation", {})
    strategy = compilation.get("strategy") if isinstance(compilation, dict) else None
    if not strategy:
        return CheckResult(
            name="compilation-strategy",
            passed=True,
            message="No compilation strategy set in manifest",
        )

    if strategy != enforce:
        return CheckResult(
            name="compilation-strategy",
            passed=False,
            message=f"Strategy '{strategy}' does not match enforced '{enforce}'",
            details=[f"strategy: {strategy}, enforced: {enforce}"],
        )
    return CheckResult(
        name="compilation-strategy",
        passed=True,
        message="Compilation strategy compliant",
    )


def _check_source_attribution(
    raw_yml: dict | None,
    policy: CompilationPolicy,
) -> CheckResult:
    """Check 13: source attribution enabled if policy requires."""
    if not policy.source_attribution:
        return CheckResult(
            name="source-attribution",
            passed=True,
            message="Source attribution not required by policy",
        )

    compilation = (raw_yml or {}).get("compilation", {})
    attribution = compilation.get("source_attribution") if isinstance(compilation, dict) else None
    if attribution is True:
        return CheckResult(
            name="source-attribution",
            passed=True,
            message="Source attribution enabled",
        )
    return CheckResult(
        name="source-attribution",
        passed=False,
        message="Source attribution required by policy but not enabled in manifest",
        details=["Set compilation.source_attribution: true in apm.yml"],
    )


def _check_required_manifest_fields(
    raw_yml: dict | None,
    policy: ManifestPolicy,
) -> CheckResult:
    """Check 14: all required fields are present with non-empty values."""
    if not policy.required_fields:
        return CheckResult(
            name="required-manifest-fields",
            passed=True,
            message="No required manifest fields configured",
        )

    data = raw_yml or {}
    missing: list[str] = []
    for field_name in policy.required_fields:
        value = data.get(field_name)
        if not value:  # None, empty string, missing
            missing.append(field_name)

    if not missing:
        return CheckResult(
            name="required-manifest-fields",
            passed=True,
            message="All required manifest fields present",
        )
    return CheckResult(
        name="required-manifest-fields",
        passed=False,
        message=f"{len(missing)} required manifest field(s) missing",
        details=missing,
    )


_INCLUDES_NOT_PROVIDED = object()


def _check_includes_explicit(
    manifest_includes,
    policy: ManifestPolicy,
) -> CheckResult:
    """Check: manifest declares an explicit ``includes:`` list when policy requires it.

    ``manifest_includes`` is the parsed value of the manifest's ``includes:``
    field as exposed by :class:`APMPackage` -- one of ``None`` (field
    absent), the literal string ``"auto"``, or a list of repo-relative
    path strings.

    Violation when ``policy.require_explicit_includes`` is True and
    ``manifest_includes`` is ``None`` or ``"auto"``.
    """
    if not policy.require_explicit_includes:
        return CheckResult(
            name="explicit-includes",
            passed=True,
            message="Explicit includes not required by policy",
        )

    if manifest_includes is None:
        return CheckResult(
            name="explicit-includes",
            passed=False,
            message=(
                "Policy requires explicit 'includes:' paths but none are "
                "declared. Add 'includes: [<path>, ...]' to apm.yml with "
                "the paths you intend to publish."
            ),
            details=[
                "includes: <absent>, require_explicit_includes: true",
            ],
        )

    if manifest_includes == "auto":
        return CheckResult(
            name="explicit-includes",
            passed=False,
            message=(
                "Policy requires explicit 'includes:' paths but manifest "
                "uses 'includes: auto'. Replace with an explicit list of "
                "paths."
            ),
            details=[
                "includes: 'auto', require_explicit_includes: true",
            ],
        )

    return CheckResult(
        name="explicit-includes",
        passed=True,
        message="Manifest declares explicit includes paths",
    )


def _check_scripts_policy(
    raw_yml: dict | None,
    policy: ManifestPolicy,
) -> CheckResult:
    """Check 15: scripts section absent if policy denies it."""
    if policy.scripts != "deny":
        return CheckResult(
            name="scripts-policy",
            passed=True,
            message="Scripts allowed by policy",
        )

    scripts = (raw_yml or {}).get("scripts")
    if scripts:
        return CheckResult(
            name="scripts-policy",
            passed=False,
            message="Scripts section present but denied by policy",
            details=list(scripts.keys()) if isinstance(scripts, dict) else ["scripts"],
        )
    return CheckResult(
        name="scripts-policy",
        passed=True,
        message="No scripts section (compliant with deny policy)",
    )


_DEFAULT_GOVERNANCE_DIRS = [
    ".github/agents",
    ".github/instructions",
    ".github/hooks",
    ".cursor/rules",
    ".claude",
    ".opencode",
    ".kiro",
]


_MAX_UNMANAGED_SCAN_FILES = 10_000

# Appended once to a non-empty unmanaged-files report so a flagged file is
# self-resolving: the reader learns how to track it or how to suppress it.
_UNMANAGED_NEXT_ACTION = (
    f"Next: {UNMANAGED_FILE_TRACK.clause()}, "
    "or add a glob to unmanaged_files.exclude to suppress it."
)


def _classify_primitive_type(rel_path: str) -> str | None:
    """Lazily classify an already-flagged unmanaged file by APM convention.

    Called ONLY on files already flagged as unmanaged -- never on the whole
    tree -- so a user can triage skill / agent / instruction / mcp artifacts.
    Returns ``None`` when the path matches no known primitive convention.
    """
    posix = rel_path.replace("\\", "/").lower()
    name = posix.rsplit("/", 1)[-1]
    segments = posix.split("/")
    # Explicit filename conventions win first (most specific signal).
    if name.endswith(".instructions.md"):
        return "instruction"
    if name.endswith(".agent.md"):
        return "agent"
    if name == "mcp.json" or name.endswith(".mcp.json"):
        return "mcp"
    if name == "skill.md":
        return "skill"
    # Directory-segment hints next (less specific). MCP is narrowed to a
    # dedicated ``.mcp/`` root -- a directory merely named ``mcp`` is not an
    # MCP config and must not mislabel files under it.
    if "instructions" in segments:
        return "instruction"
    if "agents" in segments:
        return "agent"
    if "skills" in segments:
        return "skill"
    if ".mcp" in segments:
        return "mcp"
    return None


def _unmanaged_deny_conflict(
    rel_path: str,
    dependency_deny: tuple[str, ...] | None,
    mcp_deny: tuple[str, ...] | None,
) -> str | None:
    """Return the deny pattern an unmanaged file conflicts with, or ``None``.

    Surfaces APM's OWN deny policy as a human-resolve conflict: the dependency
    side is defaults-inclusive (``dependencies.effective_deny``) and the MCP
    side is the raw ``mcp.deny`` -- mirroring the deny-list checks exactly.
    Routes through the same ``first_matching_pattern`` matcher the deny-list
    checks use -- never a second matcher.
    """
    from .matcher import first_matching_pattern

    name = rel_path.rsplit("/", 1)[-1]
    for patterns in (dependency_deny, mcp_deny):
        hit = first_matching_pattern(rel_path, patterns)
        if hit is None:
            # Fall back to the basename so a deny glob written against a bare
            # filename (e.g. 'mcp.json') still surfaces the conflict.
            hit = first_matching_pattern(name, patterns)
        if hit is not None:
            return hit
    return None


def _format_unmanaged_detail(
    rel_path: str,
    primitive_type: str | None,
    deny_hit: str | None,
) -> str:
    """Render one enriched, ASCII-only finding line for an unmanaged file."""
    label = f"{rel_path} [type: {primitive_type}]" if primitive_type else rel_path
    reasons = ["not tracked in apm.lock.yaml"]
    if deny_hit:
        reasons.append(f"matches deny rule ({deny_hit})")
    return f"{label} -- {'; '.join(reasons)}"


def _symlink_escapes_workspace(path: Path, project_root: Path) -> bool:
    """Return True if *path* is a symlink resolving outside *project_root*.

    Guards the traversal so a symlink pointing out of the workspace is never
    followed (no traversal bomb); broken or looping links also count as
    escaping and are skipped.
    """
    try:
        resolved = path.resolve()
        resolved.relative_to(project_root.resolve())
        return False
    except (OSError, RuntimeError, ValueError):
        return True


def _check_unmanaged_files(
    project_root: Path,
    lock: LockFile | None,
    policy: UnmanagedFilesPolicy,
    *,
    dependency_deny: tuple[str, ...] | None = None,
    mcp_deny: tuple[str, ...] | None = None,
) -> CheckResult:
    """Check 16: surface files in governance dirs not tracked in apm.lock.yaml.

    This is the ONE unified unmanaged-files report. Each flagged file is
    enriched in-place (within this single scan) with a factual reason, a lazy
    primitive-type classification, and -- where it matches APM's own
    ``dependencies.deny`` / ``mcp.deny`` -- a deny-conflict note for a human to
    resolve. Paths matching ``policy.exclude`` are suppressed. This is drift /
    divergence visibility, not supply-chain-attack prevention.
    """
    from ..integration.hook_ownership import verified_apm_hook_files
    from .matcher import first_matching_pattern

    if policy.effective_action == "ignore":
        return CheckResult(
            name="unmanaged-files",
            passed=True,
            message="Unmanaged files check disabled (action: ignore)",
        )

    dirs = policy.directories if policy.directories else _DEFAULT_GOVERNANCE_DIRS
    exclude = policy.exclude or ()

    deployed: set[str] = set(verified_apm_hook_files(project_root, lock)) if lock else set()
    deployed_dir_prefixes: list[str] = []
    if lock:
        for _key, dep in lock.dependencies.items():
            for f in dep.deployed_files:
                cleaned = f.rstrip("/")
                deployed.add(cleaned)
                if f.endswith("/"):
                    deployed_dir_prefixes.append(cleaned + "/")

    dir_prefix_tuple = tuple(deployed_dir_prefixes)

    details: list[str] = []
    unmanaged_count = 0
    files_scanned = 0
    cap_hit = False
    for gov_dir in dirs:
        dir_path = project_root / gov_dir
        if not dir_path.exists() or not dir_path.is_dir():
            continue
        # os.walk(followlinks=False) never recurses INTO a directory symlink, so
        # a symlinked dir resolving outside the workspace is never traversed
        # (the house pattern from security/gate.py). File symlinks still appear
        # in the listing and are guarded individually below.
        for dirpath, _subdirs, filenames in os.walk(dir_path, followlinks=False):
            for fname in filenames:
                file_path = Path(dirpath) / fname
                # File-symlink guard: never follow a link out of the workspace.
                if file_path.is_symlink() and _symlink_escapes_workspace(file_path, project_root):
                    continue
                if file_path.is_dir():  # a FIFO or other special file is counted
                    continue
                files_scanned += 1
                if files_scanned > _MAX_UNMANAGED_SCAN_FILES:
                    cap_hit = True
                    break
                rel = file_path.relative_to(project_root).as_posix()
                if (rel in deployed or rel.startswith(dir_prefix_tuple)) and file_path.is_file():
                    continue
                if first_matching_pattern(rel, exclude) is not None:
                    continue
                primitive_type = _classify_primitive_type(rel)
                deny_hit = _unmanaged_deny_conflict(rel, dependency_deny, mcp_deny)
                details.append(_format_unmanaged_detail(rel, primitive_type, deny_hit))
                unmanaged_count += 1
            if cap_hit:
                break
        if cap_hit:
            break

    if cap_hit:
        return CheckResult(
            name="unmanaged-files",
            passed=True,
            message=(
                f"Scan capped at {_MAX_UNMANAGED_SCAN_FILES:,} files "
                "-- skipping unmanaged-files check"
            ),
            details=[
                f"Governance directories contain > {_MAX_UNMANAGED_SCAN_FILES:,} files; "
                "consider adding exclude patterns in the unmanaged_files policy"
            ],
        )

    if not details:
        return CheckResult(
            name="unmanaged-files",
            passed=True,
            message="No unmanaged files in governance directories",
        )

    # One report carries a single next-action hint after the per-file lines.
    details.append(_UNMANAGED_NEXT_ACTION)

    if policy.effective_action == "warn":
        return CheckResult(
            name="unmanaged-files",
            passed=True,
            message=f"{unmanaged_count} unmanaged file(s) found (warn)",
            details=details,
        )

    # action == "deny"
    return CheckResult(
        name="unmanaged-files",
        passed=False,
        message=f"{unmanaged_count} unmanaged file(s) in governance directories",
        details=details,
    )


def _check_registry_source(
    deps: list[DependencyReference],
    policy: RegistrySourcePolicy,
    registries_map: dict[str, str] | None,
    lockfile=None,
) -> CheckResult:
    """Check registry source policy (require / allow_non_registry).

    Fail-closed when a required registry name has no URL configured in
    *registries_map* — that means the registry source is unreachable by
    definition and the install must not proceed.
    """
    check_name = "registry-source"
    no_op = not policy.require and policy.allow_non_registry
    if no_op:
        return CheckResult(name=check_name, passed=True, message="No registry source policy")

    violations: list[str] = []
    subjects: list[PolicySubject] = []

    # Fail-closed: required registry names must be configured.
    for req_name in policy.require:
        if not registries_map or req_name not in registries_map:
            violations.append(
                f"required registry '{req_name}' is not configured -- "
                "add it to the 'registries:' block or via 'apm config set registry."
                f"{req_name}.url <url>'"
            )

    for dep in deps:
        key = dep.get_canonical_dependency_string()
        is_registry = getattr(dep, "source", None) == "registry"
        registry_name = getattr(dep, "registry_name", None)
        if is_registry and registry_name is None and lockfile is not None:
            # A replayed row (for example a registry-routed catalog entry,
            # #4009) names no registry; judge it by the configured registry
            # that owns its locked URL, as its download does.
            from ..deps.registry.auth import dependency_ref_with_registry_name_from_lockfile

            registry_name = dependency_ref_with_registry_name_from_lockfile(
                dep, registries_map or {}, existing_lockfile=lockfile
            ).registry_name

        reason = None
        if not policy.allow_non_registry and not is_registry:
            reason = "non-registry source not permitted (policy requires registry sources only)"
        elif policy.require and is_registry and registry_name not in policy.require:
            reason = (
                f"sourced from registry '{registry_name}' "
                f"but policy requires one of {sorted(policy.require)}"
            )
        if reason is not None:
            violations.append(f"{key}: {reason}")
            subjects.append(PolicySubject(key, reason, remedy=None))

    if violations:
        # A missing registry is a configuration gap with no dependency to
        # name: leave ``subjects`` empty so the whole check is reported.
        return CheckResult(
            name=check_name,
            passed=False,
            message=f"{len(violations)} registry source violation(s)",
            details=violations,
            subjects=tuple(subjects) if len(subjects) == len(violations) else (),
        )
    return CheckResult(
        name=check_name,
        passed=True,
        message="All dependencies satisfy registry source policy",
    )


class MarketplaceOrigins(NamedTuple):
    """Marketplace catalog origins ``marketplace.allow`` judges (#2903).

    *declared* holds the root ``marketplaces:`` entries; *resolved* holds
    ``(label, catalog origin)`` per marketplace-resolved dependency
    (:func:`marketplace_resolution_origins`).
    """

    declared: Sequence | None = None
    resolved: Sequence[tuple[str, str]] = ()


def marketplace_resolution_origins(
    lockfile,
    *,
    keys: Iterable[str] | None = None,
    provenance: Mapping[str, Mapping[str, str]] | None = None,
) -> tuple[tuple[str, str], ...]:
    """Return ``(label, catalog origin)`` for each marketplace-resolved dependency.

    Reads the lock rows' ``source_url`` and, for an install, the fresh
    resolutions in *provenance* (which replace the row they re-resolve).
    *keys* limits the set to the dependencies being installed; ``None``
    means every lock row (``apm audit --ci``). An empty origin means none
    was recorded (a local catalog or an older lock).
    """
    wanted = None if keys is None else set(keys)
    origins: dict[str, tuple[str, str]] = {}

    def _record(key: str, via: str | None, plugin: str | None, origin: str | None) -> None:
        if via and plugin and (wanted is None or key in wanted):
            origins[key] = (f"{plugin}@{via} ({key})", origin or "")

    for key, dep in getattr(lockfile, "dependencies", {}).items():
        _record(key, dep.discovered_via, dep.marketplace_plugin_name, dep.source_url)
    for key, prov in (provenance or {}).items():
        _record(
            key,
            prov.get("discovered_via"),
            prov.get("marketplace_plugin_name"),
            prov.get("source_url"),
        )
    return tuple(origins[key] for key in sorted(origins))


def _check_marketplace_allow(origins: MarketplaceOrigins, policy: MarketplacePolicy) -> CheckResult:
    """Check marketplace catalog origins against ``marketplace.allow`` (#2903).

    Covers the root ``marketplaces:`` entries and the origins resolved
    marketplace dependencies were taken from (lock rows and fresh
    resolutions, :func:`marketplace_resolution_origins`), so a committed
    lock cannot install from a catalog the list does not name. A local
    catalog, an unparseable source and a row with no recorded origin are
    refused: an allow-list must never pass an origin it cannot name.
    """
    from ..core.remediation import MARKETPLACE_ALLOW_READD
    from ..marketplace.project_marketplaces import declared_marketplace_origin
    from ..marketplace.source_identity import marketplace_policy_subject
    from .matcher import first_matching_pattern

    check_name = "marketplace-allow"
    patterns = tuple(marketplace_policy_subject(p) for p in policy.allow or ())

    def _disallowed(origin: str) -> str | None:
        subject = marketplace_policy_subject(origin) if origin else ""
        if subject and first_matching_pattern(subject, patterns) is not None:
            return None
        return subject

    subjects: list[PolicySubject] = []
    for entry in origins.declared or ():
        try:
            origin = declared_marketplace_origin(entry)
        except ValueError:
            origin = ""
        subject = _disallowed(origin)
        if subject is None:
            continue
        reason = (
            f"origin '{subject or entry.source}' is not in the org policy marketplace.allow "
            "list; remove it from 'marketplaces:' in apm.yml or ask your org admin to allow it"
        )
        subjects.append(PolicySubject(f"marketplace '{entry.name}'", reason, remedy=None))
    for label, origin in origins.resolved:
        subject = _disallowed(origin)
        if subject is None:
            continue
        # The lock carries a package's catalog provenance forward on every
        # write, so only dropping its apm.yml entry (which prunes the row)
        # and adding it back clears it; 'apm update' never does.
        problem = (
            f"resolved from catalog '{subject}', which is not in the org policy "
            "marketplace.allow list"
            if subject
            else "has no recorded catalog origin, so the org policy marketplace.allow list "
            "cannot admit it"
        )
        reason = f"{problem}; {MARKETPLACE_ALLOW_READD.clause()}, or ask your org admin to allow the catalog"
        subjects.append(PolicySubject(label, reason, remedy=None))
    if subjects:
        return CheckResult(
            name=check_name,
            passed=False,
            message=f"{len(subjects)} marketplace origin(s) not allowed by org policy",
            details=[f"{s.ref}: {s.reason}" for s in subjects],
            subjects=tuple(subjects),
        )
    return CheckResult(name=check_name, passed=True, message="All marketplace origins are allowed")


def _check_pinned_constraints(
    deps: list[DependencyReference],
    policy: DependencyPolicy,
    direct_dep_keys: set[str] | None = None,
    declared_refs: Mapping[str, Sequence[DependencyReference]] | None = None,
) -> CheckResult:
    """Check: every direct dep declares a bounded constraint.

    Skipped (passes vacuously) when
    ``policy.require_pinned_constraint`` is ``False`` -- the default.

    Operates on the **declared** constraint, not the resolved one, so
    authors learn before the install completes that a moving ref
    slipped past review. After the resolve phase ``dep.reference``
    already holds the selected tag, so the install-pipeline callers
    pass ``declared_refs`` (unique key -> a snapshot of every declaration
    of it, see ``collect_declared_refs``) and the check classifies each
    of those instead (#3224); one bounded declaration never covers for an
    unbounded one sharing the key. ``apm audit`` passes the same
    snapshot (``collect_manifest_declarations``); install preflight already
    receives source-selected manifest deps and leaves it ``None``.

    When ``direct_dep_keys`` is provided, the check is restricted to
    direct dependencies only -- transitives are excluded, since the
    consumer cannot rewrite a constraint declared in a transitive
    package's own manifest. Callers that have direct-vs-transitive
    context (the install pipeline gate, the target-aware re-check,
    and the install preflight) should always pass it. When ``None``
    (legacy dep-only seam, or the audit wrapper that already iterates
    direct-only manifest deps) the check falls back to evaluating
    every dep in ``deps``.

    See ``_constraint_pinning.py`` for classification rules.
    """
    from ._constraint_pinning import classify_unbounded_reason, humanize_reason

    check_name = "dependency-pinned-constraint"
    if not policy.require_pinned_constraint:
        return CheckResult(
            name=check_name,
            passed=True,
            message="Pinned-constraint requirement disabled",
        )

    violations: list[PolicySubject] = []
    for dep in deps:
        unique_key = dep.get_unique_key()
        if direct_dep_keys is not None and unique_key not in direct_dep_keys:
            continue
        declared = declared_refs.get(unique_key) if declared_refs else None
        for judged in declared or (dep,):
            reason = classify_unbounded_reason(judged)
            if reason is None:
                continue
            violations.append(
                PolicySubject(
                    judged.get_canonical_dependency_string(),
                    humanize_reason(reason, judged),
                    remedy=PolicyRemedy.PIN,
                )
            )
    violations = list(dict.fromkeys(violations))

    if not violations:
        return CheckResult(
            name=check_name,
            passed=True,
            message="All dependencies use pinned constraints",
        )

    return _subjects_result(
        check_name,
        f"{len(violations)} dependency(ies) use unbounded constraints "
        "(hint: pin to a semver range, literal tag, or SHA)",
        violations,
    )


def _check_dependency_content_hashes(
    lockfile,
    policy: IntegrityPolicy,
) -> CheckResult:
    """Check that persisted non-local dependencies carry required hashes.

    The integrity decision remains owned by
    :func:`apm_cli.install.integrity.enforce_require_hashes`; this adapter only
    translates its fail-closed result into the public audit check vocabulary.
    A missing lockfile is handled by the baseline ``lockfile-exists`` check, and
    a project with no lockfile dependencies satisfies this rule vacuously.
    """
    check_name = "dependency-content-hashes"
    from apm_cli.install.integrity import (
        enforce_require_hashes,
        require_hashes_enabled,
    )

    if not require_hashes_enabled(policy):
        return CheckResult(
            name=check_name,
            passed=True,
            message="Dependency content hashes not required by policy",
        )
    if lockfile is None:
        return CheckResult(
            name=check_name,
            passed=True,
            message=("No lockfile available -- see lockfile-exists for required remediation"),
        )

    try:
        enforce_require_hashes(
            lockfile.get_package_dependencies(),
            enabled=True,
        )
    except RuntimeError as exc:
        from apm_cli.install.integrity import unhashed_dependencies, unhashed_dependency_names

        names = unhashed_dependency_names(
            unhashed_dependencies(lockfile.get_package_dependencies())
        )
        return CheckResult(
            name=check_name,
            passed=False,
            message=(
                f"Locked dependencies are missing required content hashes ({names}) -- "
                f"{CONTENT_HASHES_MISSING.clause()}"
            ),
            details=[str(exc)],
        )
    return CheckResult(
        name=check_name,
        passed=True,
        message="All locked dependencies carry required content hashes",
    )


# -- Aggregate runners ---------------------------------------------


def run_dependency_deny_check(
    deps: Iterable[DependencyReference],
    policy: ApmPolicy,
    bundled_deps: Iterable[DependencyReference] = (),
) -> CIAuditResult:
    """Judge *deps* and *bundled_deps* against ``dependencies.deny`` alone.

    For a command that ships content rather than installing it (``apm pack``):
    allow, require and pin rules judge what a project installs, not what it
    may hand on.
    """
    result = CIAuditResult()
    result.checks.append(
        _check_dependency_denylist(list(deps), policy.dependencies, list(bundled_deps))
    )
    return result


def run_dependency_policy_checks(  # noqa: PLR0913
    deps_to_install,
    *,
    lockfile=None,
    policy: ApmPolicy,
    mcp_deps=None,
    mcp_brought_in_by: Mapping[str, str] | None = None,
    effective_target: tuple[str, ...] | None = None,
    effective_target_source: str = "effective target",
    fetch_outcome: str | None = None,
    fail_fast: bool = True,
    manifest_includes=_INCLUDES_NOT_PROVIDED,
    registries: dict[str, str] | None = None,
    direct_dep_keys: set[str] | None = None,
    source_deps: Iterable[DependencyReference] | None = None,
    declared_refs: Mapping[str, Sequence[DependencyReference]] | None = None,
    installing: bool = False,
    marketplace_origins: MarketplaceOrigins | None = None,
    bundled_deps: Iterable[DependencyReference] | LocalBundleSubjects = (),
    depth_lockfile: LockFile | None = None,
) -> CIAuditResult:
    """Evaluate :class:`ApmPolicy` against an already-resolved dependency set.

    Used by both ``apm audit --ci`` (after resolving from disk) and the
    install pipeline ``policy_gate`` phase.  Reuses the private ``_check_*``
    helpers -- no logic duplication.

    Parameters
    ----------
    deps_to_install:
        Iterable of ``DependencyReference`` (the resolved set, including
        transitives).  This is what ``InstallContext.deps_to_install``
        contains after the resolve phase.
    lockfile:
        An ``ApmLockfile`` / ``LockFile`` instance, or ``None``.  Needed
        for deployed-files and version-pin checks.
    depth_lockfile:
        Rows of the graph an install resolved; ``dependencies.max_depth``
        judges these instead of *lockfile* when given.
    policy:
        The effective :class:`ApmPolicy` to enforce.
    mcp_deps:
        Iterable of ``MCPDependency`` objects, or ``None``.  When the
        resolved set includes MCP entries they are checked against
        ``policy.mcp``.
    mcp_brought_in_by:
        Transitive MCP server name -> key of the direct apm.yml dependency
        that brings it in (``CurrentMcpConfigView.brought_in_by``). Removal
        hints for transitive servers name that entry; servers absent from
        the map get no apm.yml edit hint.
    effective_target:
        The ``EffectiveTargetDecision.canonical_targets`` projection of the
        resolved target selection, or ``None``. When ``None``
        target/compilation checks are **skipped** (they belong to the
        separate W2-target-aware call).
    effective_target_source:
        Where the target selection came from (``apm.yml``, ``--target flag``,
        ...), named in compilation-target violation details.
    fetch_outcome:
        Human-readable label for diagnostic context (e.g.
        ``"cached"``, ``"fetched"``).  Currently informational only.
    fail_fast:
        Stop after the first failing check (default ``True``).
    manifest_includes:
        The parsed value of the manifest's ``includes:`` field
        (``None``, ``"auto"``, or a list of paths).  When omitted,
        the ``explicit-includes`` check is skipped -- callers that
        do not have manifest information available (e.g. dep-only
        seams) can leave it unset.
    direct_dep_keys:
        Optional set of ``DependencyReference.get_unique_key()`` for
        the direct (manifest-declared) deps. When supplied, the
        ``require_pinned_constraint`` check only evaluates direct
        deps -- transitive entries are excluded because the consumer
        cannot rewrite a constraint declared inside a transitive
        package's own manifest. When ``None`` (legacy dep-only seam
        and the audit wrapper that already iterates direct-only
        manifest deps) every dep in ``deps_to_install`` is evaluated.
    source_deps:
        Optional dependency set for the allow/deny source checks. Defaults
        to ``deps_to_install``. The audit wrapper passes effective direct deps
        (production, dev, and ``replace``-selected sources) plus locked
        transitive deps so every installed source is gated.
    declared_refs:
        Optional map of ``get_unique_key()`` -> a snapshot of every
        declaration of that direct dep (``collect_declared_refs``). The
        resolve phase rewrites ``dep.reference`` to the selected tag, so
        the install-pipeline callers pass the declarations they captured
        beforehand and ``require_pinned_constraint`` judges the authored
        constraint rather than the selected tag (#3224).
    installing:
        ``True`` for the install gate, which is about to lock
        *deps_to_install*: a declared required package missing only from
        the previous lockfile does not block the install that records it.
    marketplace_origins:
        The root manifest's ``marketplaces:`` entries and the catalog
        origins of marketplace-resolved dependencies (lock rows and fresh
        resolutions), checked against ``marketplace.allow`` when the policy
        sets it, so a lock cannot bypass it.
    bundled_deps:
        Packages shipped without an apm.yml entry in this project. A plain
        iterable meets ``dependencies.deny`` only. A local bundle's
        :class:`LocalBundleSubjects` meets deny with its name and packages,
        and ``dependencies.allow`` with its packages alone (#4646). Require
        and pin checks judge the project's own declarations.

    Returns
    -------
    CIAuditResult
        Contains individual :class:`CheckResult` entries.  The caller
        decides how to map ``enforcement`` level (block vs warn) onto
        these results.

    Notes
    -----
    ``require_resolution: project-wins`` semantics (rubber-duck I7):
    version-pin mismatches are downgraded to warnings; missing required
    packages still block; inherited org deny still wins.  This is
    handled inside ``_check_required_package_version`` which already
    reads ``policy.dependencies.require_resolution``.

    Does **not** load ``apm.yml`` from disk -- the caller supplies the
    resolved dep set directly.
    """
    result = CIAuditResult()
    deps_list = list(deps_to_install)
    source_list = deps_list if source_deps is None else list(source_deps)
    mcp_list = list(mcp_deps) if mcp_deps is not None else []

    def _run(check: CheckResult) -> bool:
        """Append check and return True if fail-fast should stop."""
        result.checks.append(check)
        return fail_fast and not check.passed

    # -- Dependency checks (1-6) -----------------------------------
    bundle = bundled_deps if isinstance(bundled_deps, LocalBundleSubjects) else None
    deny_bundled = [*bundle.names, *bundle.packages] if bundle else list(bundled_deps)
    if _run(_check_dependency_allowlist(source_list, policy.dependencies, bundle)):
        return result
    if _run(_check_dependency_denylist(source_list, policy.dependencies, deny_bundled)):
        return result
    dep_names = None
    if policy.dependencies.effective_require or (
        policy.executables.require and lockfile is not None
    ):
        dep_names = _required_policy_index(deps_list, None)
    if _run(_check_required_packages(deps_list, policy.dependencies, dep_names=dep_names)):
        return result
    if _run(
        _check_required_packages_deployed(
            deps_list, lockfile, policy.dependencies, dep_names=dep_names, installing=installing
        )
    ):
        return result
    if _run(
        _check_required_executable_untrusted(
            deps_list, lockfile, policy.executables, dep_names=dep_names
        )
    ):
        return result
    if _run(
        _check_required_package_version(
            deps_list, lockfile, policy.dependencies, dep_names=dep_names
        )
    ):
        return result
    # An install judges the graph it resolved, not the lockfile it replaces (#4826).
    depth_lock = depth_lockfile if depth_lockfile is not None else lockfile
    if _run(_check_transitive_depth(depth_lock, policy.dependencies)):
        return result

    # -- Registry source + pinned-constraint + MCP + tail checks -----
    # Collect all remaining checks into a single loop so the function
    # stays within the max-returns threshold.
    remaining_checks: list[CheckResult] = [
        _check_registry_source(deps_list, policy.registry_source, registries, lockfile),
        # Pinned-constraint: property check on declared refs. Cheap
        # (O(N) string classification, no I/O) so it always runs.
        # When direct_dep_keys is supplied, restrict to direct deps -- a
        # transitive package with an unbounded ref in its own manifest is
        # not actionable by the consumer (see Copilot review on #1494).
        _check_pinned_constraints(
            deps_list, policy.dependencies, direct_dep_keys, declared_refs=declared_refs
        ),
    ]
    if policy.marketplace.allow is not None and marketplace_origins is not None:
        remaining_checks.append(_check_marketplace_allow(marketplace_origins, policy.marketplace))
    # MCP checks -- when mcp_deps is None (not provided), skip entirely.
    # When mcp_deps is an empty list (provided but no MCP deps), still
    # run MCP checks so they report "no X configured" for completeness.
    if mcp_deps is not None:
        mcp_checks = mcp_policy_checks(mcp_list, policy)
        remaining_checks += _with_mcp_declarers(mcp_checks, mcp_list, mcp_brought_in_by)
    # Target / compilation + manifest tail checks.
    if effective_target is not None:
        remaining_checks.append(
            _check_compilation_target(
                effective_target,
                policy.compilation,
                source=effective_target_source,
            )
        )
    if manifest_includes is not _INCLUDES_NOT_PROVIDED:
        remaining_checks.append(_check_includes_explicit(manifest_includes, policy.manifest))
    for check in remaining_checks:
        if _run(check):
            return result

    # NOTE: compilation strategy, source attribution, manifest fields,
    # scripts policy, and unmanaged files are disk-level / manifest-level
    # concerns.  They are NOT included in the resolved-dep seam because
    # the install pipeline does not have the raw manifest at this point
    # and they are already covered by the full ``run_policy_checks``
    # wrapper that ``apm audit --ci`` calls.

    return result


def _audit_source_dependencies(
    manifest: APMPackage, lock: LockFile | None
) -> list[DependencyReference]:
    """Return effective direct deps plus locked transitive deps for source checks.

    Direct production and dev declarations are projected through the
    manifest's ``replace`` rules, so the selected source (not the original)
    is gated, as install does.  Raises ``ValueError`` when the replacement
    table cannot be bound.
    """
    from ..models.dependency.replacement import project_manifest_dependencies

    deps = list(project_manifest_dependencies(manifest))
    if lock is None:
        return deps
    seen = {dep.get_unique_key() for dep in deps}
    for locked in lock.get_package_dependencies():
        if locked.depth <= 1 or locked.get_unique_key() in seen:
            continue
        seen.add(locked.get_unique_key())
        deps.append(locked.to_dependency_ref())
    return deps


def run_policy_checks(
    project_root: Path,
    policy: ApmPolicy,
    *,
    fail_fast: bool = True,
    modules_root: Path | None = None,
    no_policy: bool = False,
    policy_source: str | None = None,
) -> CIAuditResult:
    """Run the full set of policy checks against a project on disk.

    Thin wrapper: loads manifest + lockfile from *project_root*, resolves
    deps, and delegates dependency/MCP checks to
    :func:`run_dependency_policy_checks`.  Then appends the disk-level
    checks (compilation, manifest, unmanaged files) that require the raw
    ``apm.yml``.

    *modules_root* and *no_policy* select the same MCP view the baseline
    config-consistency check reads (the CI scratch replay, when prepared).

    These checks are ADDED to baseline checks -- caller runs both.
    When *fail_fast* is ``True`` (default), stops after the first
    failing check.
    Returns :class:`CIAuditResult` with individual check results.
    """
    from ..deps.lockfile import LockFile, get_lockfile_path
    from ..models.dependency.replacement import project_manifest_dependencies
    from ._shared import _parse_apm_yml_safe

    result = CIAuditResult()

    # Load manifest
    apm_yml_path = manifest_file(project_root)
    if not apm_yml_path.exists():
        return result

    manifest = _parse_apm_yml_safe(apm_yml_path, result)
    if manifest is None:
        return result

    # Load lockfile (optional -- some checks work without it)
    lockfile_path = get_lockfile_path(project_root)
    lock = LockFile.read(lockfile_path) if lockfile_path.exists() else None

    # Load raw YAML for field-level checks
    raw_yml = _load_raw_apm_yml(project_root)

    # Every MCP server the lock keeps as baseline deploys (root, transitive and
    # recorded local-bundle servers), so policy judges the audit view's set,
    # as the install preflight judges the merged set.
    mcp_brought_in_by = None
    trust_check: CheckResult | None = None
    if lock is None:
        mcp_deps = manifest.get_all_mcp_dependencies()
    else:
        from .ci_checks import audit_mcp_view

        view, _admission = audit_mcp_view(
            manifest, lock, modules_root=modules_root, no_policy=no_policy
        )
        mcp_deps = list(view.kept_dependencies())
        mcp_brought_in_by = view.brought_in_by
        from ..claude_plugins.admission import locked_transitive_native_refusals
        from ..codex_plugins.admission import locked_transitive_codex_refusals

        modules = modules_root or project_root / "apm_modules"
        native = [
            *locked_transitive_native_refusals(lock, modules, policy),
            *locked_transitive_codex_refusals(lock, modules, policy),
        ]
        trust_check = _parked_trust_transitive(view, native)

    try:
        source_deps = _audit_source_dependencies(manifest, lock)
        # Production declarations through ``replace:``, as the install gate
        # judges them: required-package matching follows source replacement.
        apm_deps = project_manifest_dependencies(manifest, include_dev=False)
    except ValueError as exc:
        result.checks.append(
            CheckResult(
                name="dependency-sources",
                passed=False,
                message=f"Cannot resolve dependency sources from apm.yml: {exc}",
            )
        )
        return result

    # -- Delegate dependency + MCP checks to shared seam ---------------
    from ._constraint_pinning import collect_manifest_declarations

    dep_result = run_dependency_policy_checks(
        apm_deps,
        lockfile=lock,
        policy=policy,
        mcp_deps=mcp_deps,
        mcp_brought_in_by=mcp_brought_in_by,
        # effective_target=None: the manifest target check runs below.
        fail_fast=fail_fast,
        manifest_includes=manifest.includes,
        registries=getattr(manifest, "registries", None),
        source_deps=source_deps,
        # Judge the same source-selected declarations as the install gate.
        declared_refs=collect_manifest_declarations(manifest),
        marketplace_origins=MarketplaceOrigins(
            manifest.marketplaces, marketplace_resolution_origins(lock)
        ),
    )
    inputs = (project_root, manifest, apm_deps, lock, raw_yml)
    result.checks.extend(_advise(dep_result.checks, source_deps, policy, policy_source, inputs))

    # Early exit if dep checks already failed in fail-fast mode
    if fail_fast and not dep_result.passed:
        return result
    if trust_check is not None:
        result.checks.append(trust_check)
        if fail_fast and not trust_check.passed:
            return result

    hash_check = _check_dependency_content_hashes(
        lock,
        policy.security.integrity,
    )
    result.checks.append(hash_check)
    if fail_fast and not hash_check.passed:
        return result

    def _run(check: CheckResult) -> bool:
        """Append check and return True if fail-fast should stop."""
        result.checks.append(check)
        return fail_fast and not check.passed

    # -- Disk-level checks that only apply to full-project audits --

    # Compilation checks (11-13). Audit judges the targets the manifest
    # declares, through the same projection the install gate uses.
    from ..core.target_detection import EffectiveTargetDecision
    from ..models.apm_package import package_target_selection

    manifest_decision = EffectiveTargetDecision(package_target_selection(manifest), "apm.yml")
    if (
        _run(
            _check_compilation_target(
                manifest_decision.canonical_targets,
                policy.compilation,
                source=manifest_decision.source,
            )
        )
        or _run(_check_compilation_strategy(raw_yml, policy.compilation))
        or _run(_check_source_attribution(raw_yml, policy.compilation))
    ):
        return result

    # Manifest checks (14-15)
    if _run(_check_required_manifest_fields(raw_yml, policy.manifest)):
        return result
    if lock is not None and policy.manifest.content_types is not None:
        from .content_types_check import audit_content_types

        if _run(audit_content_types(lock, policy.manifest, project_root, modules_root)):
            return result
    if _run(_check_scripts_policy(raw_yml, policy.manifest)):
        return result

    # Unmanaged files check (16)
    _run(
        _check_unmanaged_files(
            project_root,
            lock,
            policy.unmanaged_files,
            dependency_deny=policy.dependencies.effective_deny,
            mcp_deny=policy.mcp.deny,
        )
    )

    return result
