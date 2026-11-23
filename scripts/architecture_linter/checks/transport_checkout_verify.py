"""Keep package checkouts behind their commit verifier."""

from __future__ import annotations

from scripts.architecture_linter.checks.transport_platform_shared import GROUP, _require_subs
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.models import Rule, Violation

_RULE_ID = "transport-platform-checkout-verified"
_OWNER = "src/apm_cli/cache/checkout_verify.py"
_REQUIRED: dict[str, tuple[str, ...]] = {
    _OWNER: (
        "if _object_id(self._algorithm, kind, data) != oid:",
        'raise CheckoutVerificationError(f"{rel} is not produced by the commit")',
        'raise CheckoutVerificationError(f"{missing[0]} is missing from the checkout")',
    ),
    "src/apm_cli/cache/git_cache.py": (
        "if verify_checkout(checkout, sha, paths=sparse_paths, env=git_subprocess_env(env)):",
        "self._evict_checkout(checkout)",
    ),
    "src/apm_cli/deps/github_downloader.py": (
        "require_verified_checkout(temp_clone_path, _ws2_resolved_commit, paths=_scope, env=_env)",
        "require_verified_checkout(target_path, _sha, env=_env)",
    ),
}


def _check_checkout_verified(provider: FactsProvider) -> tuple[Violation, ...]:
    """Every checkout a package is read from is verified against its commit."""
    inventory = frozenset(provider.inventory)
    findings: list[Violation] = []
    for path, literals in _REQUIRED.items():
        findings.extend(
            _require_subs(
                provider,
                inventory,
                _RULE_ID,
                path,
                literals,
                "Package checkouts must be verified against their commit before they are read",
            )
        )
    return tuple(findings)


RULES = (
    Rule(
        id=_RULE_ID,
        group=GROUP,
        guard_ids=(_RULE_ID,),
        description="Cached and cloned package checkouts are verified against their commit.",
        check=_check_checkout_verified,
    ),
)
COLLECTORS: tuple[object, ...] = ()
