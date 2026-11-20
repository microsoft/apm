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
    """

    id: str
    command: str
    fixes_promise: str
    before: str | None = None
    then: str | None = None

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
        payload = {"id": self.id, "command": self.command, "fixes": self.fixes_promise}
        if self.before:
            payload["before"] = self.before
        if self.then:
            payload["then"] = self.then
        return payload


def _sentence(clause: str) -> str:
    return f"{clause[:1].upper()}{clause[1:]}."


def either(first: Remediation, *alternatives: Remediation) -> str:
    """Render ``run 'a' to x, or 'b' to y`` for interchangeable remediations."""
    return ", or ".join((first.clause(), *(item.option() for item in alternatives)))


def either_sentence(first: Remediation, *alternatives: Remediation) -> str:
    """Render :func:`either` as a capitalized sentence ending in a period."""
    return _sentence(either(first, *alternatives))


_CATALOG: dict[str, Remediation] = {}


def _entry(
    remediation_id: str,
    command: str,
    fixes_promise: str,
    *,
    before: str | None = None,
    then: str | None = None,
) -> Remediation:
    if remediation_id in _CATALOG:
        raise ValueError(f"duplicate remediation id {remediation_id!r}")
    if not command.startswith("apm "):
        raise ValueError(f"remediation {remediation_id!r} must run an apm command")
    remediation = Remediation(remediation_id, command, fixes_promise, before, then)
    _CATALOG[remediation_id] = remediation
    return remediation


def catalog() -> Mapping[str, Remediation]:
    """Return every remediation APM can advise, keyed by id."""
    return MappingProxyType(_CATALOG)


# -- Lockfile and deployed state (apm audit, apm install) --------------------

LOCKFILE_MISSING = _entry("lockfile-missing", "apm install", "generate apm.lock.yaml")
DEPLOYED_FILES_MISSING = _entry(
    "deployed-files-missing", "apm install", "restore the missing deployed files"
)
ORPHANED_PACKAGES = _entry(
    "orphaned-packages", "apm install", "remove packages apm.yml no longer declares"
)
RESYNC_DEPLOYED = _entry("drift-resync", "apm install", "re-sync deployed files with the lockfile")
RESYNC_UNRECORDED = _entry(
    "drift-unrecorded",
    "apm install",
    "record the deployed files in apm.lock.yaml",
    then="commit apm.lock.yaml to restore content-integrity coverage",
)
RESTORE_EDITED = _entry(
    "drift-restore-edited",
    "apm install",
    "restore the locked content",
    before="delete {subject}",
    then="commit the result",
)
# What a plain 'apm install' does to an edited managed file depends on its
# kind (RA-009): it overwrites instructions, agents, prompts, commands and
# rules, and refuses to overwrite a skill. Deleting first restores both.
RESTORE_EDITED_OVERWRITE = _entry(
    "drift-restore-edited-overwrite",
    "apm install",
    "restore the locked content (a plain 'apm install' also overwrites these edited "
    "instructions, agents, prompts, commands and rules)",
    before="delete {subject}",
    then="commit the result",
)
RESTORE_EDITED_SKILL = _entry(
    "drift-restore-edited-skill",
    "apm install",
    "restore the locked content (a plain 'apm install' refuses to overwrite an edited skill)",
    before="delete {subject}",
    then="commit the result",
)
RESTORE_EDITED_FORCE = _entry(
    "drift-restore-edited-force",
    "apm install --force",
    "restore them without deleting first (overwrites every local edit and deploys "
    "despite critical security findings)",
)
RESTORE_BUNDLE_EDITED = _entry(
    "drift-restore-bundle-edited",
    "apm install <bundle-path>",
    "reinstall the bundle that deployed them",
    before="delete {subject}",
    then="commit the result",
)
RESYNC_AFTER_UNMANAGED_CONFLICT = _entry(
    "drift-resync-after-conflict",
    "apm install",
    "re-sync the other findings",
    before="resolve the ownership conflicts",
)

# -- W2 (integration/, marketplace/, bundle/) --------------------------------
# -- W2 integration --
# Advice printed by src/apm_cli/integration. Keep W2 integration additions here.
HOOK_CONFIG_UNREADABLE = _entry(
    "hook-config-unreadable",
    "apm install",
    "remove APM hook entries left for dropped targets",
    before="delete or fix the file manually",
)
HOOK_SIDECAR_UNREADABLE = _entry(
    "hook-sidecar-unreadable",
    "apm install",
    "finish the dropped-target hook cleanup",
    before="delete or fix the file manually",
)
HOOK_PLUGIN_ROOT_UNRESOLVED = _entry(
    "hook-plugin-root-unresolved",
    "apm install",
    "redeploy the hook with the resolved path",
    before="ensure the quote marks match and wrap the complete reference and path in "
    "balanced double quotes",
)
HOOK_PATH_ESCAPES_PACKAGE = _entry(
    "hook-path-escapes-package",
    "apm install",
    "redeploy the hook",
    before="keep the path inside the package",
)
CODEX_AGENT_CLAUDE_MODEL = _entry(
    "codex-agent-claude-model",
    "apm install",
    "redeploy the Codex agent",
    before="use a Codex model in a Codex-specific agent source, or remove 'model' so "
    "Codex uses its default",
)
CODEX_AGENT_FIELD_NOT_STRING = _entry(
    "codex-agent-field-not-string",
    "apm install",
    "redeploy the Codex agent with that setting",
    before="set '{field}' to a string in the source agent",
)
CODEX_AGENT_FRONTMATTER_NOT_MAPPING = _entry(
    "codex-agent-frontmatter-not-mapping",
    "apm install",
    "redeploy the Codex agent with verified tool restrictions",
    before="fix the source agent frontmatter to a YAML mapping",
)
CODEX_AGENT_FRONTMATTER_INVALID = _entry(
    "codex-agent-frontmatter-invalid",
    "apm install",
    "redeploy the Codex agent with verified tool restrictions",
    before="repair the frontmatter",
)
INSTRUCTION_FRONTMATTER_REJECTED = _entry(
    "instruction-frontmatter-rejected",
    "apm install",
    "deploy the instruction",
    before="fix or remove the invalid frontmatter",
)
LOCAL_FILE_UNMANAGED = _entry(
    "local-file-unmanaged",
    "apm install --force",
    "overwrite it with the package version",
)
COWORK_SKILLS_DIR_UNDETECTED = _entry(
    "cowork-skills-dir-undetected",
    "apm config set copilot-cowork-skills-dir <path>",
    "clean up these entries on the next install or uninstall",
)
CANVAS_EXTENSIONS_BLOCKED = _entry(
    "canvas-extensions-blocked",
    "apm approve {package}",
    "trust '{package}'",
    then="run 'apm install' to deploy them to {deploy_dir}",
)
COPILOT_APP_WORKFLOW_UNBOUND = _entry(
    "copilot-app-workflow-unbound",
    "apm install",
    "bind the workflows to that project",
    before="change into the project's git repository",
)
MCP_ADMITTED_DENIED = _entry(
    "mcp-admitted-denied",
    "apm install",
    "reconcile the MCP server",
    before="lift the deny that 'apm policy explain {target}' names",
)
MCP_ADMITTED_UNAPPROVED = _entry(
    "mcp-admitted-unapproved",
    "apm approve {target}",
    "approve its MCP servers",
    then="run 'apm install'",
)
MCP_LOCAL_PACKAGE_UNRESOLVED = _entry(
    "mcp-local-package-unresolved", "apm install", "rebuild the lockfile"
)
MCP_PACKAGE_MANIFEST_MISSING = _entry(
    "mcp-package-manifest-missing", "apm install", "restore the package manifest"
)
MCP_LOCKFILE_UNWRITABLE = _entry(
    "mcp-lockfile-unwritable",
    "apm install",
    "create {lockfile}",
    before="make the directory writable",
)
MCP_REGISTRY_ALTERNATIVE = _entry(
    "mcp-registry-alternative",
    'apm install --mcp "{server}" --registry <URL>{retry}',
    "look the server up in another registry (replace <URL>)",
)
SKILL_SELECTION_DECLARED_UNMATCHED = _entry(
    "skill-selection-declared-unmatched",
    "apm install",
    "apply the updated skill selection",
    before="update '{package}' to a version whose 'skills:' names available skills",
)
SKILL_SELECTION_UNMATCHED = _entry(
    "skill-selection-unmatched",
    "apm install",
    "apply the updated skill selection",
    before="edit 'skills:' in apm.yml to use an available name or remove the filter",
)
SKILL_CONFLICT_UNMANAGED = _entry(
    "skill-conflict-unmanaged",
    "apm install",
    "deploy the skills without the conflicts",
    before="rename the local skill or use a valid existing skills: or targets: selection "
    "in apm.yml that excludes these conflicts",
)
AUDIT_TARGET_FLAG_DISABLED = _entry(
    "audit-target-flag-disabled",
    "apm experimental enable {flag}",
    "audit this installation",
)
# -- end W2 integration --
# -- W2 marketplace --
MARKETPLACE_DUPLICATE_ALIAS = _entry(
    "marketplace-duplicate-alias",
    "apm marketplace remove {name}",
    "drop the duplicate alias",
)
MARKETPLACE_FETCH_FAILED = _entry(
    "marketplace-fetch-failed",
    "apm marketplace update {name}",
    "refetch the marketplace",
)
MARKETPLACE_GIT_ACCESS_FAILED = _entry(
    "marketplace-git-access-failed",
    "apm marketplace update {name}",
    "refetch the marketplace",
    before="correct Git access",
)
MARKETPLACE_GIT_REWRITE_REJECTED = _entry(
    "marketplace-git-rewrite-rejected",
    "apm marketplace update {name}",
    "refetch the marketplace",
    before="correct the matching Git configuration",
)
MARKETPLACE_UNREGISTERED_URL = _entry(
    "marketplace-unregistered-url",
    "apm marketplace add https://{host}/<owner>/<repo>",
    "register it",
)
MARKETPLACE_UNREGISTERED_SHORTHAND = _entry(
    "marketplace-unregistered-shorthand",
    "apm marketplace add <owner>/<repo>",
    "register it on the default host",
)
MARKETPLACE_ORIGIN_RESTORE = _entry(
    "marketplace-origin-restore",
    "apm marketplace add {source} --name {name}",
    "keep the locked marketplace (add --ref or --path if the original registration used them)",
)
MARKETPLACE_ORIGIN_SWITCH = _entry(
    "marketplace-origin-switch",
    "apm install",
    "drop the plugin locked from the old marketplace",
    before="remove {plugin} from apm.yml",
    then="add it back and run 'apm install' again",
)
MARKETPLACE_YML_COMPILE = _entry(
    "marketplace-yml-compile", "apm pack", "compile it to marketplace.json"
)
MARKETPLACE_BLOCK_COMPILE = _entry(
    "marketplace-block-compile",
    "apm pack",
    "compile this block to .claude-plugin/marketplace.json",
)
MARKETPLACE_MANUAL_KEPT = _entry(
    "marketplace-manual-kept",
    "apm marketplace remove {name}",
    "unregister the manual marketplace",
    then="re-run 'apm install -g' to let the user manifest manage it",
)
MARKETPLACE_RECONCILE_FAILED = _entry(
    "marketplace-reconcile-failed",
    "apm install -g",
    "reconcile the user manifest marketplaces",
    before="fix the source of {target} in {path} or remove it",
)
MARKETPLACE_HOST_CONFLICT = _entry(
    "marketplace-host-conflict",
    "apm marketplace add {source}",
    "register it from the host embedded in SOURCE",
    before="drop --host",
)
MARKETPLACES_OUTSIDE_USER_MANIFEST = _entry(
    "marketplaces-outside-user-manifest",
    "apm install -g",
    "register them from the user manifest",
    before="move the entries to the user manifest ({path})",
)
MARKETPLACE_REGISTER_MANUAL = _entry(
    "marketplace-register-manual",
    "apm marketplace add <source> --name <name>",
    "register one manually",
)
# -- end W2 marketplace --
# -- W2 bundle --
PACK_LOCKFILE_MISSING = _entry(
    "pack-lockfile-missing",
    "apm install",
    "resolve dependencies into apm.lock.yaml",
    then="pack again",
)
PACK_INSTALLED_FILES_MISSING = _entry(
    "pack-installed-files-missing",
    "apm install",
    "restore the installed files recorded in apm.lock.yaml",
    then="pack again",
)
PACK_INSTALLED_FILE_TAMPERED = _entry(
    "pack-installed-file-tampered",
    "apm install",
    "restore the attested content",
    before="delete {path}",
    then="pack again",
)
PACK_SKILL_SUBSET_MISSING = _entry(
    "pack-skill-subset-missing",
    "apm install",
    "re-deploy the expected skills",
    then="pack again",
)
PACK_DEPENDENCY_UNATTESTED = _entry(
    "pack-dependency-unattested",
    "apm install",
    "record provenance in apm.lock.yaml",
    then="pack again",
)
AGENT_PLUGIN_SURFACES_DROPPED = _entry(
    "agent-plugin-surfaces-dropped",
    "apm pack --format claude-plugin",
    "preserve {surfaces} in the legacy Claude client format",
)
AGENT_PLUGIN_MCP_UNPORTABLE = _entry(
    "agent-plugin-mcp-unportable",
    "apm pack --claude-plugin",
    "pack the MCP server in the legacy Claude plugin format",
)
UNPACK_CRITICAL_FINDINGS = _entry(
    "unpack-critical-findings",
    "apm unpack <bundle-path> --force",
    "deploy the bundle anyway (not recommended)",
)
# -- end W2 bundle --
# -- end W2 -------------------------------------------------------------------

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


# -- commands/** (W1) ---------------------------------------------------------
# Advice printed by src/apm_cli/commands. Keep W1 additions inside this block.

PROJECT_MANIFEST_MISSING = _entry("project-manifest-missing", "apm init --yes", "create apm.yml")
USER_MANIFEST_MISSING = _entry(
    "user-manifest-missing",
    "apm install -g <org/repo>",
    "create the user manifest with that package",
)
DEPENDENCIES_NOT_INSTALLED = _entry(
    "dependencies-not-installed", "apm install", "install the dependencies apm.yml declares"
)
DEPENDENCY_UNPLACED = _entry(
    "dependency-unplaced", "apm install", "place it in the resolved dependency tree"
)
ORPHANED_PACKAGES_PRUNE = _entry(
    "orphaned-packages-prune", "apm prune", "remove the orphaned packages"
)
LOCKFILE_MISSING_LOCK = _entry("lockfile-missing-lock", "apm lock", "generate apm.lock.yaml")
LOCKFILE_UNREADABLE = _entry(
    "lockfile-unreadable",
    "apm install",
    "regenerate apm.lock.yaml",
    before="delete or fix {path}",
)
LOCKFILE_RECORDS_INCONSISTENT = _entry(
    "lockfile-records-inconsistent",
    "apm install",
    "regenerate apm.lock.yaml",
    then="retry",
)
DEPLOYMENT_RECORDS_STALE = _entry(
    "deployment-records-stale",
    "apm install",
    "rebuild the deployment records in apm.lock.yaml",
)
PROJECT_OWNED_FILE = _entry(
    "project-owned-file",
    "apm install",
    "redeploy the project's own files",
    before="edit the project's own source (for example under .apm/)",
)
INSTALLED_FILES_DIFFER = _entry(
    "installed-files-differ",
    "apm install",
    "restore the installed files from apm.lock.yaml",
    then="retry",
)
EXECUTABLES_UNAPPROVED = _entry(
    "executables-unapproved",
    "apm approve <package>",
    "pin its executables",
    before="review each definition",
)
EXECUTABLES_APPROVED = _entry(
    "executables-approved", "apm install", "deploy the approved executables"
)
EXECUTABLES_DENIED = _entry(
    "executables-denied", "apm install", "remove executables already deployed"
)
INTEGRATIONS_UNSYNCED = _entry(
    "integrations-unsynced", "apm install", "resync the remaining integrations"
)
INTEGRATIONS_UNSYNCED_AFTER = _entry(
    "integrations-unsynced-after-fix",
    "apm install",
    "resync the remaining integrations",
    before="{fix}",
)
INTEGRATED_FILES_STALE = _entry(
    "integrated-files-stale", "apm install", "rebuild the integrated files"
)
HOOK_CONFIG_STALE = _entry("hook-config-stale", "apm install", "rebuild the hook configuration")
COPILOT_PLUGIN_REGISTRATION_STALE = _entry(
    "copilot-plugin-registration-stale",
    "apm install",
    "re-register the GitHub Copilot plugins",
    before="resolve the cause above",
)
CLAUDE_PLUGIN_REGISTRATION_STALE = _entry(
    "claude-plugin-registration-stale",
    "apm install --global",
    "reconcile the Claude native plugin registrations",
    before="resolve the errors above",
)
MCP_ENTRY_STALE = _entry(
    "mcp-entry-stale",
    "apm install",
    "reconcile the stale MCP entries",
    before="{fix}",
)
MCP_DEPENDENCY_DECLARED = _entry(
    "mcp-dependency-declared",
    "apm install",
    "remove it from your harnesses",
    before="remove '{name}' under dependencies.mcp in apm.yml",
)
LOCAL_DEPENDENCY_UNRESOLVED = _entry(
    "local-dependency-unresolved",
    "apm install",
    "refresh the local dependency",
    before="check the exact path in apm.yml",
    then="retry",
)
LOCAL_DEPENDENCY_UNMAPPED = _entry(
    "local-dependency-unmapped",
    "apm install",
    "regenerate apm.lock.yaml",
)
LOCKFILE_STATE_UNRECONCILED = _entry(
    "lockfile-state-unreconciled",
    "apm install --force",
    "resync the lockfile state",
    then="retry",
)
PRUNE_INCOMPLETE = _entry(
    "prune-incomplete",
    "apm prune",
    "finish the filesystem cleanup",
    before="resolve the removal errors",
)
PRUNE_PARTIAL = _entry(
    "prune-partial",
    "apm prune",
    "finish the filesystem cleanup",
    then="run 'apm audit'",
)
LIFECYCLE_SCRIPTS_UNTRUSTED = _entry(
    "lifecycle-scripts-untrusted",
    "apm lifecycle trust",
    "allow the project lifecycle scripts to run",
)
EXPERIMENTAL_FLAG_DISABLED = _entry(
    "experimental-flag-disabled",
    "apm experimental enable {flag}",
    "enable the {flag} experimental flag",
)
EXPERIMENTAL_FLAGS_UNKNOWN = _entry(
    "experimental-flags-unknown",
    "apm experimental reset",
    "remove the unknown flags from config",
)
OUTSIDE_CONSTRAINT_RELEASE = _entry(
    "outside-constraint-release",
    "apm install",
    "take an outside-constraint release",
    before="select a newer version explicitly in apm.yml",
)
CLI_BINARY_OUTDATED = _entry("cli-binary-outdated", "apm self-update", "update the APM CLI binary")
CLI_INSTALLER_UNREACHABLE = _entry(
    "cli-installer-unreachable",
    "apm self-update",
    "update the APM CLI binary from the mirror",
    before="set APM_INSTALLER_BASE_URL=<mirror>",
)
DEPS_UPDATE_DEPRECATED = _entry(
    "deps-update-deprecated", "apm update", "refresh dependencies with the supported command"
)
REGISTRY_CANONICAL_UNRECORDED = _entry(
    "registry-canonical-unrecorded",
    "apm uninstall {canonical}",
    "remove it by its canonical name",
    before="confirm the registry resolution is correct",
)
MARKETPLACE_REF_UNRESOLVED = _entry(
    "marketplace-ref-unresolved",
    "apm uninstall <owner/repo>",
    "remove it by its owner/repo name",
)
USER_PACKAGES_MISSING = _entry(
    "user-packages-missing", "apm install -g <package>", "install packages globally"
)
USER_INSTRUCTIONS_MISSING = _entry(
    "user-instructions-missing", "apm install -g <package>", "add global instructions"
)
USER_MANIFEST_INVALID = _entry(
    "user-manifest-invalid",
    "apm compile -g",
    "compile the user scope",
    before="fix {path}",
)
COMPILED_OUTPUTS_ORPHANED = _entry(
    "compiled-outputs-orphaned",
    "apm compile --clean",
    "remove the orphaned outputs (watch mode ignores --clean)",
)
TARGET_NOT_DEPLOYED = _entry(
    "target-not-deployed", "apm install --target {target}", "deploy to {target}"
)
STAGED_BUNDLE_DRIFT = _entry(
    "staged-bundle-drift", "apm install <bundle> --force", "restage the bundle"
)
INIT_TARGETS_UNDETECTED = _entry(
    "init-targets-undetected",
    "apm init --target <target,...>",
    "choose the targets explicitly",
)
MARKETPLACE_BUILD_REMOVED = _entry(
    "marketplace-build-removed",
    "apm pack",
    "produce marketplace.json from the apm.yml 'marketplace:' block",
)
MARKETPLACE_CONFIG_MISSING = _entry(
    "marketplace-config-missing", "apm marketplace init", "scaffold a marketplace config"
)
MARKETPLACE_CONFIG_LEGACY = _entry(
    "marketplace-config-legacy", "apm marketplace migrate", "fold marketplace.yml into apm.yml"
)
MARKETPLACE_CONFIG_DUPLICATE = _entry(
    "marketplace-config-duplicate",
    "apm marketplace migrate --force",
    "consolidate marketplace.yml into apm.yml",
)
MARKETPLACE_JSON_MISSING = _entry(
    "marketplace-json-missing", "apm pack", "generate .claude-plugin/marketplace.json"
)
MARKETPLACE_ADD_FAILED = _entry(
    "marketplace-add-failed",
    "apm marketplace add <source>",
    "register the marketplace",
    before="fix the cause",
)
MARKETPLACES_NONE_REGISTERED = _entry(
    "marketplaces-none-registered",
    "apm marketplace add <source>",
    "register one (OWNER/REPO, HTTPS URL, SSH URL, or local path)",
)
MARKETPLACE_SEARCH_FORMAT = _entry(
    "marketplace-search-format",
    "apm search <query>@<marketplace>",
    "search one registered marketplace",
)
MCP_REGISTRY_URL_OVERRIDE = _entry(
    "mcp-registry-url-override",
    "apm config unset mcp-registry-url",
    "use the default MCP registry",
)
AGENT_PLUGIN_BUNDLE_CONSUMER = _entry(
    "agent-plugin-bundle-consumer",
    "apm install --target copilot",
    "deploy the bundle",
    before="add the bundle path under dependencies.apm in apm.yml",
)
LEGACY_BUNDLE_FORMAT = _entry(
    "legacy-bundle-format",
    "apm install <bundle-path>",
    "install the bundle",
    before="repack with the default plugin format",
)
BUNDLE_TARGET_MISMATCH = _entry(
    "bundle-target-mismatch",
    "apm pack",
    "produce a {target}-targeted bundle",
    before="ask the publisher to declare targets: [{target}] in apm.yml",
)
AUTH_TOKEN_UNEXPORTED = _entry(
    "auth-token-unexported",
    "apm auth {host} --export",
    "print an export line your shell can eval",
)
RUNTIME_MISSING = _entry(
    "runtime-missing", "apm runtime setup copilot", "install the primary runtime"
)

HIDDEN_CHARACTERS_FOUND = _entry(
    "hidden-characters-found",
    "apm audit --strip",
    "remove the hidden characters",
    before="review the file contents",
)
HIDDEN_CHARACTERS_PREVIEWED = _entry(
    "hidden-characters-previewed", "apm audit --strip", "apply the previewed removals"
)
FROZEN_UPDATE_CONFLICT = _entry(
    "frozen-update-conflict",
    "apm update",
    "refresh the locked refs",
    then="use 'apm install --frozen' in CI",
)
BUNDLE_REPACK_PLUGIN = _entry(
    "bundle-repack-plugin",
    "apm pack --claude-plugin --archive",
    "repack the source as a plugin-format bundle archive",
)
LEGACY_BUNDLE_UNPACK = _entry(
    "legacy-bundle-unpack", "apm unpack <bundle-path>", "deploy the legacy bundle"
)
REGISTRY_PACKAGE_INSTALL = _entry(
    "registry-package-install", "apm install <org/package>", "install from the registry instead"
)
PROJECT_MANIFEST_BOOTSTRAP = _entry(
    "project-manifest-bootstrap", "apm install <org/repo>", "create apm.yml with that package"
)

# -- end commands/** (W1) -----------------------------------------------------

# -- W3 (every other src/apm_cli module) -------------------------------------
# -- W3 deps-compilation --
# Advice printed by src/apm_cli/deps and src/apm_cli/compilation.

ROOT_INSTRUCTIONS_HAND_AUTHORED = _entry(
    "root-instructions-hand-authored",
    "apm compile",
    "regenerate it",
    before="delete or rename it, or prepend the line '{marker}' to the top of the file",
)
ROOT_CONTEXT_HAND_AUTHORED = _entry(
    "root-context-hand-authored",
    "apm compile",
    "regenerate it",
    before="delete or rename the file",
)
NESTED_REPOSITORY_SKIPPED = _entry(
    "nested-repository-skipped",
    "apm compile",
    "compile it separately",
    before="change to {root}",
)
SINGLE_AGENTS_COMPILATION = _entry(
    "single-agents-compilation",
    "apm compile --single-agents",
    "compile one traditional AGENTS.md file instead",
)
ORPHANED_AGENTS_MD = _entry(
    "orphaned-agents-md", "apm compile --clean", "remove the orphaned AGENTS.md files"
)
IMMUTABLE_REQUIREMENTS_CONFLICT = _entry(
    "immutable-requirements-conflict",
    "apm install",
    "regenerate apm.lock.yaml",
    before="align the requested refs in the root or parent manifests to the same commit",
)
IMMUTABLE_REQUIREMENTS_CONFLICT_FROZEN = _entry(
    "immutable-requirements-conflict-frozen",
    "apm install",
    "regenerate apm.lock.yaml (--frozen never rewrites it)",
    before="align the requested refs in the root or parent manifests to the same commit",
)
FROZEN_LOCKFILE_STALE = _entry(
    "frozen-lockfile-stale",
    "apm install",
    "regenerate apm.lock.yaml (--frozen never rewrites it)",
)
PROTOCOL_FALLBACK_DISALLOWED = _entry(
    "protocol-fallback-disallowed",
    "apm config set allow-protocol-fallback true",
    "allow cross-protocol fallback (not recommended)",
)

# -- end W3 deps-compilation --
# -- W3 plugins-runtime --
# Advice printed by adapters/, agent_plugins/, claude_plugins/, codex_plugins/,
# copilot_plugins/ and runtime/.
MCP_ENV_VARS_UNRESOLVED = _entry(
    "mcp-env-vars-unresolved",
    "apm install",
    "write the resolved values into the MCP server configuration",
    before="set {names} in your environment",
)
JETBRAINS_MCP_CONFIG_UNUSABLE = _entry(
    "jetbrains-mcp-config-unusable",
    "apm install --target intellij",
    "reconcile the JetBrains Copilot MCP config",
    before="{fix}",
)
MCP_CONTAINER_OPTION_UNRESOLVED = _entry(
    "mcp-container-option-unresolved",
    "apm install",
    "configure the container MCP server for this target",
    before="set the required registry runtime variables",
)
AGENT_PLUGIN_LEGACY_REPACK = _entry(
    "agent-plugin-legacy-repack",
    "apm pack --claude-plugin",
    "produce a legacy-compatible package yourself",
)
AGENT_PLUGIN_BUNDLE_ROUTE = _entry(
    "agent-plugin-bundle-route",
    "apm install --target copilot",
    "materialize it under apm_modules and register it natively with GitHub Copilot",
    before="declare the plugin as a dependency in apm.yml (a local path works)",
)
CLAUDE_PLUGIN_BIN_UNTRUSTED = _entry(
    "claude-plugin-bin-untrusted",
    "apm install --global --trust-bin",
    "trust its bin/ executables and register the plugin",
)
CLAUDE_PLUGIN_SOURCE_STALE = _entry(
    "claude-plugin-source-stale",
    "apm install --global",
    "restore the native Claude plugin from apm.lock.yaml",
)
CLAUDE_PLUGIN_BINDING_MOVED = _entry(
    "claude-plugin-binding-moved",
    "apm install --global",
    "register the native Claude plugins under this APM home",
    before="remove the old entries from Claude settings and ~/.agents/plugins, "
    "delete apm.lock.yaml",
)
CODEX_PLUGIN_USER_SCOPE = _entry(
    "codex-plugin-user-scope",
    "apm install -g --target codex",
    "register Codex compatibility plugins at user scope",
)
CODEX_NATIVE_CACHE_DIFFERS = _entry(
    "codex-native-cache-differs",
    "apm install -g --frozen --target codex",
    "rebuild it from the admitted source",
    before="remove only this dependency's disposable apm_modules directory",
)
CODEX_PROVENANCE_UNINSTALL = _entry(
    "codex-provenance-uninstall",
    "apm uninstall -g {dependency}",
    "remove the dependency whose provenance is missing",
    before="save its pinned declaration",
    then="{reinstall}",
)
CODEX_PROVENANCE_REINSTALL = _entry(
    "codex-provenance-reinstall",
    "apm install -g <saved-pinned-dependency> --target codex",
    "reinstall it with recorded source provenance",
)
CODEX_NATIVE_REGISTRATION_UNVERIFIED = _entry(
    "codex-native-registration-unverified",
    "apm install -g --target codex",
    "register the Codex plugin again from its admitted source",
)
COPILOT_SETTINGS_INVALID = _entry(
    "copilot-settings-invalid",
    "apm install",
    "register the Agent Plugin and install the packages",
    before="fix or delete the file",
)
LLM_TLS_BOOTSTRAP_MISSING = _entry(
    "llm-tls-bootstrap-missing",
    "apm runtime setup llm",
    "install the OS-trust bootstrap into the llm venv",
    before="use Python 3.10+ or set PIP_CERT to your proxy CA bundle",
)
# -- end W3 plugins-runtime --
# -- W3 core-models --
# Advice printed by core/, models/, security/, utils/, output/,
# update_policy.py and drift.py.

GITHUB_AUTH_FIRST_ENABLED = _entry(
    "github-auth-first-enabled",
    "apm config unset github-auth-first",
    "restore anonymous-first auth for github.com",
)
CLAUDE_PLUGIN_LAYOUT = _entry(
    "claude-plugin-layout",
    "apm pack --claude-plugin",
    "produce the historical Claude plugin layout",
)
LOCKED_REFS_SATISFIED = _entry("locked-refs-satisfied", "apm update", "resolve the latest refs")
DRY_RUN_INCOMPLETE = _entry("dry-run-incomplete", "apm install", "resolve the full dependency plan")
DEPLOYMENT_OWNERS_INVALID = _entry(
    "deployment-owners-invalid",
    "apm prune",
    "repair the invalid deployment ownership records",
    then="rerun 'apm audit'",
)
PLUGIN_MCP_OWNERSHIP_UNKNOWN = _entry(
    "plugin-mcp-ownership-unknown",
    "apm install",
    "regenerate apm.lock.yaml",
    before="fix or delete the unreadable apm.lock.yaml",
    then="run 'apm pack' again",
)
FROZEN_MARKETPLACE_UNLOCKED = _entry(
    "frozen-marketplace-unlocked",
    "apm install",
    "lock the marketplace dependency",
    then="commit apm.lock.yaml",
)
MARKETPLACE_CATALOG_REPINNED = _entry(
    "marketplace-catalog-repinned",
    "apm update {package}",
    "move to the catalog's selection",
)
REGISTRY_TOKEN_IN_MANIFEST = _entry(
    "registry-token-in-manifest",
    "apm config set registry.{name}.token <value>",
    "store the token outside apm.yml",
)
DEPENDENCY_ALIAS_INVALID = _entry(
    "dependency-alias-invalid",
    "apm install",
    "install the dependency under the corrected alias",
    before="{fix}",
)
PACKAGE_DIRECTORY_COLLISION = _entry(
    "package-directory-collision",
    "apm install",
    "re-materialize the intended package",
    before="{fix}",
)
ADO_COORDINATES_MISMATCHED = _entry(
    "ado-coordinates-mismatched",
    "apm install <original-ado-url>",
    "regenerate its state from the original Azure DevOps URL",
)
SUBSET_PAIRINGS_INCONSISTENT = _entry(
    "subset-pairings-inconsistent",
    "apm install",
    "regenerate the skill and target subset records in apm.lock.yaml",
)
COMPILE_DRY_RUN = _entry("compile-dry-run", "apm compile", "write the compiled files")
HIDDEN_CHARACTERS_FLAGGED = _entry(
    "hidden-characters-flagged", "apm audit --strip", "remove the flagged hidden characters"
)
HIDDEN_CHARACTERS_STRUCTURED = _entry(
    "hidden-characters-structured",
    "apm audit --strip",
    "remove flagged characters from eligible regular documents",
    before="review structured prompt fields manually",
)
MCP_EXECUTABLES_UNTRUSTED = _entry(
    "mcp-executables-untrusted",
    "apm approve {package}",
    "trust its server executables",
    before="run 'apm policy explain {package}' and confirm policy permits it",
)
EXECUTABLES_IDENTITY_UNLOCKED = _entry(
    "executables-identity-unlocked",
    "apm install",
    "regenerate apm.lock.yaml",
    then="approve {subject}",
)
EXECUTABLES_PIN_MALFORMED = _entry(
    "executables-pin-malformed", "apm approve {package}", "record a valid sha256 pin"
)
LOCAL_FILES_COLLIDE = _entry(
    "local-files-collide", "apm install --force", "overwrite the unmanaged local files"
)
GITHUB_GITLAB_HOST_CONFLICT = _entry(
    "github-gitlab-host-conflict",
    "apm install <package>",
    "resolve the shorthand against GitLab",
    before="unset GITHUB_HOST and set GITLAB_HOST={host} for this command only",
)

# -- end W3 core-models --
# -- end W3 -------------------------------------------------------------------

# -- install/policy (W4) -------------------------------------------------------
# -- W4 frozen-policy --
# -- end W4 frozen-policy --
# -- W4 drift-trust --
# -- end W4 drift-trust --
# -- W4 phases-targets --
# -- end W4 phases-targets --
# -- end install/policy (W4) ---------------------------------------------------


__all__ = [
    "DRIFT_KIND_REMEDIATION",
    "Remediation",
    "catalog",
    "either",
    "either_sentence",
]
