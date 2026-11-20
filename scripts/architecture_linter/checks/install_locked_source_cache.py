"""Guard one locked git source for install, --frozen, update and audit --ci (#4356, #4353).

``cache/git_cache.attach_persistent_git_cache`` alone attaches the persistent
git cache to a downloader, so ``apm install`` (with ``--frozen`` and ``apm
update``) and the ``apm audit --ci`` replay recompute ``content_hash`` over the
same locked-source bytes and reach one verdict. Every re-fetch of an existing
``apm_modules`` copy is staged through ``install/transaction.stage_existing_copy``
so a refused install restores the copy byte for byte.
"""

from __future__ import annotations

from scripts.architecture_linter.checks.install_deployment_shared import (
    _facts_for,
    _name_calls_in,
    _summary,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.models import Violation

GUARD = "install-deployment-locked-source-cache"
OWNER = "src/apm_cli/cache/git_cache.py"

# (path, function, callee, purpose): each reader routes through an owner.
_ROUTES = (
    (
        "src/apm_cli/install/phases/resolve.py",
        "_setup_downloader",
        "attach_persistent_git_cache",
        "read locked sources through the shared git cache",
    ),
    (
        "src/apm_cli/install/drift.py",
        "run_replay",
        "attach_persistent_git_cache",
        "replay locked sources through the same git cache as install",
    ),
    (
        "src/apm_cli/commands/outdated.py",
        "outdated",
        "attach_persistent_git_cache",
        "read locked sources through the shared git cache",
    ),
    (
        "src/apm_cli/install/phases/download.py",
        "run",
        "stage_existing_copy",
        "stage an existing copy before a parallel re-fetch",
    ),
    (
        "src/apm_cli/install/phases/integrate.py",
        "_resolve_download_strategy",
        "stage_existing_copy",
        "stage a contradicting copy before it is re-fetched",
    ),
    (
        "src/apm_cli/install/sources.py",
        "acquire",
        "stage_existing_copy",
        "stage an existing copy before a sequential re-fetch",
    ),
)
# Closed world: only the owner wires a persistent cache onto a downloader.
_ATTACH_NEEDLE = "persistent_git_cache ="
_ATTACH_ALLOWED = frozenset({OWNER, "src/apm_cli/deps/github_downloader.py"})


def check_locked_source_cache(provider: FactsProvider) -> tuple[Violation, ...]:
    """Require one git cache owner and staged re-fetches for every locked-source reader."""
    findings: list[Violation] = []
    for path, function, callee, purpose in _ROUTES:
        facts, failures = _facts_for(provider, path, GUARD)
        findings.extend(failures)
        if not failures and callee not in _name_calls_in(facts, function):
            findings.append(_summary(GUARD, path, f"{function} must call {callee} to {purpose}"))
    for path in sorted(provider.inventory):
        if not (path.startswith("src/apm_cli/") and path.endswith(".py")):
            continue
        if path in _ATTACH_ALLOWED:
            continue
        facts, failures = _facts_for(provider, path, GUARD)
        if failures:
            continue
        for number, line in enumerate(getattr(facts, "lines", ()), start=1):
            if line.lstrip().startswith("#") or _ATTACH_NEEDLE not in line:
                continue
            findings.append(
                _summary(
                    GUARD,
                    path,
                    f"line {number}: attach the git cache with "
                    "cache.git_cache.attach_persistent_git_cache",
                )
            )
    return tuple(findings)
