"""Keep package checkouts behind their commit verifier."""

from __future__ import annotations

from scripts.architecture_linter.checks.transport_platform_shared import (
    GROUP,
    _require_subs,
    _src_python,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import violation
from scripts.architecture_linter.models import Rule, Violation

_RULE_ID = "transport-platform-checkout-verified"
_OWNER = "src/apm_cli/cache/checkout_verify.py"
_REQUIRED: dict[str, tuple[str, ...]] = {
    _OWNER: (
        "if _object_id(self._algorithm, kind, data) != oid:",
        'raise CheckoutVerificationError(f"{rel} is not produced by the commit")',
        'raise CheckoutVerificationError(f"{missing[0]} is missing from the checkout")',
        "any(state.get(name) not in (None, False) for name in _TRANSFORMS)",
        "_check_mode(rel, info, want[1])",
        "env=cached_repo_env(dict(os.environ if env is None else env), offline=True),",
    ),
    "src/apm_cli/cache/git_cache.py": (
        "if verify_checkout(checkout, sha, paths=sparse_paths, cone=cone, env=verify_env):",
        "if checkout_dir.is_dir() and healed():",
    ),
    "src/apm_cli/deps/github_downloader.py": (
        "target_path, temp_clone_path, _ws2_resolved_commit, subdir_path, _env",
        "require_verified_copy(target_path, _cached, _cached.parent.name)",
        "require_verified_checkout(target_path, resolved_ref.resolved_commit)",
        "repository_license = checked_license(temp_clone_path, subdir_path, _ws2_resolved_commit)",
    ),
    "src/apm_cli/marketplace/client.py": (
        "return json.loads(read_verified_file(Path(checkout_dir), file_path))",
    ),
}
# Readers that hand out checkout bytes before verification, and the only
# function each may be called from (which verifies what it returns or copies).
_UNVERIFIED_READERS: dict[str, str] = {
    "_get_checkout": "GitCache.get_checkout",
    "_read_only_checkout": "GitCache._get_checkout",
    "_create_checkout": "GitCache._get_checkout",
    "materialize_from_bare": "GitHubPackageDownloader._materialize_from_bare",
    "_materialize_from_bare": "GitHubPackageDownloader.download_subdirectory_package",
}


def _unverified_reads(provider: FactsProvider) -> list[Violation]:
    findings: list[Violation] = []
    for path in _src_python(provider):
        for call in provider.file_facts(path).calls:
            name = call.qualname.rsplit(".", 1)[-1]
            allowed = _UNVERIFIED_READERS.get(name)
            if allowed is not None and call.scope != allowed.rsplit(".", 1)[-1]:
                findings.append(
                    violation(
                        _RULE_ID,
                        path,
                        f"{name} hands out unverified checkout bytes; call it only from {allowed}",
                        line=call.line,
                    )
                )
    return findings


def _check_checkout_verified(provider: FactsProvider) -> tuple[Violation, ...]:
    """Every checkout a package is read from is verified against its commit."""
    inventory = frozenset(provider.inventory)
    findings: list[Violation] = _unverified_reads(provider)
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
