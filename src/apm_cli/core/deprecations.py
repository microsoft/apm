"""The one catalog of APM deprecations.

Each entry owns a deprecation's warning template, its replacement, the version
that deprecated it (``None``: unknown) and its removal horizon (a version, or
``None``: not scheduled). Nothing here decides *when* a warning fires: domain
owners keep triggering, dynamic replacement hints, scope and repetition, and
output owners keep rendering. Call sites read their text through
:func:`deprecation_message`; no other module may spell a catalog template
(guarded by ``registry-delegation-deprecation-catalog``).

Deprecation, rejection and deferral are different decisions: an entry records
the horizon its warning already announces and never infers a removal date.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .remediation import (
    CLI_BINARY_OUTDATED,
    DEPS_UPDATE_DEPRECATED,
    LEGACY_BUNDLE_FORMAT,
    MARKETPLACE_CONFIG_LEGACY,
    Remediation,
)

UNKNOWN_SINCE = "unknown"
NOT_SCHEDULED = "not scheduled"

# How a deprecation reaches the user today.
SIGNAL_WARNING = "warning"
SIGNAL_PYTHON = "python-warning"
SIGNAL_COLLECTED = "collected"

# What ``apm update`` does now; the self-updater deprecation and the CI banner
# both lead with it.
UPDATE_REFRESHES_DEPENDENCIES = "'apm update' refreshes APM dependencies."


@dataclass(frozen=True)
class Deprecation:
    """One deprecated surface and the words used to warn about it."""

    key: str
    surface: str
    replacement: str
    template: str
    signal: str = SIGNAL_WARNING
    since: str | None = None
    removal: str | None = None
    remedy: Remediation | None = None

    def message(self, **fields: object) -> str:
        """Render the warning text; *fields* fill the domain owner's dynamic parts.

        A ``{remedy}`` slot renders :attr:`remedy` from ``core/remediation.py``,
        which owns every "run 'apm ...'" sentence.
        """
        if self.remedy is not None:
            fields = {"remedy": self.remedy.sentence(), **fields}
        return self.template.format(**fields)


@dataclass(frozen=True)
class WarningFreeDeprecation:
    """A surface the code calls deprecated that prints no warning today."""

    surface: str
    replacement: str
    location: str
    kind: str


_CATALOG: tuple[Deprecation, ...] = (
    Deprecation(
        key="config-auto-integrate",
        surface="`apm config` key `auto-integrate`",
        replacement="None (it has no effect); remove it",
        template=(
            "'auto-integrate' is deprecated and has no effect: no command reads it. "
            "It will be removed in a future release."
        ),
        since="0.37.0",
    ),
    Deprecation(
        key="target-alias-vscode",
        surface="`vscode` target alias",
        replacement="`copilot`",
        template=(
            "replace `{token}` with `{replacement}`{location}; "
            "`{token}` is deprecated and will be removed in a future release."
        ),
        since="0.37.0",
    ),
    Deprecation(
        key="manifest-target-key",
        surface="Singular `target:` in `apm.yml`",
        replacement="`targets:` (the warning prints the exact value)",
        template=(
            "{label} uses the deprecated 'target:' key; 'target:' will be removed in a "
            "future release in favor of 'targets:'. {hint}"
        ),
        since="0.37.0",
    ),
    Deprecation(
        key="manifest-compilation-target",
        surface="`compilation.target` in `apm.yml`",
        replacement="None (ignored); delete it",
        template=(
            "{label} sets 'compilation.target', which APM ignores; targets come from "
            "'targets:' or auto-detection. Delete it; a future release will reject it."
        ),
        since="0.37.0",
    ),
    Deprecation(
        key="manifest-targets-all",
        surface="`all` in `apm.yml` `targets:`",
        replacement="Omit the field",
        template=(
            "'all' in apm.yml targets is deprecated and will become a hard "
            "error in a future release. APM is treating the field as omitted "
            "so the current command's default target selection applies. If you "
            "maintain this package, remove 'all' from the manifest."
        ),
    ),
    Deprecation(
        key="compile-target-all",
        surface="`apm compile --target all`",
        replacement="`apm compile --all`",
        template="'--target all' is deprecated; use '--all' instead.",
    ),
    Deprecation(
        key="allow-executables",
        surface="`allowExecutables:` in `apm.yml`",
        replacement="`executables.allow` (migrated by `apm approve`/`apm deny`)",
        template=(
            "'allowExecutables' in apm.yml is deprecated; it now maps to "
            "'executables.allow'. It will migrate automatically on your next "
            "'apm approve'/'apm deny'."
        ),
        since="0.22.0",
    ),
    Deprecation(
        key="policy-bin-deploy",
        surface="`bin_deploy:` in `apm-policy.yml`",
        replacement="`executables` (`deny_all`/`deny`)",
        template=(
            "'bin_deploy' is deprecated; use 'executables' (deny_all/deny) instead. "
            "bin_deploy is still honored as a bin-scoped deny alias for one minor cycle."
        ),
        since="0.22.0",
    ),
    Deprecation(
        key="pack-target",
        surface="`apm pack --target`",
        replacement="None (bundles are target-agnostic)",
        template=(
            "--target is deprecated and will be removed in a future release. "
            "Bundles are target-agnostic; the value is recorded as informational "
            "pack.target metadata only and is ignored by 'apm install'."
        ),
    ),
    Deprecation(
        key="unpack",
        surface="`apm unpack`",
        replacement="`apm install <bundle-path>`",
        template="'apm unpack' is deprecated and will be removed in a future release. {remedy}",
        remedy=LEGACY_BUNDLE_FORMAT,
    ),
    Deprecation(
        key="deps-update",
        surface="`apm deps update`",
        replacement="`apm update`",
        template=(
            "'apm deps update' is deprecated. {remedy} "
            "'apm update' now supports -g/--global, [PACKAGES]..., --force, "
            "--parallel-downloads, --target, and --legacy-skill-paths, plus an "
            "interactive plan, --dry-run, and --yes."
        ),
        since="0.17.0",
        remedy=DEPS_UPDATE_DEPRECATED,
    ),
    Deprecation(
        key="update-self-updater",
        surface="`apm update` outside an `apm.yml` project",
        replacement="`apm self-update`",
        template=f"{UPDATE_REFRESHES_DEPENDENCIES} {{remedy}} Forwarding for back-compat (deprecated).",
        since="0.13.0",
        remedy=CLI_BINARY_OUTDATED,
    ),
    Deprecation(
        key="update-check",
        surface="`apm update --check`",
        replacement="`apm update --dry-run` or `apm self-update --check`",
        template=(
            "'apm update --check' is the deprecated self-updater shim. "
            "Use 'apm update --dry-run' to preview dependency changes, "
            "or 'apm self-update --check' to check for a new CLI binary. "
            "Forwarding for back-compat (deprecated)."
        ),
    ),
    Deprecation(
        key="marketplace-yml",
        surface="Standalone `marketplace.yml`",
        replacement="`marketplace:` block in `apm.yml` (`apm marketplace migrate`)",
        template="marketplace.yml is deprecated. {remedy}",
        since="0.11.0",
        remedy=MARKETPLACE_CONFIG_LEGACY,
    ),
    Deprecation(
        key="marketplace-outputs-list",
        surface="List or string form of `marketplace.outputs`",
        replacement="Map form (`outputs: {claude: {}}`)",
        template=(
            "outputs: [{names}] is deprecated; use the map form:\n\n      outputs:\n{map_lines}"
        ),
        since="0.14.0",
    ),
    Deprecation(
        key="target-agents",
        surface="`--target agents`",
        replacement="`--target copilot` or `--target agent-skills`",
        template=(
            "'--target agents' is deprecated -- it maps to 'copilot' (.github/), "
            "not '.agents/'. Use '--target copilot' or '--target agent-skills' "
            "(.agents/skills/). Removal in v1.0."
        ),
        since="0.12.0",
        removal="1.0",
    ),
    Deprecation(
        key="mcp-angle-var",
        surface="`<VAR>` placeholders in MCP server config",
        replacement="`${VAR}`",
        template=(
            "Deprecated: <VAR> placeholder syntax used in {count} {noun} "
            "({servers}). Migrate to ${{VAR}} in apm.yml. "
            "<VAR> support will be removed in v1.0."
        ),
        removal="1.0",
    ),
    Deprecation(
        key="registry-get-server-info",
        surface="Python API `SimpleRegistryClient.get_server_info()`",
        replacement="`SimpleRegistryClient.get_server()`",
        template=(
            "SimpleRegistryClient.get_server_info(server_name) is deprecated; "
            "use SimpleRegistryClient.get_server(server_name, version='latest'). "
            "The parameter now means a server name per MCP Registry v0.1."
        ),
        signal=SIGNAL_PYTHON,
        since="0.14.0",
    ),
    Deprecation(
        key="artifactory-base-url",
        surface="`ARTIFACTORY_BASE_URL`",
        replacement="`PROXY_REGISTRY_URL`",
        template="ARTIFACTORY_BASE_URL is deprecated; use PROXY_REGISTRY_URL instead.",
        signal=SIGNAL_PYTHON,
        since="0.8.12",
    ),
    Deprecation(
        key="artifactory-apm-token",
        surface="`ARTIFACTORY_APM_TOKEN`",
        replacement="`PROXY_REGISTRY_TOKEN`",
        template="ARTIFACTORY_APM_TOKEN is deprecated; use PROXY_REGISTRY_TOKEN instead.",
        signal=SIGNAL_PYTHON,
    ),
    Deprecation(
        key="artifactory-only",
        surface="`ARTIFACTORY_ONLY`",
        replacement="`PROXY_REGISTRY_ONLY`",
        template="ARTIFACTORY_ONLY is deprecated; use PROXY_REGISTRY_ONLY instead.",
        signal=SIGNAL_PYTHON,
    ),
)

DEPRECATIONS: Mapping[str, Deprecation] = MappingProxyType({d.key: d for d in _CATALOG})


# Surfaces the code already calls deprecated but that print no warning. Listed
# so the inventory is complete; adding a warning to any of them is a separate
# decision.
WARNING_FREE_DEPRECATIONS: tuple[WarningFreeDeprecation, ...] = (
    WarningFreeDeprecation(
        surface="`apm install --update`",
        replacement="`apm update` (or `apm update --yes` in CI)",
        location="src/apm_cli/commands/install.py",
        kind="flag",
    ),
    WarningFreeDeprecation(
        surface="`apm marketplace add --branch`",
        replacement="`--ref`",
        location="src/apm_cli/commands/marketplace/__init__.py",
        kind="flag",
    ),
    WarningFreeDeprecation(
        surface="User `approvals.yml` store",
        replacement="`executables` in the user `config.json` (migrated on first read)",
        location="src/apm_cli/security/executables.py",
        kind="file",
    ),
    WarningFreeDeprecation(
        surface="`PolicyBlockError`",
        replacement="`PolicyViolationError`",
        location="src/apm_cli/policy/install_preflight.py",
        kind="python-api",
    ),
    WarningFreeDeprecation(
        surface="`get_lockfile_installed_paths()`",
        replacement="`LockFile.installed_paths_for_project()`",
        location="src/apm_cli/deps/lockfile.py",
        kind="python-api",
    ),
    WarningFreeDeprecation(
        surface="`HookIntegrator.integrate_package_hooks_{claude,cursor,codex}()`",
        replacement="`HookIntegrator.integrate_hooks_for_target()`",
        location="src/apm_cli/integration/hook_integrator.py",
        kind="python-api",
    ),
    WarningFreeDeprecation(
        surface="`CommandIntegrator.sync_integration()`, `.sync_integration_opencode()`",
        replacement="`sync_for_target()`",
        location="src/apm_cli/integration/command_integrator.py",
        kind="python-api",
    ),
    WarningFreeDeprecation(
        surface="`AgentIntegrator.sync_integration_{cursor,opencode}()`",
        replacement="`sync_for_target()`",
        location="src/apm_cli/integration/agent_integrator.py",
        kind="python-api",
    ),
    WarningFreeDeprecation(
        surface="`InstructionIntegrator.sync_integration_{cursor,claude}()`",
        replacement="`sync_for_target()`",
        location="src/apm_cli/integration/instruction_integrator.py",
        kind="python-api",
    ),
    WarningFreeDeprecation(
        surface="`InstallLogger.download_complete(ref_suffix=)`",
        replacement="`ref=` and `sha=`",
        location="src/apm_cli/core/command_logger.py",
        kind="python-api",
    ),
    WarningFreeDeprecation(
        surface="`render_marketplace_yml_template()`",
        replacement="`render_marketplace_block()`",
        location="src/apm_cli/marketplace/init_template.py",
        kind="python-api",
    ),
)


def deprecation_message(key: str, **fields: object) -> str:
    """Return the catalog warning text for *key*, filled with the owner's *fields*."""
    return DEPRECATIONS[key].message(**fields)


__all__ = [
    "DEPRECATIONS",
    "NOT_SCHEDULED",
    "SIGNAL_COLLECTED",
    "SIGNAL_PYTHON",
    "SIGNAL_WARNING",
    "UNKNOWN_SINCE",
    "UPDATE_REFRESHES_DEPENDENCIES",
    "WARNING_FREE_DEPRECATIONS",
    "Deprecation",
    "WarningFreeDeprecation",
    "deprecation_message",
]
