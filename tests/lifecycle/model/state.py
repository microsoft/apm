"""Pure reference state of the lifecycle model (#4093).

``ModelState`` is what the user has asked for, never what the CLI did:
declared packages, executable approvals, the org policy variant, targets,
scope, the lock generator state, the user-edited set and the cache state.
Every transition returns a new state; nothing here touches the filesystem.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace

UNGATED_KINDS = frozenset({"skill", "other-file"})
GATED_KINDS = frozenset({"hook", "mcp", "lsp", "bin", "canvas", "native"})
KINDS = UNGATED_KINDS | GATED_KINDS
SCOPES = ("project", "user")
LOCK_STATES = ("absent", "consistent", "stale", "newer-generator")
CACHE_STATES = ("cold", "warm")
POLICY_VARIANTS = (
    "none",
    "allow",
    "deny-pkg",
    "exec-deny-all",
    "mcp-deny",
    "unreachable",
    "hash-mismatch",
    "chain-cycle",
)
# Policy variants whose effect on an install the model does not predict.
UNPREDICTED_POLICIES = frozenset({"unreachable", "hash-mismatch", "chain-cycle"})
DECISIONS = ("allowed", "denied")


@dataclass(frozen=True)
class ModelState:
    declared: frozenset[str] = frozenset()
    # (package, kind) -> "allowed" | "denied"; absent means unapproved.
    decisions: Mapping[tuple[str, str], str] = field(default_factory=dict)
    # (package, kind) pairs whose approval was revoked after a deploy.
    revoked: frozenset[tuple[str, str]] = frozenset()
    policy: str = "none"
    policy_denied: frozenset[str] = frozenset()
    targets: frozenset[str] = frozenset({"copilot"})
    scope: str = "project"
    lock: str = "absent"
    edited: frozenset[str] = frozenset()
    cache: str = "cold"
    converged: bool = False

    def __post_init__(self) -> None:
        assert self.scope in SCOPES, self.scope
        assert self.lock in LOCK_STATES, self.lock
        assert self.cache in CACHE_STATES, self.cache
        assert self.policy in POLICY_VARIANTS, self.policy
        assert set(self.decisions.values()) <= set(DECISIONS), self.decisions

    # -- queries -------------------------------------------------------------

    def gate(self, package: str, kind: str) -> str:
        """The GATE factor of one (package, kind) cell."""
        if kind in UNGATED_KINDS:
            return "none"
        decision = self.decisions.get((package, kind))
        if decision is not None:
            return decision
        if (package, kind) in self.revoked:
            return "revoked-after-deploy"
        return "unapproved-exec"

    def policy_allows(self, package: str) -> bool:
        return not (self.policy == "deny-pkg" and package in self.policy_denied)

    def org_denies(self, kind: str) -> bool:
        if self.policy == "exec-deny-all":
            return kind in GATED_KINDS
        return self.policy == "mcp-deny" and kind == "mcp"

    # -- transitions ---------------------------------------------------------

    def declare(self, *packages: str) -> ModelState:
        return self._moved(declared=self.declared | set(packages))

    def undeclare(self, *packages: str) -> ModelState:
        return self._moved(declared=self.declared - set(packages))

    def approve(self, package: str, kinds: Iterable[str]) -> ModelState:
        return self._decide(package, kinds, "allowed")

    def deny(self, package: str, kinds: Iterable[str]) -> ModelState:
        return self._decide(package, kinds, "denied")

    def revoke(self, package: str, kinds: Iterable[str]) -> ModelState:
        keys = {(package, kind) for kind in kinds if kind in GATED_KINDS}
        decisions = {key: value for key, value in self.decisions.items() if key not in keys}
        return self._moved(decisions=decisions, revoked=self.revoked | keys)

    def with_policy(self, variant: str, denied: Iterable[str] = ()) -> ModelState:
        return self._moved(policy=variant, policy_denied=frozenset(denied))

    def with_targets(self, *targets: str) -> ModelState:
        return self._moved(targets=frozenset(targets))

    def with_lock(self, state: str) -> ModelState:
        return self._moved(lock=state)

    def edit(self, *paths: str) -> ModelState:
        return self._moved(edited=self.edited | set(paths))

    def installed(self) -> ModelState:
        """The state after a successful install: lock consistent, cache warm, converged."""
        return replace(self, lock="consistent", cache="warm", converged=True)

    def _decide(self, package: str, kinds: Iterable[str], decision: str) -> ModelState:
        decisions = dict(self.decisions)
        decisions.update({(package, kind): decision for kind in kinds if kind in GATED_KINDS})
        return self._moved(decisions=decisions)

    def _moved(self, **changes: object) -> ModelState:
        return replace(self, converged=False, **changes)
