"""Pure expectations derived from ``ModelState`` (#4093).

``expected_deployed`` is the reference model of the family::

    {(pkg, prim, target) | pkg declared, policy allows pkg, target effective,
     and (prim is ungated or (gate[pkg, prim] == allowed and org does not deny prim))}

``expected_exit`` narrows each command's documented exit contract
(``EXIT_CONTRACTS``) with what the model knows; when the model cannot predict
the outcome it returns the whole contract.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping

from tests.lifecycle.model.state import UNGATED_KINDS, UNPREDICTED_POLICIES, ModelState

DEFAULT_EXIT_CONTRACT = frozenset({0, 1, 2})
EXIT_CONTRACTS: Mapping[str, frozenset[int]] = {
    # commands/audit.py: 0 clean, 1 critical, 2 warnings only, 3 external scanner error.
    "audit": frozenset({0, 1, 2, 3}),
    # commands/pack.py: 3 version alignment, 4 working-tree drift, 5 strict metadata.
    "pack": frozenset({0, 1, 2, 3, 4, 5}),
}
# Commands that resolve and deploy the declared set.
_RESOLVING = frozenset({"install", "update"})
# Commands whose success the model predicts when nothing else blocks them.
_PREDICTED = frozenset({"install", "update", "uninstall", "prune", "approve", "deny"})


def exit_contract(command: str) -> frozenset[int]:
    return EXIT_CONTRACTS.get(command, DEFAULT_EXIT_CONTRACT)


def expected_deployed(
    model: ModelState, kinds: Mapping[str, Collection[str]]
) -> frozenset[tuple[str, str, str]]:
    """Every (package, primitive kind, target) the model allows to be deployed."""
    return frozenset(
        (package, kind, target)
        for package in model.declared
        if model.policy_allows(package)
        for kind in kinds[package]
        if kind in UNGATED_KINDS
        or (model.gate(package, kind) == "allowed" and not model.org_denies(kind))
        for target in model.targets
    )


def expected_lock_rows(
    model: ModelState, requires: Mapping[str, Collection[str]] | None = None
) -> frozenset[str]:
    """Packages a successful install records: the declared set and its closure."""
    requires = requires or {}
    rows: set[str] = set()
    pending = list(model.declared)
    while pending:
        package = pending.pop()
        if package not in rows:
            rows.add(package)
            pending.extend(requires.get(package, ()))
    return frozenset(rows)


def expected_exit(
    command: str, model: ModelState, *, frozen: bool = False, usage_error: bool = False
) -> frozenset[int]:
    """Exit codes *command* may return on *model*, within its documented contract."""
    contract = exit_contract(command)
    if usage_error:
        return frozenset({2})
    if command not in _PREDICTED or model.policy in UNPREDICTED_POLICIES:
        return contract
    if command in _RESOLVING:
        if any(not model.policy_allows(package) for package in model.declared):
            return frozenset({1})
        # A lock written by a newer generator refuses every resolving command (M-SCOPE-LOCK).
        if model.lock == "newer-generator" or (frozen and model.lock in {"absent", "stale"}):
            return frozenset({1})
    return frozenset({0})
