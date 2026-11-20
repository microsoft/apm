"""Remediation advice: the command a user runs and the reported state it clears.

Single owner of every "run 'apm ...' to ..." line APM prints. Call sites pick a
catalog entry (binding any ``{placeholder}`` such as a package name) and render
it with :meth:`Remediation.clause`, :meth:`Remediation.sentence` or
:func:`either`; they never spell remediation text themselves. ``apm audit``
serializes the same entries (:meth:`Remediation.to_json`) so scripts can run
the advised command.

Every catalog entry is a promise: running ``command`` (after ``before``, when
set) clears the state that reported it. The installed-CLI proofs in
``tests/integration/test_remediation_advice_lifecycle.py`` run each entry and
observe that, and the architecture rule
``contracts-tooling-remediation-advice`` rejects free-text remediation
anywhere else under ``src/apm_cli``.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType


@dataclass(frozen=True)
class Remediation:
    """One structured remediation.

    ``command`` is the exact command line to run (``<name>`` marks a value the
    user supplies; ``{name}`` marks a value the call site binds). ``fixes_promise``
    is the verb phrase naming what running it fixes. ``before`` is a manual
    step required first; ``then`` is a step to take after the command succeeds.
    ``clears`` names the reported state the remediation clears; its proof
    induces that state and observes it gone.
    """

    id: str
    command: str
    fixes_promise: str
    before: str | None = None
    then: str | None = None
    clears: str = ""

    def bind(self, **values: object) -> Remediation:
        """Return this remediation with its ``{placeholders}`` filled in."""
        text = {key: str(value) for key, value in values.items()}
        return replace(
            self,
            command=self.command.format(**text),
            fixes_promise=self.fixes_promise.format(**text),
            before=self.before.format(**text) if self.before else None,
            then=self.then.format(**text) if self.then else None,
        )

    def _require_bound(self) -> None:
        for field_text in (self.command, self.fixes_promise, self.before, self.then):
            if field_text and "{" in field_text:
                raise ValueError(f"remediation {self.id!r} has unbound placeholders")

    def option(self) -> str:
        """Render ``'command' to promise`` (no verb), for a list of choices."""
        self._require_bound()
        return f"'{self.command}' to {self.fixes_promise}"

    def clause(self) -> str:
        """Render the lowercase clause, e.g. ``run 'apm install' to ...``."""
        text = f"run {self.option()}"
        if self.before:
            text = f"{self.before}, then {text}"
        if self.then:
            text = f"{text}, then {self.then}"
        return text

    def sentence(self) -> str:
        """Render the clause as a capitalized sentence ending in a period."""
        return _sentence(self.clause())

    def argv(self) -> tuple[str, ...]:
        """Return the command split into arguments (``argv[0]`` is ``apm``)."""
        self._require_bound()
        return tuple(shlex.split(self.command))

    def to_json(self) -> dict[str, str]:
        """Serialize for machine-readable reports (``apm audit --format json``)."""
        self._require_bound()
        payload = {
            "id": self.id,
            "command": self.command,
            "fixes": self.fixes_promise,
            "clears": self.clears,
        }
        if self.before:
            payload["before"] = self.before
        if self.then:
            payload["then"] = self.then
        return payload


def _sentence(clause: str) -> str:
    return f"{clause[:1].upper()}{clause[1:]}."


def either(first: Remediation, *alternatives: Remediation) -> str:
    """Render ``run 'a' to x, or 'b' to y`` for interchangeable remediations."""
    rendered = (
        item.clause() if item.before or item.then else item.option() for item in alternatives
    )
    return ", or ".join((first.clause(), *rendered))


def each(*remediations: Remediation) -> str:
    """Render ``run 'a' to x; run 'b' to y`` for remediations that all apply."""
    return "; ".join(item.clause() for item in remediations)


def either_sentence(first: Remediation, *alternatives: Remediation) -> str:
    """Render :func:`either` as a capitalized sentence ending in a period."""
    return _sentence(either(first, *alternatives))


_CATALOG: dict[str, Remediation] = {}


def _entry(
    remediation_id: str,
    command: str,
    fixes_promise: str,
    *,
    clears: str,
    before: str | None = None,
    then: str | None = None,
) -> Remediation:
    if remediation_id in _CATALOG:
        raise ValueError(f"duplicate remediation id {remediation_id!r}")
    if not command.startswith("apm "):
        raise ValueError(f"remediation {remediation_id!r} must run an apm command")
    if not clears:
        raise ValueError(f"remediation {remediation_id!r} must name the state it clears")
    remediation = Remediation(remediation_id, command, fixes_promise, before, then, clears)
    _CATALOG[remediation_id] = remediation
    return remediation


def catalog() -> Mapping[str, Remediation]:
    """Return every remediation APM can advise, keyed by id."""
    return MappingProxyType(_CATALOG)


# -- Lockfile and deployed state (apm audit, apm install) --------------------

LOCKFILE_MISSING = _entry(
    "lockfile-missing",
    "apm install",
    "generate apm.lock.yaml",
    clears="apm.lock.yaml is missing or unreadable",
)
DEPLOYED_FILES_MISSING = _entry(
    "deployed-files-missing",
    "apm install",
    "restore the missing deployed files",
    clears="files apm.lock.yaml records are missing on disk",
)
ORPHANED_PACKAGES = _entry(
    "orphaned-packages",
    "apm install",
    "remove packages apm.yml no longer declares",
    clears="apm.lock.yaml holds packages apm.yml no longer declares",
)
REF_MISMATCH = _entry(
    "ref-mismatch",
    "apm install",
    "resolve the manifest refs into apm.lock.yaml",
    clears="an apm.yml ref differs from the ref apm.lock.yaml records",
)
REF_MISMATCH_UPDATE = _entry(
    "ref-mismatch-update",
    "apm update",
    "re-resolve the changed refs into apm.lock.yaml",
    clears="an apm.yml source or pin differs from apm.lock.yaml",
)
SKILL_SUBSET_MISMATCH = _entry(
    "skill-subset-mismatch",
    "apm install",
    "record the declared skill selections in apm.lock.yaml",
    clears="the declared skill selection differs from apm.lock.yaml",
)
MCP_CONFIG_MISMATCH = _entry(
    "mcp-config-mismatch",
    "apm install",
    "reconcile the MCP configs with apm.lock.yaml (add '--only apm' for a skills-only install)",
    clears="MCP configs differ from the apm.lock.yaml baseline",
)
STRIP_HIDDEN_UNICODE = _entry(
    "strip-hidden-unicode",
    "apm audit --strip",
    "remove the hidden Unicode characters",
    clears="deployed files contain hidden Unicode characters",
)
LIST_INSTALLED_PACKAGES = _entry(
    "list-installed-packages",
    "apm deps list",
    "see the packages apm.lock.yaml records",
    clears="the audited package is not in apm.lock.yaml",
)
AUDIT_POLICY_IN_CI = _entry(
    "audit-policy-in-ci",
    "apm audit --ci --policy <source>",
    "run the policy checks",
    clears="--policy was passed without --ci, so no policy checks ran",
)
REVIEW_PARKED_MCP = _entry(
    "review-parked-mcp",
    "apm approve --pending",
    "review the parked servers",
    clears="MCP servers are parked because their definition changed",
)
RESTAGE_BUNDLE = _entry(
    "restage-bundle",
    "apm install <bundle> --force",
    "restage the drifted bundle files",
    clears="staged bundle files drifted from their recorded hashes",
)
REPAIR_OWNERSHIP = _entry(
    "repair-ownership",
    "apm install",
    "record deployment ownership for the listed files",
    clears="deployed files have no recorded deployment owner",
)
RESTORE_MANAGED_PATHS = _entry(
    "restore-managed-paths",
    "apm install",
    "restore the unsafe or unresolved managed paths",
    before="remove each listed path that is a symlink or not a regular file",
    clears="managed deployed paths are unsafe or unresolved",
)
REBUILD_LOCAL_GRAPH = _entry(
    "rebuild-local-graph",
    "apm install",
    "rebuild the local dependency graph in apm.lock.yaml",
    clears="the local dependency graph in apm.lock.yaml is corrupt",
)
POPULATE_INSTALL_CACHE = _entry(
    "populate-install-cache",
    "apm install",
    "populate the install cache drift replays from",
    clears="the install cache lacks the locked package copies",
)
RETIRE_BUNDLE_HOOKS = _entry(
    "retire-bundle-hooks",
    "apm install",
    "retire the hooks executables trust does not admit",
    clears="local bundle hooks are deployed that trust does not admit",
)
KEEP_BUNDLE_HOOKS = _entry(
    "keep-bundle-hooks",
    "apm install <bundle>",
    "redeploy its hooks",
    before="approve the bundle",
    clears="local bundle hooks were retired because trust did not admit them",
)
AUDIT_FROM_PROJECT_ROOT = _entry(
    "audit-from-project-root",
    "apm audit --ci",
    "audit the project (APM does not search parent directories)",
    before="change to the project root that contains apm.yml",
    clears="audit ran outside a project, so nothing was audited",
)
INVALID_DEPLOYMENT_OWNERS = _entry(
    "invalid-deployment-owners",
    "apm prune",
    "remove the invalid deployment ownership records",
    then="rerun 'apm audit'",
    clears="deployment rows name owners apm.lock.yaml does not hold",
)
REPAIR_REQUIRED_PACKAGES = _entry(
    "required-packages-absent",
    "apm install --no-policy",
    "record the required packages in apm.lock.yaml",
    then="reinstall normally",
    clears="policy-required packages are absent from apm.lock.yaml",
)
TRUST_PACKAGE = _entry(
    "trust-package",
    "apm approve {package}",
    "trust its executables",
    clears="a package's executables are parked pending trust",
)
TRUST_RECOMMENDED = _entry(
    "trust-recommended",
    "apm approve --recommended",
    "trust the org-recommended packages",
    clears="org-recommended packages' executables are parked pending trust",
)
TRACK_UNMANAGED_FILE = _entry(
    "track-unmanaged-file",
    "apm install <ref>",
    "track a flagged file",
    clears="an unmanaged file sits in a governed directory",
)
CONFIGURE_REGISTRY = _entry(
    "configure-registry",
    "apm config set registry.{name}.url <url>",
    "configure the required registry",
    before="run 'apm experimental enable registries'",
    clears="a policy-required registry is not configured",
)
REGENERATE_CONTENT_HASHES = _entry(
    "regenerate-content-hashes",
    "apm install",
    "record the required content hashes",
    clears="locked dependencies lack required content hashes",
)
POLICY_FULL_REPORT = _entry(
    "policy-full-report",
    "apm install --dry-run --verbose <same-arguments>",
    "list every policy violation without installing",
    clears="only part of the policy report was shown",
)
UPDATE_REFS = _entry(
    "update-refs",
    "apm update",
    "re-resolve refs to the latest matching versions",
    clears="dependency refs are behind the latest matching versions",
)
UPDATE_REFS_THEN_FROZEN = _entry(
    "update-refs-then-frozen",
    "apm update",
    "refresh the refs in apm.lock.yaml",
    then="run 'apm install --frozen' in CI",
    clears="--frozen and --update were combined",
)
APPLY_SKILL_SELECTION = _entry(
    "apply-skill-selection",
    "apm install",
    "apply it",
    before="edit the skills: list in apm.yml",
    clears="deployed skills differ from the edited skills: list",
)
REPACK_PLUGIN_BUNDLE = _entry(
    "repack-plugin-bundle",
    "apm pack --claude-plugin --archive",
    "repack it in the plugin format",
    before="change to the bundle's source project",
    clears="a bundle is not in the plugin format install accepts",
)
UNPACK_LEGACY_BUNDLE = _entry(
    "unpack-legacy-bundle",
    "apm unpack <bundle>",
    "deploy the legacy bundle",
    clears="a legacy-format bundle is not deployed",
)
INSTALL_FROM_REGISTRY = _entry(
    "install-from-registry",
    "apm install <org/package>",
    "install a package from its repository",
    clears="a package reference was taken for a bundle archive",
)
INIT_PROJECT = _entry(
    "init-project", "apm init", "create apm.yml", clears="the project has no apm.yml"
)
BOOTSTRAP_PROJECT_MANIFEST = _entry(
    "bootstrap-project-manifest",
    "apm install <org/repo>",
    "create apm.yml and install the package",
    clears="the project has no apm.yml",
)
BOOTSTRAP_USER_MANIFEST = _entry(
    "bootstrap-user-manifest",
    "apm install -g <org/repo>",
    "create the user manifest and install the package",
    clears="the user scope has no apm.yml",
)
DIAGNOSE_CONNECTIVITY = _entry(
    "diagnose-connectivity",
    "apm doctor",
    "diagnose auth and connectivity",
    clears="a download failed for an auth or network reason",
)
_KEEP_SCOPE = "keep --root DIR or --global if you used them"
RECORD_SELECTION_CHANGE = _entry(
    "record-selection-change",
    "apm install",
    f"record the changed selections in apm.lock.yaml ({_KEEP_SCOPE})",
    then="commit apm.lock.yaml",
    clears="dependency selections differ from apm.lock.yaml",
)
FROZEN_REFRESH_LOCK = _entry(
    "frozen-refresh-lock",
    "apm install",
    "refresh apm.lock.yaml",
    before="update apm.yml",
    then="retry as 'apm install --frozen', without packages or --mcp",
    clears="a frozen install was asked to change apm.yml",
)
ACCEPT_LOCAL_CHANGE = _entry(
    "accept-local-change",
    "apm install",
    "accept and record the changed local content",
    clears="local dependency content differs from apm.lock.yaml",
)
RECORD_MCP_DECLARATIONS = _entry(
    "record-mcp-declarations",
    "apm install",
    "record the intended MCP declarations",
    before="review the restored sources and lockfile",
    clears="restored MCP declarations differ from apm.lock.yaml",
)
RECORD_DEPLOYED_FILES = _entry(
    "record-deployed-files",
    "apm install",
    f"record them in apm.lock.yaml ({_KEEP_SCOPE})",
    then="commit the deployed files and apm.lock.yaml",
    clears="deployed files are not recorded in apm.lock.yaml",
)
RECORD_REPLACEMENT_SOURCES = _entry(
    "record-replacement-sources",
    "apm install",
    "record the selected sources in apm.lock.yaml",
    clears="replacement rules differ from apm.lock.yaml",
)
RESTORE_USER_PACKAGES = _entry(
    "restore-user-packages",
    "apm install --global",
    "restore the user-scope package copies",
    clears="user-scope package copies are missing or stale",
)
PIN_REF_FOR_AUDIT = _entry(
    "pin-ref-for-audit",
    "apm install",
    "record a resolved commit audit can replay",
    before="pin the dependency to a commit, tag, or branch in apm.yml",
    clears="a locked remote dependency has no resolved commit",
)
REPAIR_LOCK_STATE = _entry(
    "repair-lock-state",
    "apm install",
    f"create or repair the {{lock_state}} ({_KEEP_SCOPE}, and the other original options)",
    clears="lock state differs from what a frozen install needs",
)
TRUST_BIN = _entry(
    "trust-bin",
    "apm install --global --trust-bin {package}",
    "deploy its bin/ executables",
    clears="a package's bin/ executables were not deployed",
)
EXPLAIN_POLICY = _entry(
    "explain-policy",
    "apm policy explain {package}",
    "see which policy rule applies",
    clears="a policy rule denied or parked a package",
)
FORCE_INSTALL = _entry(
    "force-install",
    "apm install --force",
    "deploy despite the critical findings",
    clears="install-time audit blocked critical findings",
)
REVIEW_HIDDEN_UNICODE = _entry(
    "review-hidden-unicode",
    "apm audit",
    "review the findings",
    clears="install-time audit found hidden-character findings",
)
PRUNE_PACKAGE_COPIES = _entry(
    "prune-package-copies",
    "apm prune",
    "remove the leftover package copies",
    clears="leftover package copies remain in apm_modules",
)
READMIT_CODEX_PLUGINS = _entry(
    "readmit-codex-plugins",
    "apm install -g --target codex",
    "re-admit the Codex plugins",
    before="review the executable approvals",
    clears="Codex admission differs from the saved approval state",
)
COMPLETE_CODEX_MIGRATION = _entry(
    "complete-codex-migration",
    "apm install -g --target codex",
    "complete the native migration",
    before="preserve or relocate your edits and resolve the cleanup conflicts",
    clears="loose Codex files block the native migration",
)
VERIFY_LOCKED_CONTENT = _entry(
    "verify-locked-content",
    "apm install",
    "record its verified content",
    before="reinstall {package} so its bytes are verified",
    clears="a package's locked content cannot be verified",
)
COMPILE_ROOT_CONTEXT = _entry(
    "compile-root-context",
    "apm compile",
    "update {outputs}",
    clears="root context files miss newly deployed instructions",
)
COMPILE_USER_ROOT_CONTEXT = _entry(
    "compile-user-root-context",
    "apm compile -g",
    "update the root context files for: {targets}",
    clears="user root context files miss applicable instructions",
)
DEPLOY_APPROVED_EXECUTABLES = _entry(
    "deploy-approved-executables",
    "apm install",
    "deploy the approved executables",
    clears="newly approved executables are not deployed yet",
)
TRUST_AND_DEPLOY = _entry(
    "trust-and-deploy",
    "apm approve {package}",
    "trust its executables",
    then="rerun 'apm install' to deploy them",
    clears="a package's executables are parked pending trust",
)
RERUN_AFTER_FIX = _entry(
    "rerun-after-fix",
    "apm install",
    "deploy the corrected source",
    before="{fix}",
    clears="a source APM could not read or deploy",
)
APPLY_REPLACEMENT_TABLE = _entry(
    "apply-replacement-table",
    "apm install",
    "apply the complete replacement table (no package selectors)",
    clears="a partial install met changed replacement rules",
)
SET_COWORK_SKILLS_DIR = _entry(
    "set-cowork-skills-dir",
    "apm config set copilot-cowork-skills-dir <path>",
    "point APM at the Cowork skills directory",
    clears="the Cowork skills directory is unknown",
)
INSTALL_COWORK_GLOBAL = _entry(
    "install-cowork-global",
    "apm install --target copilot-cowork --global",
    "install to user scope",
    clears="the copilot-cowork target was used without --global",
)
CLEAN_DEPS_CACHE = _entry(
    "clean-deps-cache",
    "apm deps clean --yes",
    "remove the cached package copies",
    then="retry",
    clears="a cached package copy is invalid",
)
REPACK_WITH_INTEGRITY = _entry(
    "repack-with-integrity",
    "apm pack",
    "embed an integrity manifest",
    before="change to the bundle's source project",
    clears="a bundle has no integrity manifest",
)
COMPILE_STAGED_INSTRUCTIONS = _entry(
    "compile-staged-instructions",
    "apm compile",
    "merge them into AGENTS.md / GEMINI.md / equivalent",
    clears="staged bundle instructions are not compiled",
)
RETRY_INSTALL = _entry(
    "retry-install", "apm install", "retry the install", clears="an install step was interrupted"
)
REINSTALL_USER_PACKAGE = _entry(
    "reinstall-user-package",
    "apm install --global {package}",
    "reinstall it natively",
    before="run 'apm uninstall --global {package}'",
    clears="a package could not switch to its native install",
)
CLEAN_DOWNLOAD_CACHE = _entry(
    "clean-download-cache",
    "apm cache clean --yes",
    "drop the cached copy",
    then="retry",
    clears="downloaded content differs from its locked hash",
)
ACCEPT_NEW_CONTENT = _entry(
    "accept-new-content",
    "apm install{global_flag}",
    "accept the new content",
    before="remove the '{package}' entry from {lockfile}",
    clears="downloaded content differs from its locked hash",
)
ENABLE_EXPERIMENTAL = _entry(
    "enable-experimental",
    "apm experimental enable {flag}",
    "enable it",
    clears="a target needs an experimental flag",
)
APPROVE_TRANSITIVE_MCP = _entry(
    "approve-transitive-mcp",
    "apm approve {package}",
    "keep its MCP servers on later installs",
    clears="transitive MCP servers were deployed for one run only",
)
RESET_MCP_REGISTRY_URL = _entry(
    "reset-mcp-registry-url",
    "apm config unset mcp-registry-url",
    "fall back to the default registry",
    then="retry",
    clears="the configured MCP registry URL is invalid",
)
RESOLVE_DEPENDENCIES = _entry(
    "resolve-dependencies",
    "apm install",
    "resolve the dependencies (no --dry-run)",
    clears="a dry run could not preview every dependency",
)
RESYNC_DEPLOYED = _entry(
    "drift-resync",
    "apm install",
    "re-sync deployed files with the lockfile",
    clears="deployed files are missing, orphaned or not yet integrated",
)
RESYNC_UNRECORDED = _entry(
    "drift-unrecorded",
    "apm install",
    "record the deployed files in apm.lock.yaml",
    then="commit apm.lock.yaml to restore content-integrity coverage",
    clears="deployed files are not recorded in apm.lock.yaml",
)
RESTORE_EDITED = _entry(
    "drift-restore-edited",
    "apm install",
    "restore the locked content",
    before="delete {subject}",
    then="commit the result",
    clears="managed files were edited locally",
)
RESTORE_EDITED_FORCE = _entry(
    "drift-restore-edited-force",
    "apm install --force",
    "restore them without deleting first (overwrites every local edit and deploys "
    "despite critical security findings)",
    clears="managed files were edited locally",
)
RESTORE_BUNDLE_EDITED = _entry(
    "drift-restore-bundle-edited",
    "apm install <bundle-path>",
    "reinstall the bundle that deployed them",
    before="delete {subject}",
    then="commit the result",
    clears="files a local bundle deployed were edited",
)
EXPORT_LOCAL_EDITS = _entry(
    "export-local-edits",
    "apm export-patch",
    "export them as upstream patches before an install overwrites them",
    clears="local edits to managed files would be overwritten",
)
ADOPT_NEW_RENDERING = _entry(
    "adopt-new-rendering",
    "apm install",
    "adopt the new rendering",
    then="commit the result",
    clears="files match apm.lock.yaml but render differently now",
)
RESYNC_AFTER_UNMANAGED_CONFLICT = _entry(
    "drift-resync-after-conflict",
    "apm install",
    "re-sync the other findings",
    before="resolve the ownership conflicts",
    clears="drift remains once ownership conflicts are resolved",
)


# Drift finding kind -> the remediation that clears it. A plain 'apm install'
# keeps a local edit to a managed file, so an edited ("modified") finding is
# restored only by deleting it first (#3994); re-sync covers the kinds that a
# plain install repairs. Unmanaged conflicts belong to the user: APM cannot
# repair them, so they map to no command.
DRIFT_KIND_REMEDIATION: Mapping[str, Remediation | None] = MappingProxyType(
    {
        "unmanaged-conflict": None,
        "modified": RESTORE_EDITED,
        "unintegrated": RESYNC_DEPLOYED,
        "orphaned": RESYNC_DEPLOYED,
        "unrecorded": RESYNC_UNRECORDED,
    }
)


def restore_edited(subject: str, *, bundle: bool = False) -> Remediation:
    """Return the remediation that restores edited managed files named by *subject*.

    A plain ``apm install`` keeps a local edit to a managed file, so the files
    are deleted first. Files a local bundle deployed are not redeployed by
    ``apm install`` at all; only reinstalling the bundle restores them (#3994).
    """
    restore = RESTORE_BUNDLE_EDITED if bundle else DRIFT_KIND_REMEDIATION["modified"]
    return restore.bind(subject=subject)


__all__ = [
    "DRIFT_KIND_REMEDIATION",
    "Remediation",
    "catalog",
    "each",
    "either",
    "either_sentence",
    "restore_edited",
]
