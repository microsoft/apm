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
        Ev("src/apm_cli/commands/update.py", 460, 'argument("packages"'),
        Ev("src/apm_cli/commands/update.py", 470, '"--dry-run"'),
        Ev("src/apm_cli/commands/update.py", 491, '"--force"'),
        Ev("src/apm_cli/commands/update.py", 518, '"--target"'),
    ),
    "uninstall": (
        Ev("src/apm_cli/commands/uninstall/cli.py", 544, 'argument("packages"'),
        Ev("src/apm_cli/commands/uninstall/cli.py", 546, '"--dry-run"'),
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
    "pack": (Ev(_PACK, 183, '"--target"'), Ev(_PACK, 219, '"--dry-run"'), Ev(_PACK, 222, '"--force"')),
    "bundle-install": (Ev(_INSTALL, 1233, "detect_local_bundle"),),
    "mcp-install": (Ev(_INSTALL, 953, '"--mcp"'), Ev(_INSTALL, 885, '"--target"')),
    "approve-deny": (
        Ev(_APPROVE, 134, 'argument("packages"'),
        Ev(_APPROVE, 214, 'argument("packages"'),
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
    {"install", "update", "uninstall", "compile", "lock", "bundle-install", "mcp-install", "approve-deny"}
)
USER_SCOPE_EVIDENCE: Mapping[str, Ev] = {
    "install": Ev(_INSTALL, 917, '"--global"'),
    "update": Ev("src/apm_cli/commands/update.py", 483, '"--global"'),
    "uninstall": Ev("src/apm_cli/commands/uninstall/cli.py", 552, '"--global"'),
    "compile": Ev(_COMPILE, 1378, '"--global"'),
    "lock": Ev("src/apm_cli/commands/lock.py", 100, '"--global"'),
    "approve-deny": Ev(_APPROVE, 149, '"--user"'),
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
            Ev(_APPROVE, 164, "hooks, MCP, LSP, bin, canvas"),
            Ev("src/apm_cli/policy/install_preflight.py", 220, "Install blocked by org policy"),
        ),
    ),
    "U3": (
        "edited is defined relative to a receipt; present with unknown provenance has no lock",
        (Ev("src/apm_cli/core/deployment_ledger.py", 588, "class DeploymentLedgerCodec"),),
    ),
    "U4": (
        "a declared target change without a receipt is reachable; only receipt-backed "
        "contraction needs a prior lock",
        (Ev("src/apm_cli/install/manifest_reconcile.py", 756, "canonical target-contraction owner"),),
    ),
    "U5": ("pack -g is a syntactic refusal", (Ev(_PACK, 162, '@click.command(name="pack"'),)),
    "U6": (
        "pack provenance is the dependencies / marketplace block, not SRC",
        (Ev(_PACK, 33, "dependencies: block"), Ev(_PACK, 34, "marketplace: block")),
    ),
    "U7": (
        "mcp-install acts on the requested server; other primitives keep no-collateral-write",
        (Ev(_INSTALL, 953, '"--mcp"'),),
    ),
    "U8": (
        "bundle-install comes only from a local bundle; marketplace add takes git, a local "
        "path or file://; mcp-install resolves from the MCP registry",
        (
            Ev(_INSTALL, 1233, "detect_local_bundle"),
            Ev("src/apm_cli/commands/marketplace/__init__.py", 359, "a local filesystem path"),
            Ev("src/apm_cli/commands/marketplace/__init__.py", 360, "file://"),
            Ev(_INSTALL, 997, '"--registry"'),
        ),
    ),
    "U9": (
        "approve/deny with no prior decision is reachable; --list and --pending are read-only",
        (Ev(_APPROVE, 184, "if list_decisions:"), Ev(_APPROVE, 188, "if pending:")),
    ),
    "U10": (
        "compile renders hook/mcp/lsp only; --validate reads MCP declarations",
        (Ev(_COMPILE, 685, "def _run_validation_mode"), Ev(_COMPILE, 716, "get_mcp_dependencies")),
    ),
    "E1": (
        "hosts are equivalent except lock identity (HOSTID)",
        (Ev("src/apm_cli/models/dependency/identity.py", 246, "def build_dependency_unique_key"),),
    ),
    "E2": (
        "targets are equivalent except MCP ownership (MCPOWN)",
        (
            Ev("src/apm_cli/install/mcp/retired_destinations.py", 84, "_FIRST_ROOT_MCP_JSON_RELEASE"),
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
            Ev("src/apm_cli/install/services.py", 842, "staged_files"),
            Ev("src/apm_cli/install/services.py", 1055, '"instructions" not in target.primitives'),
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

# Rule cells: must-cover singletons a rule names on its own.
RULE_CELLS: Mapping[str, str] = {
    "rule|U9|approve-list-read-only": "U9",
    "rule|U9|approve-pending-read-only": "U9",
    "rule|U10|compile-validate-reads-mcp": "U10",
    "rule|U10|compile-render-only-non-mutation": "U10",
}
# Rule cells whose evidence is a state fact (nothing written).
STATE_FACT_RULE_CELLS = frozenset(
    {
        "rule|U9|approve-list-read-only",
        "rule|U9|approve-pending-read-only",
        "rule|U10|compile-render-only-non-mutation",
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
        or (op == "install" and src == "local-bundle")
        or (op == "marketplace" and src not in MARKETPLACE_SOURCES)
        or (op == "mcp-install" and src != "registry")
        or (src == "registry" and prim is not None and prim != "mcp")
    ):
        return "U8"
    if (deployed in ("clean", "edited") and lock == "absent") or (
        deployed == "present-provenance-unknown" and lock not in (None, "absent")
    ):
        return "U3"
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
