"""Refusal, definition and partial-equivalence rules of the obligation set (#4099).

Every rule carries its code evidence as ``Ev(path, line, needle)``: the
needle must appear on the cited line or within ``EVIDENCE_WINDOW`` lines
after it (``test_generator_rules.py`` checks every anchor). A rule is one of:

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

EVIDENCE_WINDOW = 8


@dataclass(frozen=True)
class Ev:
    path: str
    line: int
    needle: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}"


_INSTALL = "src/apm_cli/commands/install.py"
_APPROVE = "src/apm_cli/commands/approve.py"
_COMPILE = "src/apm_cli/commands/compile/cli.py"
_PACK = "src/apm_cli/commands/pack.py"

# -- U1: the modifiers each command accepts -----------------------------------

ACCEPTS: Mapping[str, frozenset[str]] = {
    "install": frozenset(MODIFIERS),
    "update": frozenset({"DRYRUN", "FORCE", "SELECT"}),
    "uninstall": frozenset({"DRYRUN", "SELECT"}),
    "prune": frozenset({"DRYRUN"}),
    "compile": frozenset({"DRYRUN", "SELECT"}),
    "lock": frozenset({"SELECT", "UPDATE", "NOPOLICY"}),
    "pack": frozenset({"DRYRUN", "FORCE", "SELECT"}),
    "bundle-install": frozenset({"DRYRUN", "FORCE", "SELECT", "UPDATE", "NOPOLICY"}),
    # install --mcp accepts --target: mcp-install x SELECT is a success cell
    # (tests/integration/test_copilot_root_mcp_json_lifecycle.py).
    "mcp-install": frozenset({"DRYRUN", "FORCE", "SELECT", "NOPOLICY"}),
    "approve-deny": frozenset({"SELECT"}),
    "audit": frozenset({"DRYRUN", "SELECT", "NOPOLICY"}),
    "marketplace": frozenset({"SELECT"}),
}
ACCEPTS_EVIDENCE: Mapping[str, tuple[Ev, ...]] = {
    "install": (
        Ev(_INSTALL, 844, '"--only"'),
        Ev(_INSTALL, 849, '"--update"'),
        Ev(_INSTALL, 853, '"--dry-run"'),
        Ev(_INSTALL, 855, '"--force"'),
        Ev(_INSTALL, 860, '"--frozen"'),
        Ev(_INSTALL, 885, '"--target"'),
        Ev(_INSTALL, 1017, '"--no-policy"'),
    ),
    "update": (
        Ev("src/apm_cli/commands/update.py", 477, 'argument("packages"'),
        Ev("src/apm_cli/commands/update.py", 487, '"--dry-run"'),
        Ev("src/apm_cli/commands/update.py", 508, '"--force"'),
        Ev("src/apm_cli/commands/update.py", 535, '"--target"'),
    ),
    "uninstall": (
        Ev("src/apm_cli/commands/uninstall/cli.py", 583, 'argument("packages"'),
        Ev("src/apm_cli/commands/uninstall/cli.py", 585, '"--dry-run"'),
    ),
    "prune": (
        Ev("src/apm_cli/commands/prune.py", 71, '"--dry-run"'),
        Ev("src/apm_cli/commands/prune.py", 77, "def prune(ctx, dry_run)"),
    ),
    "compile": (Ev(_COMPILE, 1269, '"--target"'), Ev(_COMPILE, 1279, '"--dry-run"')),
    "lock": (
        Ev("src/apm_cli/commands/lock.py", 108, '"--update"'),
        Ev("src/apm_cli/commands/lock.py", 115, '"--no-policy"'),
        Ev("src/apm_cli/commands/lock.py", 121, '"--target"'),
    ),
    "pack": (
        Ev(_PACK, 183, '"--target"'),
        Ev(_PACK, 219, '"--dry-run"'),
        Ev(_PACK, 222, '"--force"'),
    ),
    "bundle-install": (Ev(_INSTALL, 1233, "detect_local_bundle"),),
    "mcp-install": (Ev(_INSTALL, 953, '"--mcp"'), Ev(_INSTALL, 885, '"--target"')),
    "approve-deny": (
        Ev(_APPROVE, 124, 'argument("packages"'),
        Ev(_APPROVE, 207, 'argument("packages"'),
    ),
    "audit": (
        Ev("src/apm_cli/commands/audit.py", 1369, 'argument("package"'),
        Ev("src/apm_cli/commands/audit.py", 1388, '"--dry-run"'),
        Ev("src/apm_cli/commands/audit.py", 1426, '"--no-policy"'),
    ),
    "marketplace": (
        Ev("src/apm_cli/commands/marketplace/__init__.py", 351, "def add(source, name"),
    ),
}
# Refusals that exit 1 (a semantic refusal, ClickException) instead of a
# Click usage error (exit 2).
REFUSAL_EXIT: Mapping[tuple[str, ...], int] = {
    ("mcp-install", "FROZEN"): 1,
    ("bundle-install", "FROZEN"): 1,
}
REFUSAL_EVIDENCE: Mapping[tuple[str, ...], Ev] = {
    ("mcp-install", "FROZEN"): Ev(_INSTALL, 1186, 'reject_frozen_mutation(frozen, "--mcp")'),
    ("bundle-install", "FROZEN"): Ev(_INSTALL, 1188, '"positional packages"'),
    ("mcp-install", "UPDATE"): Ev(
        "src/apm_cli/install/mcp/conflicts.py", 89, "use 'apm update' instead"
    ),
    # Verified on the real CLI: exit 2 at project and user scope, lock unchanged.
    ("install", "FROZEN", "UPDATE"): Ev(_INSTALL, 1190, "--frozen and --update are mutually"),
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
    "install": Ev(_INSTALL, 917, '"--global"'),
    "update": Ev("src/apm_cli/commands/update.py", 500, '"--global"'),
    "uninstall": Ev("src/apm_cli/commands/uninstall/cli.py", 591, '"--global"'),
    "compile": Ev(_COMPILE, 1378, '"--global"'),
    "lock": Ev("src/apm_cli/commands/lock.py", 100, '"--global"'),
    "approve-deny": Ev(_APPROVE, 139, '"--user"'),
    # U5: pack has no --global (options 163-301).
    "pack": Ev(_PACK, 162, '@click.command(name="pack"'),
    "prune": Ev("src/apm_cli/commands/prune.py", 77, "def prune(ctx, dry_run)"),
    "audit": Ev("src/apm_cli/commands/audit.py", 1490, "def audit("),
    "marketplace": Ev("src/apm_cli/commands/marketplace/__init__.py", 351, "def add(source, name"),
}

# -- Rule evidence -------------------------------------------------------------

RULES: Mapping[str, tuple[str, tuple[Ev, ...]]] = {
    "U1": ("a modifier the command does not accept is one refusal cell per command", ()),
    "U2": (
        "the executable gate governs only executable primitives; a plain file next to a "
        "revoked executable must survive; an org policy dependency deny removes it",
        (
            Ev(_APPROVE, 154, "hooks, MCP, LSP, bin, canvas"),
            Ev("src/apm_cli/policy/install_preflight.py", 220, "Install blocked by org policy"),
        ),
    ),
    "U3": (
        "edited is defined relative to a receipt; present with unknown provenance has no lock",
        (Ev("src/apm_cli/core/deployment_ledger.py", 596, "class DeploymentLedgerCodec"),),
    ),
    "U4": (
        "a declared target change without a receipt is reachable; only receipt-backed "
        "contraction needs a prior lock",
        (
            Ev(
                "src/apm_cli/install/manifest_reconcile.py",
                756,
                "canonical target-contraction owner",
            ),
        ),
    ),
    "U5": ("pack -g is a syntactic refusal", (Ev(_PACK, 162, '@click.command(name="pack"'),)),
    "U6": (
        "pack provenance is the dependencies / marketplace block, not SRC",
        (Ev(_PACK, 32, "dependencies: block"), Ev(_PACK, 33, "marketplace: block")),
    ),
    "U7": (
        "mcp-install acts on the requested server; other primitives keep no-collateral-write",
        (Ev(_INSTALL, 953, '"--mcp"'),),
    ),
    "U8": (
        "the source decides the command: bundle-install is install of a local bundle (install "
        "x local-bundle maps to it; no other source can state it); install --mcp takes an MCP "
        "registry name or an inline server (--url, or a command after --), never a package "
        "source, and an MCP source delivers only MCP; a package registry delivers whole "
        "packages. Parsed-then-refused shapes are refusal cells, one per message class: "
        "marketplace add of a source without a catalog, uninstall of an MCP server "
        "(a registry name is not found in apm.yml; an inline name is an MCP server), and a "
        "local bundle, which writes no manifest entry (uninstall refuses it; update and "
        "prune leave it untouched). A native plugin carries no canvas primitive",
        (
            Ev(_INSTALL, 1233, "detect_local_bundle"),
            Ev(_INSTALL, 997, '"--registry"'),
            Ev(_INSTALL, 971, '"--url"'),
            Ev("src/apm_cli/models/dependency/reference.py", 860, '"registry" in entry'),
            Ev("src/apm_cli/marketplace/registration.py", 45, "No marketplace.json found"),
            Ev("src/apm_cli/commands/uninstall/engine.py", 698, "is an MCP server, not a package"),
            Ev("src/apm_cli/commands/uninstall/engine.py", 857, "was not found in apm.yml"),
            Ev("src/apm_cli/commands/uninstall/engine.py", 690, "cannot remove local-bundle"),
            Ev("src/apm_cli/commands/update.py", 448, "No APM dependencies to update"),
            Ev("src/apm_cli/commands/prune.py", 199, "Nothing to prune"),
            Ev("src/apm_cli/integration/canvas_integrator.py", 5, ".apm/extensions/"),
        ),
    ),
    "U-BIN": (
        "bin/ deploys only for a marketplace plugin installed at user scope with a Claude "
        "target that supports skills (plugin_bin_deployable), so bin/ at project scope is "
        "unreachable and removed. The package type, not SRC, makes a marketplace plugin, so "
        "SRC cells stay. Self-reinstating: the generator hashes the predicate "
        "(generator/bin_pin.py); when the hash differs from U_BIN_PIN the cells return as "
        "must-cover and --check fails until a reviewed change updates the pin. The "
        "closed-world pins of the bin/ writer and unpack live with the gate tables (T3)",
        (
            Ev("src/apm_cli/install/exec_gate.py", 497, "PackageType.MARKETPLACE_PLUGIN"),
            Ev("src/apm_cli/install/exec_gate.py", 498, "InstallScope.USER"),
            Ev("src/apm_cli/install/exec_gate.py", 501, 'target.name == "claude"'),
        ),
    ),
    "U9": (
        "approve/deny with no prior decision is reachable; --list and --pending are read-only",
        (Ev(_APPROVE, 174, "if list_decisions:"), Ev(_APPROVE, 178, "if pending:")),
    ),
    "U10": (
        "compile renders hook/mcp/lsp only; --validate reads MCP declarations",
        (Ev(_COMPILE, 684, "def _run_validation_mode"), Ev(_COMPILE, 716, "get_mcp_dependencies")),
    ),
    "E1": (
        "hosts are equivalent except lock identity (HOSTID)",
        (Ev("src/apm_cli/models/dependency/identity.py", 246, "def build_dependency_unique_key"),),
    ),
    "E2": (
        "targets are equivalent except MCP ownership (MCPOWN)",
        (
            Ev(
                "src/apm_cli/install/mcp/retired_destinations.py",
                84,
                "_FIRST_ROOT_MCP_JSON_RELEASE",
            ),
            Ev(
                "src/apm_cli/install/mcp/retired_destinations.py",
                163,
                "def heal_retired_project_mcp_destinations",
            ),
        ),
    ),
    "E3": (
        "file primitives are equivalent except compile-staging routing (FILEROUTE)",
        (
            Ev("src/apm_cli/install/services.py", 850, "staged_files"),
            Ev("src/apm_cli/install/services.py", 1050, '"instructions" not in target.primitives'),
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
MARKETPLACE_SOURCES = frozenset({"marketplace-git", "marketplace-local", "marketplace-file"})
# Sources of a single MCP server (install --mcp), not of a package.
MCP_SOURCES = frozenset({"mcp-registry", "inline-mcp"})
# U8 refusal cells: the CLI parses these shapes, then refuses them (exit 1).
MARKETPLACE_ADD_REFUSAL = "rule|U8|marketplace-add-refuses-a-source-without-catalog"
UNINSTALL_MCP_REFUSALS: Mapping[str, str] = {
    "mcp-registry": "rule|U8|uninstall-refuses-a-registry-mcp-name",
    "inline-mcp": "rule|U8|uninstall-refuses-an-inline-mcp-name",
}
# A local bundle writes no manifest entry: uninstall refuses it, update and
# prune parse and leave it untouched (one cell per message class).
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
    LOCAL_BUNDLE_REFUSALS["uninstall"]: (1, r"is a local bundle\. Bundles installed with"),
    LOCAL_BUNDLE_REFUSALS["update"]: (0, r"No APM dependencies"),
    LOCAL_BUNDLE_REFUSALS["prune"]: (0, r"Nothing to prune"),
}
# Refusal rule cells whose current message is a defect: (tracking issue, a regex
# the fixed output matches). The cell is known-failing until that output appears.
RULE_CELL_KNOWN: Mapping[str, tuple[str, str]] = {
    # The message says there is nothing to update while a local bundle is installed.
    LOCAL_BUNDLE_REFUSALS["update"]: ("#4318", r"p-exec"),
}

# Rule cells: must-cover singletons a rule names on its own.
RULE_CELLS: Mapping[str, str] = {
    "rule|U9|approve-list-read-only": "U9",
    "rule|U9|approve-pending-read-only": "U9",
    "rule|U10|compile-validate-reads-mcp": "U10",
    "rule|U10|compile-render-only-non-mutation": "U10",
    **dict.fromkeys(RULE_CELL_REFUSALS, "U8"),
}
# Rule cells whose evidence is a state fact (nothing written).
STATE_FACT_RULE_CELLS = frozenset(
    {
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
    if u_bin_applies(cell) and u_bin_pinned():
        return "U-BIN"
    if (deployed in ("clean", "edited") and lock == "absent") or (
        deployed == "present-provenance-unknown" and lock not in (None, "absent")
    ):
        return "U3"
    return None


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
    source_target = _u8_target(cell)
    if source_target is not None:
        return source_target, "U8"
    policy_level = GATE_AUTHORITY.get(gate) == "org-policy"
    if op == "compile" and prim in ("hook", "lsp", "mcp") and policy_level:
        return None
    if op == "compile" and prim in ("hook", "lsp"):
        return "rule|U10|compile-render-only-non-mutation", "U10"
    if op == "compile" and prim == "mcp":
        return "rule|U10|compile-validate-reads-mcp", "U10"
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
    return tuple(rules)


def classify(family: str, cell: Mapping[str, str]) -> Verdict:
    """Apply definition, refusal, mapping and must-cover rules in that order."""
    rule = definition_rule(cell)
    if rule:
        return Verdict("removed", (rule,))
    refusal = refusal_cell(cell)
    if refusal:
        rule = "U5" if cell.get("OP") == "pack" and cell.get("SCOPE") == "user" else "U1"
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


# -- U-BIN: bin/ deploys only at user scope (steward ruling) -------------------

# The approved digest of plugin_bin_deployable (generator/bin_pin.py). While the
# live digest equals it, U-BIN removes the bin cells of project scope; when it
# differs they are must-cover again and --check fails until a reviewed change.
U_BIN_PIN = "a2fb54bea5abdfe3d5aababbe1a10461fc5a89ec357c94f0dbca9e21b57e6851"


def u_bin_applies(cell: Mapping[str, str]) -> bool:
    """bin/ at project scope: the shape U-BIN defines away (exec_gate.py:498)."""
    return cell.get("PRIM") == "bin" and cell.get("SCOPE") == "project"


def u_bin_pinned() -> bool:
    from tests.lifecycle.generator.bin_pin import predicate_digest

    return predicate_digest() == U_BIN_PIN
