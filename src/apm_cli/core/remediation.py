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

# -- end commands/** (W1) -----------------------------------------------------

# -- W3 (every other src/apm_cli module) -------------------------------------
# -- W3 deps-compilation --
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
# -- end W3 core-models --
# -- end W3 -------------------------------------------------------------------


__all__ = [
    "DRIFT_KIND_REMEDIATION",
    "Remediation",
    "catalog",
    "either",
    "either_sentence",
]
