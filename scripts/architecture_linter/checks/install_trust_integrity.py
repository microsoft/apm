"""Trust and integrity consistency guards (#4027).

* ``resolve_trust_policy`` in ``policy/outcome_routing.py`` is the one
  policy-outcome owner for executables-trust readers. Only registered,
  outcome-consuming call sites reach ``discover_policy_with_chain``; a new
  caller that reads ``.policy`` and drops the outcome fails closed here.
* ``apm update`` keeps the content-hash check for a dependency whose
  resolved commit did not move.
* Every registry download routes through ``download_registry_package``,
  which verifies an unchanged registry version against its locked hash
  (#4125).
* ``apm unpack`` refuses executable content before it writes anything.
* ``apm install -g <bundle>`` keeps its lock, approvals and gate config
  under ``APM_HOME`` (``core.scope``), never the home directory.
* Every bundle deploy entry point admits the bundle through
  ``install/bundle_admission.py`` (org policy and integrity) and records what
  it deployed through the shared local-bundle lockfile owner (#4080).
"""

from __future__ import annotations

import re

from scripts.architecture_linter.checks.install_deployment_shared import (
    _facts_for,
    _first_line_re,
    _lines,
    _present,
    _python_paths,
    _summary,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import violation
from scripts.architecture_linter.models import Violation

GUARD_TRUST_POLICY_OUTCOME = "install-deployment-trust-policy-outcome"
GUARD_UPDATE_CONTENT_INTEGRITY = "install-deployment-update-content-integrity"
GUARD_UPDATE_MARKETPLACE_PROVENANCE = "install-deployment-update-marketplace-provenance"
GUARD_UPDATE_REGISTRY_INTEGRITY = "install-deployment-update-registry-integrity"
GUARD_UNPACK_EXECUTABLES = "install-deployment-unpack-executable-refusal"
GUARD_BUNDLE_USER_SCOPE_ROOT = "install-deployment-bundle-user-scope-root"
GUARD_LOCAL_BUNDLE_HOOK_TRUST = "install-deployment-local-bundle-hook-trust"
GUARD_BUNDLE_ADMISSION = "install-deployment-bundle-admission-owner"

_OWNER = "src/apm_cli/policy/outcome_routing.py"
_EXECUTABLES = "src/apm_cli/security/executables.py"
_EXEC_GATE = "src/apm_cli/install/exec_gate.py"
_LOCAL_BUNDLE = "src/apm_cli/install/local_bundle_handler.py"
_SOURCES = "src/apm_cli/install/sources.py"
_INTEGRATE = "src/apm_cli/install/phases/integrate.py"
_RESOLVE = "src/apm_cli/install/phases/resolve.py"
_HELPERS = "src/apm_cli/install/source_helpers.py"
_UNPACKER = "src/apm_cli/bundle/unpacker.py"
_INSTALL_COMMAND = "src/apm_cli/commands/install.py"
_HOOK_OWNERSHIP = "src/apm_cli/integration/hook_ownership.py"
_HOOK_INTEGRATOR = "src/apm_cli/integration/hook_integrator.py"
_BUNDLE_HOOK_TRUST = "src/apm_cli/install/local_bundle_hook_trust.py"
_CI_CHECKS = "src/apm_cli/policy/ci_checks.py"

_DISCOVERY = "discover_policy_with_chain"
# Every remaining direct discovery call consumes the outcome itself: the
# install gate and preflight route it, audit/policy commands report it,
# lifecycle scripts fail toward the gate. Trust readers, approve, update
# and MCP admission are not here; they ask the routing owner.
_DISCOVERY_CALLERS = frozenset(
    {
        (_OWNER, "discover_trust_policy"),
        ("src/apm_cli/policy/install_preflight.py", "run_policy_preflight"),
        ("src/apm_cli/install/phases/policy_gate.py", "_discover_with_chain"),
        ("src/apm_cli/commands/policy.py", "status"),
        ("src/apm_cli/commands/audit.py", "_audit_ci_gate"),
        ("src/apm_cli/commands/audit.py", "_resolve_fail_on_drift"),
        ("src/apm_cli/core/lifecycle_scripts.py", "build_runner_from_context"),
    }
)
_TRUST_READERS: dict[str, str] = {
    _EXECUTABLES: (
        "manifest_root(project_root, user_scope=user_scope), no_policy=no_policy, logger=logger"
    ),
    _EXEC_GATE: "manifest_root(self._project_root, user_scope=self._user_scope), announce=False",
    _LOCAL_BUNDLE: "policy = resolve_trust_policy(",
    "src/apm_cli/commands/approve.py": "policy = resolve_trust_policy(project_root, logger=logger)",
    # The ``apm audit --ci`` drift replay reuses the run's one discovery.
    "src/apm_cli/commands/audit.py": (
        "return resolve_trust_policy(cfg.project_root, announce=False, fetch_result=fetch_policy())"
    ),
}


def check_trust_policy_outcome(provider: FactsProvider) -> tuple[Violation, ...]:
    """Trust readers route policy outcomes through one owner."""
    findings: list[Violation] = []
    facts, failures = _facts_for(provider, _OWNER, GUARD_TRUST_POLICY_OUTCOME)
    findings.extend(failures)
    if not failures:
        for needle in (
            "def resolve_trust_policy(",
            "fetch_failure_default=read_project_fetch_failure_default(root),",
        ):
            if not _present(facts, needle):
                findings.append(
                    _summary(
                        GUARD_TRUST_POLICY_OUTCOME,
                        _OWNER,
                        f"The trust policy owner must route the outcome; missing {needle}",
                    )
                )
    for path, needle in _TRUST_READERS.items():
        facts, failures = _facts_for(provider, path, GUARD_TRUST_POLICY_OUTCOME)
        findings.extend(failures)
        if not failures and not _present(facts, needle):
            findings.append(
                _summary(
                    GUARD_TRUST_POLICY_OUTCOME,
                    path,
                    "Executables-trust readers must ask resolve_trust_policy for org policy",
                )
            )
    facts, failures = _facts_for(provider, _LOCAL_BUNDLE, GUARD_TRUST_POLICY_OUTCOME)
    if not failures and _present(facts, "cache_only"):
        findings.append(
            _summary(
                GUARD_TRUST_POLICY_OUTCOME,
                _LOCAL_BUNDLE,
                "Local bundle installs resolve org policy as package installs do, never cache-only",
            )
        )
    for path in _python_paths(provider, "src/apm_cli/"):
        path_facts, path_failures = _facts_for(provider, path, GUARD_TRUST_POLICY_OUTCOME)
        if path_failures:
            continue
        findings.extend(
            violation(
                GUARD_TRUST_POLICY_OUTCOME,
                path,
                f"{call.scope} calls {_DISCOVERY} outside the registered outcome consumers; "
                "ask policy.outcome_routing.resolve_trust_policy instead",
                line=call.line,
            )
            for call in path_facts.calls
            if call.qualname.rsplit(".", 1)[-1] == _DISCOVERY
            and (path, call.scope) not in _DISCOVERY_CALLERS
        )
    return tuple(findings)


# One predicate decides whether the locked commit moved; every integrity
# check that ``apm update`` relaxes asks it.
_HELPER_NEEDLES = (
    (
        'return bool(commit not in (None, "", "cached") and commit == locked_commit)',
        "locked_commit_unchanged must compare real commits only",
    ),
    (
        "return bool(ctx.update_refs and not locked_commit_unchanged(locked_dep, resolved_commit))",
        "apm update may skip the content-hash check only for a moved commit",
    ),
    (
        "        update_accepts_new_content(ctx, locked_dep, resolved_commit)",
        "apm update may skip the carried-licence check only for a moved commit",
    ),
)


def check_update_content_integrity(provider: FactsProvider) -> tuple[Violation, ...]:
    """``apm update`` skips the content-hash check only for a moved commit."""
    facts, failures = _facts_for(provider, _SOURCES, GUARD_UPDATE_CONTENT_INTEGRITY)
    if failures:
        return tuple(failures)
    findings: list[Violation] = []
    if not _present(facts, "not update_accepts_new_content(ctx, dep_locked_chk, resolved_commit)"):
        findings.append(
            _summary(
                GUARD_UPDATE_CONTENT_INTEGRITY,
                _SOURCES,
                "The fresh-download hash check must ask update_accepts_new_content",
            )
        )
    helpers, helper_failures = _facts_for(provider, _HELPERS, GUARD_UPDATE_CONTENT_INTEGRITY)
    findings.extend(helper_failures)
    if not helper_failures:
        for needle, message in _HELPER_NEEDLES:
            if not _present(helpers, needle):
                findings.append(_summary(GUARD_UPDATE_CONTENT_INTEGRITY, _HELPERS, message))
    integrate, integrate_failures = _facts_for(provider, _INTEGRATE, GUARD_UPDATE_CONTENT_INTEGRITY)
    findings.extend(integrate_failures)
    if not integrate_failures and not (
        _present(integrate, "and not _locked_commit_unchanged")
        and _present(integrate, "and locked_commit_unchanged(_dep_locked_chk, _callback_sha)")
    ):
        findings.append(
            _summary(
                GUARD_UPDATE_CONTENT_INTEGRITY,
                _INTEGRATE,
                "A same-commit callback receipt must not stand in for the locked content hash",
            )
        )
    lines = _lines(facts)
    for number, line in enumerate(lines, start=1):
        if line.strip() == "not ctx.update_refs" and any(
            "dep_key not in _expected_hash_deps" in later for later in lines[number : number + 2]
        ):
            findings.append(
                violation(
                    GUARD_UPDATE_CONTENT_INTEGRITY,
                    _SOURCES,
                    "apm update must not skip the content-hash check for every dependency",
                    line=number,
                )
            )
    return tuple(findings)


# Registry identity is name plus exact version: one predicate decides whether
# the locked version moved, and one download owner verifies the locked hash.
_REGISTRY_HELPER_NEEDLES = (
    (
        'and version == getattr(locked_dep, "version", None)',
        "locked_version_unchanged must compare the exact locked registry version",
    ),
    (
        "or not locked_version_unchanged(locked_dep, version)",
        "download_registry_package must ask locked_version_unchanged",
    ),
    (
        'or expected.removeprefix("sha256:").lower() == actual.removeprefix("sha256:").lower()',
        "download_registry_package must compare the locked resolved_hash",
    ),
)
_REGISTRY_DOWNLOADERS = (_SOURCES, _RESOLVE)


def check_update_registry_integrity(provider: FactsProvider) -> tuple[Violation, ...]:
    """Registry downloads verify an unchanged version against its locked hash."""
    findings: list[Violation] = []
    helpers, failures = _facts_for(provider, _HELPERS, GUARD_UPDATE_REGISTRY_INTEGRITY)
    findings.extend(failures)
    if not failures:
        findings.extend(
            _summary(GUARD_UPDATE_REGISTRY_INTEGRITY, _HELPERS, message)
            for needle, message in _REGISTRY_HELPER_NEEDLES
            if not _present(helpers, needle)
        )
    for path in _REGISTRY_DOWNLOADERS:
        facts, path_failures = _facts_for(provider, path, GUARD_UPDATE_REGISTRY_INTEGRITY)
        findings.extend(path_failures)
        if not path_failures and not _present(facts, "download_registry_package("):
            findings.append(
                _summary(
                    GUARD_UPDATE_REGISTRY_INTEGRITY,
                    path,
                    "Registry downloads must route through download_registry_package",
                )
            )
    # Closed world: no registry resolver download outside the owner.
    for path in _python_paths(provider, "src/apm_cli/"):
        if path.startswith("src/apm_cli/deps/registry/"):
            continue
        path_facts, path_failures = _facts_for(provider, path, GUARD_UPDATE_REGISTRY_INTEGRITY)
        if path_failures:
            continue
        for call in path_facts.calls:
            receiver, _, name = call.qualname.rpartition(".")
            if name == "download_package" and "registry" in receiver.lower():
                findings.append(
                    violation(
                        GUARD_UPDATE_REGISTRY_INTEGRITY,
                        path,
                        f"{call.scope} downloads a registry package directly; "
                        "call install.source_helpers.download_registry_package",
                        line=call.line,
                    )
                )
    return tuple(findings)


def check_unpack_executable_refusal(provider: FactsProvider) -> tuple[Violation, ...]:
    """``apm unpack`` refuses executable content before any write."""
    facts, failures = _facts_for(provider, _UNPACKER, GUARD_UNPACK_EXECUTABLES)
    if failures:
        return tuple(failures)
    import re

    refusal = _first_line_re(
        facts, re.compile(r"_refuse_executable_content\(unique_files, source_dir, bundle_path\)")
    )
    first_exit = min(
        (
            line
            for line in (
                _first_line_re(facts, re.compile(r"^\s+if dry_run:")),
                _first_line_re(facts, re.compile(r"shutil\.copy")),
            )
            if line is not None
        ),
        default=None,
    )
    if refusal is None or (first_exit is not None and refusal > first_exit):
        return (
            _summary(
                GUARD_UNPACK_EXECUTABLES,
                _UNPACKER,
                "apm unpack must refuse executable content before a preview or a write",
            ),
        )
    return ()


def check_bundle_user_scope_root(provider: FactsProvider) -> tuple[Violation, ...]:
    """A ``-g`` local bundle reads and writes user metadata under ``APM_HOME``."""
    findings: list[Violation] = []
    facts, failures = _facts_for(provider, _LOCAL_BUNDLE, GUARD_BUNDLE_USER_SCOPE_ROOT)
    findings.extend(failures)
    if not failures:
        if not _present(
            facts, "return get_apm_dir(InstallScope.USER if user_scope else InstallScope.PROJECT)"
        ):
            findings.append(
                _summary(
                    GUARD_BUNDLE_USER_SCOPE_ROOT,
                    _LOCAL_BUNDLE,
                    "The bundle lock root must come from core.scope.get_apm_dir",
                )
            )
        for number, line in enumerate(_lines(facts), start=1):
            if (
                "get_lockfile_path(project_root)" in line
                or "migrate_lockfile_if_needed(project_root)" in line
            ):
                findings.append(
                    violation(
                        GUARD_BUNDLE_USER_SCOPE_ROOT,
                        _LOCAL_BUNDLE,
                        "A -g bundle lock lives under APM_HOME; use _bundle_lock_root",
                        line=number,
                    )
                )
    facts, failures = _facts_for(provider, _INSTALL_COMMAND, GUARD_BUNDLE_USER_SCOPE_ROOT)
    findings.extend(failures)
    if not failures and not _present(facts, "user_scope=global_,"):
        findings.append(
            _summary(
                GUARD_BUNDLE_USER_SCOPE_ROOT,
                _INSTALL_COMMAND,
                "apm install -g <bundle> must read grants from the user-scope manifest",
            )
        )
    return tuple(findings)


_BUNDLE_HOOK_TRUST_REQUIRED: dict[str, tuple[str, ...]] = {
    _HOOK_OWNERSHIP: (
        "def local_bundle_hook_source_marker(bundle_id: str, approval_key: str | None = None)",
        "def parse_local_bundle_hook_source(",
        "def write_per_file_bundle_record(",
    ),
    _HOOK_INTEGRATOR: (
        "local_bundle_hook_source_marker(bundle_id, package_info.local_bundle_approval)",
        "LocalBundleMarkers(bundle_id)",
    ),
    _BUNDLE_HOOK_TRUST: (
        "parse_local_bundle_hook_source(",
        "package_type_approved(allow_map, (key,), EXEC_TYPE_HOOKS)",
        "if None in keys:",
        "HookIntegrator().retire_local_bundle_hooks(item.slug, project_root, hook_targets)",
        "_retire_per_file(project_root, targets, item)",
        "_drop_lock_rows(lock_dir, retired_files)",
    ),
    _INSTALL_COMMAND: ("reconcile_scope_bundle_hooks(",),
    "src/apm_cli/commands/update.py": ("reconcile_scope_bundle_hooks(",),
    "src/apm_cli/commands/prune.py": ("reconcile_scope_bundle_hooks(",),
    _CI_CHECKS: (
        "unadmitted_bundle_hooks(deployment_root, targets, allow_provider, bundle_paths)",
    ),
    "src/apm_cli/install/local_bundle_hooks.py": (
        "package_info.local_bundle_approval = approval_key",
        "_record_per_file_approval(",
    ),
}
# The approval record is read and written only in hook_ownership.py.
_MARKER_TOKENS = ('"local-bundle::', "LOCAL_BUNDLE_HOOK_SOURCE_PREFIX")


def check_local_bundle_hook_trust(provider: FactsProvider) -> tuple[Violation, ...]:
    """Every command re-checks local-bundle hooks through one record and one owner."""
    findings: list[Violation] = []
    for path, needles in _BUNDLE_HOOK_TRUST_REQUIRED.items():
        facts, failures = _facts_for(provider, path, GUARD_LOCAL_BUNDLE_HOOK_TRUST)
        findings.extend(failures)
        if failures:
            continue
        missing = [needle for needle in needles if not _present(facts, needle)]
        if missing:
            findings.append(
                _summary(
                    GUARD_LOCAL_BUNDLE_HOOK_TRUST,
                    path,
                    "Local-bundle hook trust routing is missing: " + ", ".join(missing),
                )
            )
    for path in _python_paths(provider, "src/apm_cli/"):
        if path == _HOOK_OWNERSHIP:
            continue
        facts, failures = _facts_for(provider, path, GUARD_LOCAL_BUNDLE_HOOK_TRUST)
        if failures:
            continue
        findings.extend(
            violation(
                GUARD_LOCAL_BUNDLE_HOOK_TRUST,
                path,
                "Read or write a local-bundle hook approval record through "
                "integration.hook_ownership, never by hand",
                line=number,
            )
            for number, line in enumerate(_lines(facts), start=1)
            if any(token in line for token in _MARKER_TOKENS)
        )
    return tuple(findings)


_ADMISSION = "src/apm_cli/install/bundle_admission.py"
_PACK_COMMAND = "src/apm_cli/commands/pack.py"
# Calls that write bundle files into a deployment root.
_BUNDLE_DEPLOY_PRIMITIVES = frozenset({"integrate_local_bundle", "unpack_bundle"})
# Closed world: every function that deploys bundle files, with the admission
# call and the lockfile record call it must make.
_BUNDLE_ENTRY_POINTS: dict[tuple[str, str], tuple[str, ...]] = {
    (_LOCAL_BUNDLE, "install_local_bundle"): (
        "admit_bundle",
        "DeploymentLedgerCodec.record_local_bundle_files",
    ),
    (_PACK_COMMAND, "unpack_cmd"): ("apm_bundle_admission", "record_unpacked_files"),
}
# The owner's own routing: policy preflight and require_hashes in one place,
# and the unpack record through the shared local-bundle lockfile owner.
_ADMISSION_ROUTES: dict[str, tuple[str, ...]] = {
    "admit_bundle": ("run_policy_preflight", "require_hashes_enabled"),
    "admit": ("prepare_unpack_record", "admit_bundle", "_bundle_target_decision"),
    "prepare_unpack_record": ("ensure_lockfile_writable",),
    "_bundle_target_decision": ("pack_target_prefixes",),
    "record_unpacked_files": ("DeploymentLedgerCodec.record_local_bundle_files",),
}


def _scope_calls(facts: object, scope: str) -> set[str]:
    return {call.qualname for call in getattr(facts, "calls", ()) if call.scope == scope}


def check_bundle_admission_owner(provider: FactsProvider) -> tuple[Violation, ...]:
    """Every bundle deploy entry point routes through the bundle admission owner."""
    import re

    findings: list[Violation] = []
    for path in _python_paths(provider, "src/apm_cli/"):
        facts, failures = _facts_for(provider, path, GUARD_BUNDLE_ADMISSION)
        if failures:
            continue
        findings.extend(
            violation(
                GUARD_BUNDLE_ADMISSION,
                path,
                f"{call.scope} deploys bundle files outside the registered bundle "
                "entry points; admit the bundle through install/bundle_admission.py",
                line=call.line,
            )
            for call in facts.calls
            if call.qualname.rsplit(".", 1)[-1] in _BUNDLE_DEPLOY_PRIMITIVES
            and (path, call.scope) not in _BUNDLE_ENTRY_POINTS
        )
    for (path, function), required in _BUNDLE_ENTRY_POINTS.items():
        facts, failures = _facts_for(provider, path, GUARD_BUNDLE_ADMISSION)
        findings.extend(failures)
        if failures:
            continue
        missing = [name for name in required if name not in _scope_calls(facts, function)]
        if missing:
            findings.append(
                _summary(
                    GUARD_BUNDLE_ADMISSION,
                    path,
                    f"{function} must admit and record the bundle through the shared "
                    "owners; missing " + ", ".join(missing),
                )
            )
    facts, failures = _facts_for(provider, _ADMISSION, GUARD_BUNDLE_ADMISSION)
    findings.extend(failures)
    if not failures:
        for function, required in _ADMISSION_ROUTES.items():
            missing = [name for name in required if name not in _scope_calls(facts, function)]
            if missing:
                findings.append(
                    _summary(
                        GUARD_BUNDLE_ADMISSION,
                        _ADMISSION,
                        f"{function} must route through " + ", ".join(missing),
                    )
                )
    facts, failures = _facts_for(provider, _UNPACKER, GUARD_BUNDLE_ADMISSION)
    findings.extend(failures)
    if not failures:
        admit = _first_line_re(facts, re.compile(r"^\s+admit\($"))
        first_exit = min(
            (
                line
                for line in (
                    _first_line_re(facts, re.compile(r"^\s+if dry_run:")),
                    _first_line_re(facts, re.compile(r"shutil\.copy")),
                )
                if line is not None
            ),
            default=None,
        )
        if (
            not _present(facts, "    admit: Callable[[UnpackCandidate], None],")
            or admit is None
            or (first_exit is not None and admit > first_exit)
        ):
            findings.append(
                _summary(
                    GUARD_BUNDLE_ADMISSION,
                    _UNPACKER,
                    "unpack_bundle must require an admission and run it before a preview or a write",
                )
            )
    facts, failures = _facts_for(provider, _PACK_COMMAND, GUARD_BUNDLE_ADMISSION)
    findings.extend(failures)
    if not failures and not any(
        definition.name == "unpack_cmd"
        and "serialized_lifecycle_unless('dry_run')" in definition.decorators
        for definition in facts.definitions
    ):
        findings.append(
            _summary(
                GUARD_BUNDLE_ADMISSION,
                _PACK_COMMAND,
                "apm unpack must hold the lifecycle lock (serialized_lifecycle_unless)",
            )
        )
    return tuple(findings)


_UPDATE = "src/apm_cli/commands/update.py"
_CATALOG_ORIGIN = "src/apm_cli/marketplace/catalog_origin.py"
_PROVENANCE_REQUIRED = {
    _UPDATE: ("_existing_lock = _reverified_lock(LockFile.read(_lock_path)",),
    _CATALOG_ORIGIN: (
        "def reverify_locked_provenance(",
        "require_locked_row_origin(dep)",
        "resolution = resolve_marketplace_plugin(plugin, alias)",
    ),
}
# Lock catalog provenance is written by the lockfile phase and the origin owner only.
_PROVENANCE_WRITERS = frozenset({"src/apm_cli/install/phases/lockfile.py", _CATALOG_ORIGIN})
_PROVENANCE_WRITE = re.compile(r"\.(source_url|source_digest) = ")


def check_update_marketplace_provenance(provider: FactsProvider) -> tuple[Violation, ...]:
    """``apm update`` re-verifies locked marketplace provenance through one owner (#4228)."""
    findings: list[Violation] = []
    for path, needles in _PROVENANCE_REQUIRED.items():
        facts, failures = _facts_for(provider, path, GUARD_UPDATE_MARKETPLACE_PROVENANCE)
        findings.extend(failures)
        if failures:
            continue
        missing = [needle for needle in needles if not _present(facts, needle)]
        if missing:
            findings.append(
                _summary(
                    GUARD_UPDATE_MARKETPLACE_PROVENANCE,
                    path,
                    "Marketplace provenance re-verification is missing: " + ", ".join(missing),
                )
            )
    for path in _python_paths(provider, "src/apm_cli/"):
        if path in _PROVENANCE_WRITERS:
            continue
        facts, failures = _facts_for(provider, path, GUARD_UPDATE_MARKETPLACE_PROVENANCE)
        if failures:
            continue
        findings.extend(
            violation(
                GUARD_UPDATE_MARKETPLACE_PROVENANCE,
                path,
                "Write lock source_url/source_digest through marketplace.catalog_origin",
                line=number,
            )
            for number, line in enumerate(_lines(facts), start=1)
            if _PROVENANCE_WRITE.search(line)
        )
    return tuple(findings)
