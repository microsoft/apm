"""Refusal, definition and partial-equivalence rules of the obligation set (#4099).

Every rule carries its code evidence as ``Ev(path, symbol, needle)``: the
needle must appear inside the cited symbol (a qualname, or a Markdown heading;
see ``anchors.py``), never at a line number, so an unrelated edit to the cited
file does not move the citation (#4696). ``test_generator_rules.py`` checks
every anchor. A rule is one of:

- definition: the cell is not a reachable shape (it is removed, with its rule);
- refusal: the cell is a flag combination the command refuses; every such
  cell maps onto ONE refusal cell per command shape (U1, U5);
- mapping: a partial equivalence maps the cell onto another obligation along
  non-exception factors only (U2, U10, E3);
- must-cover: the cell stays and names the rule that forces it (U2, U7, U9).

A refusal witness never discharges a success obligation: refusal cells are
their own family and nothing maps a success cell onto one unless the
success shape is unreachable.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from tests.lifecycle.generator.factors import (
    EXCEPTION_FACTORS,
    GATE_AUTHORITY,
    MODIFIERS,
    ON,
    PLAIN_PRIMS,
    cell_id,
)


@dataclass(frozen=True)
class Ev:
    path: str
    symbol: str
    needle: str

    def __str__(self) -> str:
        return f"{self.path}::{self.symbol}" if self.symbol else self.path


_INSTALL = "src/apm_cli/commands/install.py"
_APPROVE = "src/apm_cli/commands/approve.py"
_COMPILE = "src/apm_cli/commands/compile/cli.py"
_PACK = "src/apm_cli/commands/pack.py"
_MCP_VIEW = "src/apm_cli/integration/mcp_config_view.py"
_RETIRED = "src/apm_cli/install/mcp/retired_destinations.py"

# -- U1: the modifiers each command accepts -----------------------------------

ACCEPTS: Mapping[str, frozenset[str]] = {
    "install": frozenset(MODIFIERS),
    "update": frozenset({"DRYRUN", "FORCE", "SELECT"}),
    "uninstall": frozenset({"DRYRUN", "SELECT"}),
    "prune": frozenset({"DRYRUN"}),
    "compile": frozenset({"DRYRUN", "SELECT"}),
    "lock": frozenset({"SELECT", "UPDATE", "NOPOLICY"}),
    "pack": frozenset({"DRYRUN", "FORCE", "SELECT"}),
    # --update is rejected with a local bundle (install.py rejected_flags; exit 2).
    "bundle-install": frozenset({"DRYRUN", "FORCE", "SELECT", "NOPOLICY"}),
    # install --mcp accepts --target: mcp-install x SELECT is a success cell
    # (tests/integration/test_copilot_root_mcp_json_lifecycle.py).
    "mcp-install": frozenset({"DRYRUN", "FORCE", "SELECT", "NOPOLICY"}),
    "approve-deny": frozenset({"SELECT"}),
    "audit": frozenset({"DRYRUN", "SELECT", "NOPOLICY"}),
    "marketplace": frozenset({"SELECT"}),
}
ACCEPTS_EVIDENCE: Mapping[str, tuple[Ev, ...]] = {
    "install": (
        Ev(_INSTALL, "install", '"--only"'),
        Ev(_INSTALL, "install", '"--update"'),
        Ev(_INSTALL, "install", '"--dry-run"'),
        Ev(_INSTALL, "install", '"--force"'),
        Ev(_INSTALL, "install", '"--frozen/--no-frozen"'),
        Ev(_INSTALL, "install", '"--target"'),
        Ev(_INSTALL, "install", '"--no-policy"'),
    ),
    "update": (
        Ev("src/apm_cli/commands/update.py", "update", 'argument("packages"'),
        Ev("src/apm_cli/commands/update.py", "update", '"--dry-run"'),
        Ev("src/apm_cli/commands/update.py", "update", '"--force"'),
        Ev("src/apm_cli/commands/update.py", "update", '"--target"'),
    ),
    "uninstall": (
        Ev("src/apm_cli/commands/uninstall/cli.py", "uninstall", 'argument("packages"'),
        Ev("src/apm_cli/commands/uninstall/cli.py", "uninstall", '"--dry-run"'),
    ),
    "prune": (
        Ev("src/apm_cli/commands/prune.py", "prune", '"--dry-run"'),
        Ev("src/apm_cli/commands/prune.py", "prune", "def prune(ctx, dry_run)"),
    ),
    "compile": (Ev(_COMPILE, "compile", '"--target"'), Ev(_COMPILE, "compile", '"--dry-run"')),
    "lock": (
        Ev("src/apm_cli/commands/lock.py", "lock", '"--update"'),
        Ev("src/apm_cli/commands/lock.py", "lock", '"--no-policy"'),
        Ev("src/apm_cli/commands/lock.py", "lock", '"--target"'),
    ),
    "pack": (
        Ev(_PACK, "pack_cmd", '"--target"'),
        Ev(_PACK, "pack_cmd", '"--dry-run"'),
        Ev(_PACK, "pack_cmd", '"--force"'),
    ),
    "bundle-install": (Ev(_INSTALL, "install", "detect_local_bundle"),),
    "mcp-install": (Ev(_INSTALL, "install", '"--mcp"'), Ev(_INSTALL, "install", '"--target"')),
    "approve-deny": (
        Ev(_APPROVE, "approve_cmd", 'argument("packages"'),
        Ev(_APPROVE, "deny_cmd", 'argument("packages"'),
    ),
    "audit": (
        Ev("src/apm_cli/commands/audit.py", "audit", 'argument("package"'),
        Ev("src/apm_cli/commands/audit.py", "audit", '"--dry-run"'),
        Ev("src/apm_cli/commands/audit.py", "audit", '"--no-policy"'),
    ),
    "marketplace": (
        Ev("src/apm_cli/commands/marketplace/__init__.py", "add", "def add(source, name"),
    ),
}
# Refusals that exit 1 (a semantic refusal, ClickException) instead of a
# Click usage error (exit 2).
REFUSAL_EXIT: Mapping[tuple[str, ...], int] = {
    ("mcp-install", "FROZEN"): 1,
    ("bundle-install", "FROZEN"): 1,
}
REFUSAL_EVIDENCE: Mapping[tuple[str, ...], Ev] = {
    ("mcp-install", "FROZEN"): Ev(_INSTALL, "install", 'reject_frozen_mutation(frozen, "--mcp")'),
    ("bundle-install", "FROZEN"): Ev(_INSTALL, "install", '"positional packages"'),
    ("mcp-install", "UPDATE"): Ev(
        "src/apm_cli/install/mcp/conflicts.py",
        "validate_mcp_conflicts",
        "--update does not apply to --mcp",
    ),
    ("bundle-install", "UPDATE"): Ev(_INSTALL, "install", '"--update": update'),
    # Verified on the real CLI: exit 2 at project and user scope, lock unchanged.
    ("install", "FROZEN", "UPDATE"): Ev(_INSTALL, "install", "--frozen and --update are mutually"),
}
# Refusal cells whose message the covering replay pins (a regex over the output).
REFUSAL_MESSAGES: Mapping[tuple[str, ...], str] = {
    ("bundle-install", "UPDATE"): r"not valid with a local bundle install",
}
# Commands with a user-scope form; any other command refuses -g (U5 for pack).
USER_SCOPE_OPS = frozenset(
    {
        "install",
        "update",
        "uninstall",
        "compile",
        "lock",
        "bundle-install",
        "mcp-install",
        "approve-deny",
    }
)
USER_SCOPE_EVIDENCE: Mapping[str, Ev] = {
    "install": Ev(_INSTALL, "install", '"--global"'),
    "update": Ev("src/apm_cli/commands/update.py", "update", '"--global"'),
    "uninstall": Ev("src/apm_cli/commands/uninstall/cli.py", "uninstall", '"--global"'),
    "compile": Ev(_COMPILE, "compile", '"--global"'),
    "lock": Ev("src/apm_cli/commands/lock.py", "lock", '"--global"'),
    "approve-deny": Ev(_APPROVE, "approve_cmd", '"--user"'),
    # U5: pack has no --global (options 163-301).
    "pack": Ev(_PACK, "pack_cmd", '@click.command(name="pack"'),
    "prune": Ev("src/apm_cli/commands/prune.py", "prune", "def prune(ctx, dry_run)"),
    "audit": Ev("src/apm_cli/commands/audit.py", "audit", "def audit("),
    "marketplace": Ev(
        "src/apm_cli/commands/marketplace/__init__.py", "add", "def add(source, name"
    ),
}

# -- Rule evidence -------------------------------------------------------------

RULES: Mapping[str, tuple[str, tuple[Ev, ...]]] = {
    "U1": ("a modifier the command does not accept is one refusal cell per command", ()),
    "U2": (
        "the executable gate governs only executable primitives; a plain file next to a "
        "revoked executable must survive; an org policy dependency deny removes it",
        (
            Ev(_APPROVE, "approve_cmd", "hooks, MCP, LSP, bin, canvas"),
            Ev(
                "src/apm_cli/policy/install_preflight.py",
                "run_policy_preflight",
                "Install blocked by org policy",
            ),
        ),
    ),
    "U3": (
        "edited is defined relative to a receipt; present with unknown provenance has no lock",
        (
            Ev(
                "src/apm_cli/core/deployment_ledger.py",
                "DeploymentLedgerCodec",
                "class DeploymentLedgerCodec",
            ),
        ),
    ),
    "U4": (
        "a declared target change without a receipt is reachable; only receipt-backed "
        "contraction needs a prior lock",
        (
            Ev(
                "src/apm_cli/install/manifest_reconcile.py",
                "reconcile_target_deployed_files",
                "canonical target-contraction owner",
            ),
        ),
    ),
    "U5": (
        "pack -g is a syntactic refusal",
        (Ev(_PACK, "pack_cmd", '@click.command(name="pack"'),),
    ),
    "U6": (
        "pack provenance is the dependencies / marketplace block, not SRC",
        (
            Ev(_PACK, "_PACK_HELP", "dependencies: block"),
            Ev(_PACK, "_PACK_HELP", "marketplace: block"),
        ),
    ),
    "U7": (
        "mcp-install acts on the requested server; other primitives keep no-collateral-write",
        (Ev(_INSTALL, "install", '"--mcp"'),),
    ),
    "U8": (
        "the source decides the command: bundle-install is install of a local bundle (install "
        "x local-bundle maps to it; no other source can state it); install --mcp takes an MCP "
        "registry name or an inline server (--url, or a command after --), never a package "
        "source, and an MCP source delivers only MCP; a package registry delivers whole "
        "packages. Parsed-then-refused shapes are refusal cells, one per message class: "
        "marketplace add of a source without a catalog, uninstall of an MCP server "
        "(a registry name is not found in apm.yml; an inline name is an MCP server), and a "
        "local bundle, which writes no manifest entry (uninstall removes only its deployed "
        "hooks and refuses a bundle with none; update and prune leave it untouched). "
        "A native plugin carries no canvas primitive",
        (
            Ev(_INSTALL, "install", "detect_local_bundle"),
            Ev(_INSTALL, "install", '"--registry"'),
            Ev(_INSTALL, "install", '"--url"'),
            Ev(
                "src/apm_cli/models/dependency/reference.py",
                "DependencyReference.parse_from_dict",
                '"registry" in entry',
            ),
            Ev(
                "src/apm_cli/marketplace/registration.py",
                "MarketplaceManifestNotFoundError.__init__",
                "No marketplace.json found",
            ),
            Ev(
                "src/apm_cli/commands/uninstall/engine.py",
                "_mcp_server_hint",
                "is an MCP server, not a package",
            ),
            Ev(
                "src/apm_cli/commands/uninstall/engine.py",
                "_validate_uninstall_packages",
                "was not found in apm.yml",
            ),
            Ev(
                "src/apm_cli/commands/uninstall/local_bundle.py",
                "uninstall_local_bundles",
                "has no hooks deployed",
            ),
            Ev(
                "src/apm_cli/commands/update.py",
                "_handle_service_only_update",
                "No APM dependencies to update",
            ),
            Ev(
                "src/apm_cli/core/command_logger.py",
                "CommandLogger.nothing_to_prune",
                "Nothing to prune",
            ),
            Ev("src/apm_cli/integration/canvas_integrator.py", "", ".apm/extensions/"),
        ),
    ),
    "U-BIN": (
        "bin/ reaches project scope only inside a trusted whole Claude plugin: loose bin/ "
        "deploys only for a marketplace plugin at user scope with a Claude target that "
        "supports skills (plugin_bin_deployable), but a plugin that ships bin/ deploys whole "
        "to .agents/plugins/<plugin>/ under its whole-plugin approval and --trust-bin "
        "(claude_plugins/admission.py), and prune retires that root, bin/ included "
        "(native_plugins/uninstall.py). So every project-scope bin cell is reachable and "
        "U-BIN removes none; the M-GATE plugin bin spine witnesses them. Self-reinstating: "
        "the generator hashes the pinned elements (generator/bin_pin.py); when the hash "
        "differs from U_BIN_PIN the project-scope bin cells become must-cover and --check "
        "fails until a reviewed change updates the pin. The closed-world pins of the bin/ "
        "writer and unpack live with the gate tables (T3)",
        (
            Ev(
                "src/apm_cli/install/exec_gate.py",
                "plugin_bin_deployable",
                "PackageType.MARKETPLACE_PLUGIN",
            ),
            Ev("src/apm_cli/install/exec_gate.py", "plugin_bin_deployable", "InstallScope.USER"),
            Ev(
                "src/apm_cli/claude_plugins/admission.py",
                "plan_native_claude_plugin",
                "elif ships_bin and skip_bin:",
            ),
            Ev("src/apm_cli/native_plugins/record.py", "PLUGINS_RELATIVE", ".agents/plugins"),
            Ev(
                "src/apm_cli/native_plugins/uninstall.py",
                "retire_gated_native_plugins",
                "gate_retirements(",
            ),
            Ev(
                "docs/src/content/docs/reference/cli/prune.md",
                "",
                "is retired as one unit: prune removes its root and its declarations",
            ),
        ),
    ),
    "U-BIN-OPS": (
        "a gate level of PRIM=bin under pack, marketplace or a local bundle "
        "install is vacuous (OP x PRIM x GATE only): a loose bin/ deploys only for a "
        "marketplace plugin at user scope (exec_gate.plugin_bin_deployable), these "
        "commands have no user-scope form and write no plugin root, and a local bundle "
        "is never a marketplace plugin, so no gate level ever reaches a bin/ there (the "
        "#4575 review: an absence U-BIN guarantees proves nothing about the gate). prune "
        "is not listed: it retires a trusted project-scope whole plugin's root, bin/ "
        "included (#4559, #4808); nor is audit: its drift replay fingerprints that root "
        "(drift.verify_claude_native_cache). An org-policy level stays its own cell (U2 "
        "no-collapse); OP x SRC x PRIM and OP x PRIM cells keep their absence witnesses. "
        "Under marketplace that org-policy level is a real cell (#4894): a trusted "
        "project-scope plugin deploys whole, bin/ included, and the plugin bin spine "
        "checks that marketplace leaves that bin/ unchanged under the deny",
        (
            Ev(
                "src/apm_cli/install/exec_gate.py",
                "plugin_bin_deployable",
                "PackageType.MARKETPLACE_PLUGIN",
            ),
            Ev("src/apm_cli/install/exec_gate.py", "plugin_bin_deployable", "InstallScope.USER"),
        ),
    ),
    "U-BIN-BUNDLE": (
        "a local bundle never deploys bin/, at any scope or gate level (#4095 lane T-1c, "
        "QL ruling): integrate_local_bundle routes every packed file through "
        "bundle_deploy_relative_path, which keeps only files under a primitive a target "
        "deploys (_lower_to_target, _is_primitive_deploy_path); no target declares a bin "
        "primitive, and the only bin/ deploy decision (exec_gate.plugin_bin_deployable) "
        "belongs to the package pipeline, which the bundle side door never calls. So every "
        "PRIM=bin cell of OP=bundle-install or SRC=local-bundle is vacuous for bin/, the "
        "org-policy level included: the bundle spine still witnesses the deny's refusal "
        "itself. Self-reinstating: bin_pin.bundle_bin_digest hashes the routing elements; "
        "when it differs from U_BIN_BUNDLE_PIN the cells become must-cover, and the "
        "tripwire test_no_target_routes_a_bundle_bin_file fails if any target ever routes "
        "a bundle bin/ path",
        (
            Ev(
                "src/apm_cli/install/services.py",
                "integrate_local_bundle",
                "_deploy_rel = _bundle_rel(",
            ),
            Ev(
                "src/apm_cli/install/local_bundle_paths.py",
                "bundle_deploy_relative_path",
                "return _lower_to_target(deploy_rel, target, rendering_extension)",
            ),
            Ev(
                "src/apm_cli/install/local_bundle_paths.py",
                "_lower_to_target",
                "if not _is_primitive_deploy_path(rel_path, target):",
            ),
            Ev(
                "src/apm_cli/install/exec_gate.py",
                "plugin_bin_deployable",
                "PackageType.MARKETPLACE_PLUGIN",
            ),
        ),
    ),
    "U-BIN-SRC": (
        "a local path whose package can deploy bin/ is a Claude plugin directory, which is "
        "the native-plugin source: bin/ deploys only for a MARKETPLACE_PLUGIN package, "
        "loose at user scope (exec_gate.plugin_bin_deployable) or whole at project scope "
        "(admission.plan_native_claude_plugin), and a local directory with an eligible "
        "apm.yml is an APM package (format_detection.NormalizationPlanner.plan), never a "
        "marketplace plugin. So OP x SRC=local-path x PRIM=bin maps to the same cell with "
        "SRC=native-plugin, which the M-SOURCE bin spine witnesses. Self-reinstating: the "
        "mapping holds only while the U-BIN pin holds (generator/bin_pin.py)",
        (
            Ev(
                "src/apm_cli/install/exec_gate.py",
                "plugin_bin_deployable",
                "PackageType.MARKETPLACE_PLUGIN",
            ),
            Ev(
                "src/apm_cli/claude_plugins/admission.py",
                "plan_native_claude_plugin",
                "package_type is not PackageType.MARKETPLACE_PLUGIN",
            ),
            Ev(
                "src/apm_cli/models/format_detection.py",
                "NormalizationPlanner.plan",
                "return PackageType.APM_PACKAGE, None",
            ),
        ),
    ),
    "U9": (
        "approve/deny with no prior decision is reachable; --list and --pending are read-only",
        (
            Ev(_APPROVE, "approve_cmd", "if list_decisions:"),
            Ev(_APPROVE, "approve_cmd", "if pending:"),
        ),
    ),
    "U10": (
        "compile renders hook/mcp/lsp only; --validate reads MCP declarations",
        (
            Ev(_COMPILE, "_run_validation_mode", "def _run_validation_mode"),
            Ev(_COMPILE, "_run_validation_mode", "get_mcp_dependencies"),
        ),
    ),
    "U-MKT": (
        "marketplace commands act on catalogs, never on deployed primitives: add, list, "
        "browse, update, remove and validate read or refresh the catalog cache, and the "
        "authoring commands edit the marketplace block; a primitive reaches a project only "
        "through install of plugin@marketplace (OP=install, SRC=marketplace-*). Every "
        "OP=marketplace x PRIM cell but the org-policy GATE level (U2 no-collapse) maps onto "
        "one rule cell witnessed at the allowed and the revoked GATE level (deployed "
        "surfaces and the lock byte-identical)",
        (
            Ev("src/apm_cli/commands/marketplace/__init__.py", "add", "def add(source, name"),
            Ev(
                "docs/src/content/docs/reference/cli/marketplace.md",
                "`apm marketplace update [NAME]`",
                "apm marketplace update",
            ),
            Ev(
                "docs/src/content/docs/consumer/installing-from-marketplaces.md",
                "How marketplace installs lock",
                "refreshes the cached",
            ),
        ),
    ),
    "U-FIRST-PARTY-MCP": (
        "an MCP server the project declares in its own apm.yml is first-party: the "
        "executables gate governs servers from dependencies and never parks it, so every "
        "executable-gate level of an MCP source is one must-cover cell that witnesses it "
        "(an org policy dependency deny still applies). apm approve / apm deny decide only "
        "installed packages, so approve-deny of an MCP source maps there too (R5)",
        (
            Ev(
                _MCP_VIEW,
                "parked_mcp_message",
                "Parked MCP server '{name}' from transitive package",
            ),
            Ev(_MCP_VIEW, "parked_mcp_message", "Parked MCP server '{name}' ({package})"),
            Ev(
                "docs/src/content/docs/enterprise/control-executables.mdx",
                "How the gate works",
                "from dependencies",
            ),
            Ev(
                "docs/src/content/docs/consumer/install-mcp-servers.md",
                "Servers that come from dependencies",
                "Re-declare it",
            ),
            Ev(
                "src/apm_cli/commands/approve.py",
                "scan_installed_executable_packages",
                "apm_modules",
            ),
        ),
    ),
    "U11": (
        "--frozen refuses any declared-target change before it changes anything: a dropped "
        "target (#4517) and an added one. A changed TGT with FROZEN maps to one refusal cell, "
        "witnessed by the T-6a history scenario",
        (
            Ev(
                "src/apm_cli/install/service.py",
                "InstallService._refuse_frozen_dropped_targets",
                "no longer declares target(s)",
            ),
            Ev(
                "src/apm_cli/install/frozen_withdrawal.py",
                "refuse_unrecorded_deployments",
                "does not record everything this install deploys",
            ),
            Ev(
                "src/apm_cli/install/service.py",
                "frozen_services_error",
                "cannot replay the selected native services",
            ),
        ),
    ),
    "U-MCPOWN-USER": (
        "the retired MCP owners (vscode, legacy-receipt) exist only at project scope: APM "
        "<= 0.36 wrote them to project files and the retired-claim reader returns no claims "
        "at user scope, so a user-scope cell with a retired owner is unreachable and removed",
        (
            Ev(_RETIRED, "", "APM <= 0.36 wrote ``copilot`` project MCP servers"),
            Ev(_RETIRED, "split_mcp_claims", "if user_scope:"),
            Ev(_RETIRED, "split_mcp_claims", "return RetiredMcpClaims({}, {})"),
        ),
    ),
    "U-USER-SCOPE-ORG-POLICY": (
        "org policy does not govern user-scope (-g) installs (#3703, a 0.39 design gap, not "
        "a defect): user scope has no git remote, so policy_discovery_uses_git_remote is "
        "False at $APM_HOME and install, lock, update and install --mcp at -g discover no "
        "org policy; the local executables gate still parks bin/ there, and project scope "
        "refuses the denied plugin with exit 1. A user-scope cell whose GATE is "
        "denied-dependency under one of those commands is removed until #3703 lands; a "
        "scope-free PRIM=bin cell is not (a whole native plugin carries bin/ at project "
        "scope, where the org deny refuses it). Self-reinstating: "
        "generator/bin_pin.py hashes the predicate, and when the hash differs from "
        "U_USER_ORG_POLICY_PIN the cells return as must-cover and --check fails",
        (
            Ev(
                "src/apm_cli/core/scope.py",
                "policy_discovery_uses_git_remote",
                "def policy_discovery_uses_git_remote",
            ),
            Ev(
                "src/apm_cli/core/scope.py",
                "policy_discovery_uses_git_remote",
                "return not is_user_scope_root(root)",
            ),
            Ev(
                "src/apm_cli/policy/discovery.py",
                "_auto_discover",
                "policy_discovery_uses_git_remote",
            ),
            Ev(
                "docs/src/content/docs/enterprise/apm-policy.mdx",
                "",
                "Org policy does not govern user-scope (`-g`) installs",
            ),
            Ev(
                "docs/src/content/docs/enterprise/security.md",
                "Threat model",
                "user-scope installs (`apm install -g`) are not governed by it",
            ),
            Ev(
                "docs/src/content/docs/reference/cli/policy.md",
                "`apm policy status`",
                "User scope has no git remote, so no org policy is discovered",
            ),
        ),
    ),
    "E1": (
        "hosts are equivalent except lock identity (HOSTID)",
        (
            Ev(
                "src/apm_cli/models/dependency/identity.py",
                "build_dependency_unique_key",
                "def build_dependency_unique_key",
            ),
        ),
    ),
    "E2": (
        "targets are equivalent except MCP ownership (MCPOWN)",
        (
            Ev(
                "src/apm_cli/install/mcp/retired_destinations.py",
                "_FIRST_ROOT_MCP_JSON_RELEASE",
                "_FIRST_ROOT_MCP_JSON_RELEASE",
            ),
            Ev(
                "src/apm_cli/install/mcp/retired_destinations.py",
                "heal_retired_project_mcp_destinations",
                "def heal_retired_project_mcp_destinations",
            ),
        ),
    ),
    "E3": (
        "file primitives are equivalent except compile-staging routing (FILEROUTE)",
        (
            Ev("src/apm_cli/install/services.py", "integrate_local_bundle", "staged_files"),
            Ev(
                "src/apm_cli/install/services.py",
                "integrate_local_bundle",
                '"instructions" not in target.primitives',
            ),
        ),
    ),
}

# -- Exception factors: the factors each one is crossed with --------------------

EXCEPTION_PARTNERS: Mapping[str, Mapping[str, tuple[str, ...] | None]] = {
    # E1: the lock key carries host and port.
    "HOSTID": {
        "OP": ("install", "update", "uninstall", "lock", "audit"),
        "SRC": ("git", "marketplace-git"),
        "LOCK": None,
    },
    # E2: MCP ownership per runtime and legacy receipts.
    "MCPOWN": {
        "OP": ("install", "update", "uninstall", "prune", "mcp-install"),
        "LOCK": None,
        "SCOPE": None,
    },
    # E3: compile-staged vs deployed file routing.
    "FILEROUTE": {
        "OP": ("install", "update", "uninstall", "prune", "compile", "bundle-install"),
        "TGT": None,
        "PRIM": PLAIN_PRIMS,
    },
}
# Ops where E3's exception (compile-staging routing) applies to a file primitive.
FILEROUTE_OPS = frozenset({"compile", "bundle-install"})
# MCPOWN values that name a retired project destination (U-MCPOWN-USER).
RETIRED_MCP_OWNERS = frozenset({"vscode", "legacy-receipt"})
MARKETPLACE_SOURCES = frozenset({"marketplace-git", "marketplace-local", "marketplace-file"})
# Sources of a single MCP server (install --mcp), not of a package.
MCP_SOURCES = frozenset({"mcp-registry", "inline-mcp"})
# U8 refusal cells: the CLI parses these shapes, then refuses them (exit 1).
MARKETPLACE_ADD_REFUSAL = "rule|U8|marketplace-add-refuses-a-source-without-catalog"
UNINSTALL_MCP_REFUSALS: Mapping[str, str] = {
    "mcp-registry": "rule|U8|uninstall-refuses-a-registry-mcp-name",
    "inline-mcp": "rule|U8|uninstall-refuses-an-inline-mcp-name",
}
# A local bundle writes no manifest entry: uninstall refuses one with no deployed
# hooks (#3700), update and prune parse and leave it untouched (one cell per message class).
LOCAL_BUNDLE_REFUSALS: Mapping[str, str] = {
    "uninstall": "rule|U8|uninstall-refuses-a-local-bundle",
    "update": "rule|U8|update-leaves-a-local-bundle-untouched",
    "prune": "rule|U8|prune-leaves-a-local-bundle-untouched",
}
# Exit code and pinned message of every refusal rule cell.
RULE_CELL_REFUSALS: Mapping[str, tuple[int, str]] = {
    MARKETPLACE_ADD_REFUSAL: (1, r"No marketplace\.json found in"),
    UNINSTALL_MCP_REFUSALS["mcp-registry"]: (1, r"was not found in apm\.yml"),
    UNINSTALL_MCP_REFUSALS["inline-mcp"]: (1, r"is an MCP server, not a package"),
    LOCAL_BUNDLE_REFUSALS["uninstall"]: (1, r"Local bundle 'p-exec' has no hooks deployed\."),
    # p-exec ships only executables the gate parks, so its bundle install records
    # nothing: update has no bundle to name (#4318 names a recorded one).
    LOCAL_BUNDLE_REFUSALS["update"]: (0, r"No APM dependencies"),
    # One canonical no-op line (#4460); the retired wordings must not match.
    LOCAL_BUNDLE_REFUSALS["prune"]: (0, r"No orphaned packages found\. Nothing to prune\."),
}
# U11: --frozen after a declared-target change. Its pinned messages (H2) and its
# witness: the T-6a history scenario, since a replay row cannot change targets.
FROZEN_TARGET_CHANGE = "refusal|TGT=changed|FROZEN=on"
FROZEN_TARGET_CHANGE_MESSAGES = (
    # narrowed: a dropped target's files are still recorded (#4517).
    r"--frozen: apm\.yml no longer declares target\(s\) .+, but apm\.lock\.yaml still"
    r" records their files\.",
    # widened: the lock records neither the added target's files nor its native
    # (MCP/LSP) ownership; whichever check runs first refuses.
    r"--frozen: apm\.lock\.yaml does not record everything this install deploys\."
    r"|--frozen cannot replay the selected native services\.",
)
# Refusal cells a scenario (not a generated replay row) witnesses.
SCENARIO_REFUSALS: Mapping[str, str] = {
    FROZEN_TARGET_CHANGE: (
        "tests/lifecycle/scenarios/test_history_upgrade.py::"
        "test_frozen_refuses_a_target_change[multi-narrowed]"
    ),
}
# Refusal rule cells whose current message is a defect: (tracking issue, a regex
# the fixed output matches). The cell is known-failing until that output appears.
RULE_CELL_KNOWN: Mapping[str, tuple[str, str]] = {}

# U-MKT: every OP=marketplace x PRIM cell maps here (Q&L ruling 2026-11-21).
MARKETPLACE_UNTOUCHED = "rule|U-MKT|marketplace-never-touches-deployed"

# U-FIRST-PARTY-MCP: the one cell every executable-gate level of an MCP source maps to.
FIRST_PARTY_MCP_CELL = "rule|U-FIRST-PARTY-MCP|root-declared-mcp-not-gated"

# Rule cells: must-cover singletons a rule names on its own.
RULE_CELLS: Mapping[str, str] = {
    MARKETPLACE_UNTOUCHED: "U-MKT",
    FIRST_PARTY_MCP_CELL: "U-FIRST-PARTY-MCP",
    "rule|U9|approve-list-read-only": "U9",
    "rule|U9|approve-pending-read-only": "U9",
    "rule|U10|compile-validate-reads-mcp": "U10",
    "rule|U10|compile-render-only-non-mutation": "U10",
    **dict.fromkeys(RULE_CELL_REFUSALS, "U8"),
}
# Rule cells whose evidence is a state fact (nothing written).
STATE_FACT_RULE_CELLS = frozenset(
    {
        MARKETPLACE_UNTOUCHED,
        FIRST_PARTY_MCP_CELL,
        "rule|U9|approve-list-read-only",
        "rule|U9|approve-pending-read-only",
        "rule|U10|compile-render-only-non-mutation",
        *RULE_CELL_REFUSALS,
    }
)


@dataclass(frozen=True)
class Verdict:
    """What the rules make of one candidate cell."""

    status: str  # "obligation" | "removed" | "mapped"
    rules: tuple[str, ...] = ()
    maps_to: str | None = None
    must_cover: bool = False


def refusal_cell(cell: Mapping[str, str]) -> tuple[str, int] | None:
    """The refusal cell id (and its exit) a cell falls into, or None."""
    if "FROZEN" in cell and cell.get("TGT", "").endswith(("widened", "narrowed")):
        return FROZEN_TARGET_CHANGE, 1
    op = cell.get("OP")
    if "FROZEN" in cell and "UPDATE" in cell and op in (None, "install"):
        return cell_id("refusal", {"OP": "install", "FROZEN": ON, "UPDATE": ON}), 2
    if op is None:
        return None
    for modifier in MODIFIERS:
        if modifier in cell and modifier not in ACCEPTS[op]:
            return (
                cell_id("refusal", {"OP": op, modifier: ON}),
                REFUSAL_EXIT.get((op, modifier), 2),
            )
    if cell.get("SCOPE") == "user" and op not in USER_SCOPE_OPS:
        return cell_id("refusal", {"OP": op, "SCOPE": "user"}), 2
    return None


def definition_rule(cell: Mapping[str, str]) -> str | None:
    """The rule that makes *cell* an unreachable shape, or None."""
    op, src, prim = cell.get("OP"), cell.get("SRC"), cell.get("PRIM")
    deployed, lock = cell.get("DEPLOYED"), cell.get("LOCK")
    if op == "pack" and src is not None:
        return "U6"
    if src is not None and (
        (op == "bundle-install" and src != "local-bundle")
        or (op == "mcp-install" and src not in MCP_SOURCES)
        or (src in MCP_SOURCES and prim is not None and prim != "mcp")
        or (src == "native-plugin" and prim == "canvas")
    ):
        return "U8"
    if u_bin_ops_applies(cell) and u_bin_pinned():
        return "U-BIN-OPS"
    if u_bin_bundle_applies(cell) and u_bin_bundle_pinned():
        return "U-BIN-BUNDLE"
    if u_user_org_policy_applies(cell) and u_user_org_policy_pinned():
        return "U-USER-SCOPE-ORG-POLICY"
    if cell.get("SCOPE") == "user" and cell.get("MCPOWN") in RETIRED_MCP_OWNERS:
        return "U-MCPOWN-USER"
    if (deployed in ("clean", "edited") and lock == "absent") or (
        deployed == "present-provenance-unknown" and lock not in (None, "absent")
    ):
        return "U3"
    return None


_BUNDLE = "bundle-install"


def _u8_target(cell: Mapping[str, str]) -> dict[str, str] | str | None:
    """Where U8 sends a source the command parses: the same command, or its refusal."""
    op, src = cell.get("OP"), cell.get("SRC")
    if op == "install" and src == "local-bundle":
        return {**cell, "OP": "bundle-install"}
    if op == "marketplace" and src is not None and src not in MARKETPLACE_SOURCES:
        return MARKETPLACE_ADD_REFUSAL
    if op == "uninstall" and src in MCP_SOURCES:
        return UNINSTALL_MCP_REFUSALS[src]
    if src == "local-bundle" and op in LOCAL_BUNDLE_REFUSALS:
        return LOCAL_BUNDLE_REFUSALS[op]
    if src == "local-bundle" and op is None:
        # Only an install states a local bundle; a modifier it refuses names that refusal.
        if any(name in cell and name not in ACCEPTS[_BUNDLE] for name in MODIFIERS):
            return {**cell, "OP": _BUNDLE}
    return None


def _source_mapping(cell: Mapping[str, str]) -> tuple[dict[str, str] | str, str] | None:
    """(target, rule) of a mapping the source decides: U8, or U-FIRST-PARTY-MCP.

    ``install --mcp NAME`` declares NAME in the root apm.yml, so the server it
    installs is first-party too: its executable-gate levels map the same way.
    """
    target = _u8_target(cell)
    if target is not None:
        return target, "U8"
    first_party = cell.get("SRC") in MCP_SOURCES or (
        cell.get("OP") == "mcp-install" and cell.get("PRIM") == "mcp" and "SRC" not in cell
    )
    if first_party and GATE_AUTHORITY.get(cell.get("GATE")) == "executable-gate":
        return FIRST_PARTY_MCP_CELL, "U-FIRST-PARTY-MCP"
    # R5: approve / deny decide only installed packages, never a root-declared server.
    if cell.get("SRC") in MCP_SOURCES and cell.get("OP") == "approve-deny" and "GATE" not in cell:
        return FIRST_PARTY_MCP_CELL, "U-FIRST-PARTY-MCP"
    return None


def _rule_cell_target(cell: Mapping[str, str]) -> tuple[str, str] | None:
    """The rule cell (and rule) U-MKT or U10 maps *cell* onto, or None.

    An org-policy GATE level is never mapped (U2 no-collapse: a dependency
    deny stays its own cell), and OP x SRC x PRIM (T3) stays with the source lane.
    """
    op, prim = cell.get("OP"), cell.get("PRIM")
    if GATE_AUTHORITY.get(cell.get("GATE")) == "org-policy":
        return None
    if op == "marketplace" and prim is not None and "SRC" not in cell:
        return MARKETPLACE_UNTOUCHED, "U-MKT"
    if op == "compile" and prim in ("hook", "lsp"):
        return "rule|U10|compile-render-only-non-mutation", "U10"
    if op == "compile" and prim == "mcp":
        return "rule|U10|compile-validate-reads-mcp", "U10"
    return None


def mapped_cell(cell: Mapping[str, str]) -> tuple[dict[str, str] | str, str] | None:
    """(target cell or rule cell id, rule) of a partial equivalence, or None.

    Mappings move only along non-exception factors: a cell that names an
    exception factor is never mapped, and an org-policy GATE level is never
    mapped (U2 keeps the two deny levels apart).
    """
    if any(name in cell for name in EXCEPTION_FACTORS):
        return None
    op, prim, gate = cell.get("OP"), cell.get("PRIM"), cell.get("GATE")
    source = _source_mapping(cell)
    if source is not None:
        return source
    if prim == "bin" and cell.get("SRC") == "local-path" and u_bin_pinned():
        return {**cell, "SRC": "native-plugin"}, "U-BIN-SRC"
    rule_target = _rule_cell_target(cell)
    if rule_target is not None:
        return rule_target
    if op == "compile" and prim in ("hook", "lsp", "mcp"):
        return None  # an org-policy level stays its own cell (U2 no-collapse)
    if (
        prim in PLAIN_PRIMS
        and GATE_AUTHORITY.get(gate) == "executable-gate"
        and gate != "revoked-after-deploy"
    ):
        return {**cell, "GATE": "none"}, "U2"
    if prim == "other-file" and op not in FILEROUTE_OPS:
        return {**cell, "PRIM": "skill"}, "E3"
    return None


def must_cover_rules(cell: Mapping[str, str]) -> tuple[str, ...]:
    op, prim, gate = cell.get("OP"), cell.get("PRIM"), cell.get("GATE")
    rules: list[str] = []
    if prim in PLAIN_PRIMS and gate in ("revoked-after-deploy", "denied-dependency"):
        rules.append("U2")
    if op == "mcp-install" and prim is not None and prim != "mcp":
        rules.append("U7")
    if op == "approve-deny" and gate == "unapproved-exec":
        rules.append("U9")
    if u_bin_applies(cell) and not u_bin_pinned():
        rules.append("U-BIN")
    if u_bin_bundle_applies(cell) and not u_bin_bundle_pinned():
        rules.append("U-BIN-BUNDLE")
    if u_user_org_policy_applies(cell) and not u_user_org_policy_pinned():
        rules.append("U-USER-SCOPE-ORG-POLICY")
    return tuple(rules)


def classify(family: str, cell: Mapping[str, str]) -> Verdict:
    """Apply definition, refusal, mapping and must-cover rules in that order."""
    rule = definition_rule(cell)
    if rule:
        return Verdict("removed", (rule,))
    refusal = refusal_cell(cell)
    if refusal:
        rule = "U5" if cell.get("OP") == "pack" and cell.get("SCOPE") == "user" else "U1"
        rule = "U11" if refusal[0] == FROZEN_TARGET_CHANGE else rule
        return Verdict("mapped", (rule,), refusal[0])
    mapped = mapped_cell(cell)
    if mapped:
        target, rule = mapped
        # No mapping may move an org-policy GATE level (U2 no-collapse).
        if GATE_AUTHORITY.get(cell.get("GATE")) == "org-policy":
            assert isinstance(target, dict) and target["GATE"] == cell["GATE"], cell
        return Verdict(
            "mapped", (rule,), target if isinstance(target, str) else cell_id(family, target)
        )
    rules = must_cover_rules(cell)
    return Verdict("obligation", rules, must_cover=bool(rules))


# -- U-BIN: bin/ at project scope only inside a whole plugin (#4894) ----------

# The approved digest of the U-BIN elements (generator/bin_pin.py). While the
# live digest equals it, the project-scope bin cells are plain obligations the
# plugin bin spine witnesses; when it differs they are must-cover and --check
# fails until a reviewed change updates it.
U_BIN_PIN = "9ac0d749836e552b423fe94cf20970c0ed1277c8a7871af859745c477d2a78bd"


def u_bin_applies(cell: Mapping[str, str]) -> bool:
    """bin/ at project scope: reachable only through a trusted whole plugin (U-BIN)."""
    return cell.get("PRIM") == "bin" and cell.get("SCOPE") == "project"


# Commands with no user-scope form that write no plugin root, and the local bundle
# install: bin/ never deploys there. prune retires a whole plugin's root, bin/ included
# (#4559, #4808), and audit's drift replay fingerprints that root (drift.py
# verify_claude_native_cache), so their bin gate levels are reachable cells.
BIN_LESS_OPS = frozenset({"pack", "marketplace", "bundle-install"})


def u_bin_ops_applies(cell: Mapping[str, str]) -> bool:
    """U-BIN-OPS: an OP x PRIM x GATE cell of bin/ under a command bin/ never reaches."""
    return (
        set(cell) == {"OP", "PRIM", "GATE"}
        and cell["PRIM"] == "bin"
        and cell["OP"] in BIN_LESS_OPS
        and GATE_AUTHORITY.get(cell["GATE"]) != "org-policy"
    )


def u_bin_pinned() -> bool:
    from tests.lifecycle.generator.bin_pin import predicate_digest

    return predicate_digest() == U_BIN_PIN


# -- U-BIN-BUNDLE: a local bundle never deploys bin/ (#4095 lane T-1c) ----------

# The approved digest of the bundle deploy routing (generator/bin_pin.py). While the
# live digest equals it, every PRIM=bin cell of a local bundle is removed; when it
# differs they are must-cover and --check fails until a reviewed change updates it.
# Re-pinned after main's bundle frontmatter-hooks fixes (2dfb0a11..37df5b73): they add one
# more skip (_declares_unapproved_hooks) to bundle_deploy_relative_path and leave
# _lower_to_target and _is_primitive_deploy_path unchanged, so routing only narrowed.
# The pin also covers integrate_local_bundle's target= on its routing call
# (bin_pin.BUNDLE_BIN_CALLER).
U_BIN_BUNDLE_PIN = "b83d2577325ef5b20d7d43c0bc0f3e1c5baa8e7cf090d1a7a0defc10f5b301f7"


def u_bin_bundle_applies(cell: Mapping[str, str]) -> bool:
    """U-BIN-BUNDLE: a PRIM=bin cell whose package arrives as a local bundle.

    A command U8 maps to a rule cell (one that refuses or ignores a local bundle)
    keeps that mapping: the refusal is proven there, whatever the bundle ships.
    install of a local bundle is bundle-install, so it is removed with it.
    """
    if cell.get("PRIM") != "bin" or not (
        cell.get("OP") == "bundle-install" or cell.get("SRC") == "local-bundle"
    ):
        return False
    mapped = mapped_cell(cell)
    return mapped is None or not isinstance(mapped[0], str)


def u_bin_bundle_pinned() -> bool:
    from tests.lifecycle.generator.bin_pin import bundle_bin_digest

    return bundle_bin_digest() == U_BIN_BUNDLE_PIN


# -- U-USER-SCOPE-ORG-POLICY: user scope discovers no org policy (#3703) ----------

# The approved digest of core/scope.policy_discovery_uses_git_remote. #3703 must
# change that decision; the digest then differs, the cells return as must-cover
# and the tripwire in test_generator_rules.py fails until the rule is dropped.
U_USER_ORG_POLICY_PIN = "7839bf8d042c59baa029c1323413078d972eebb0c45edfd8e8b14106957fd513"
# Commands an org dependency deny governs at project scope (FROZEN alone is install).
ORG_DENY_OPS = frozenset({"install", "lock", "update", "mcp-install"})


def u_user_org_policy_applies(cell: Mapping[str, str]) -> bool:
    """A user-scope cell whose promise is that an org dependency deny governs it."""
    if cell.get("GATE") != "denied-dependency":
        return False
    # Only an explicit user-scope cell: a scope-free bin cell is reachable at project
    # scope too, where a whole native plugin carries bin/ and the org deny refuses it.
    op = cell.get("OP") or ("install" if "FROZEN" in cell else None)
    return cell.get("SCOPE") == "user" and op in ORG_DENY_OPS


def u_user_org_policy_pinned() -> bool:
    from tests.lifecycle.generator.bin_pin import user_org_policy_digest

    return user_org_policy_digest() == U_USER_ORG_POLICY_PIN
