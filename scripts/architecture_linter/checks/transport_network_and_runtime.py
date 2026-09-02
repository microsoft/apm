"""Network, ref-resolution, and runtime-safety transport analyzers.

Ports the remaining four of the ten canonical owner decisions in
``.apm/architecture/owners/transport-auth-platform.json``, plus the two
non-owner transport-domain guards:

* ``transport-platform-github-throttle`` -- ``deps/github_rate_limit.py`` owns
  GitHub API throttle classification (legacy AC17).
* ``transport-platform-ref-freshness`` -- ``deps/tiered_ref_resolver.py`` owns
  Git ref freshness and cache eligibility (legacy RefFreshnessPolicy guard).
* ``transport-platform-git-semver-preflight`` -- ``install/helpers/ref_reuse.py``
  owns Git semver preflight eligibility and transport selection (legacy AC13).
* ``transport-platform-network-host-parsing`` -- ``utils/net.py`` owns network
  host literal parsing and loopback classification (legacy network-host
  guard).

Two additional, non-owner rules port transport-domain guards whose owners are
not among the ten registry owners above: the process-wide TLS trust-injection
scope guard (legacy AC5 TLS, owner ``core/tls_trust.py``) and the runtime
wall-clock deadline guard (legacy AC7, owner ``runtime/base.py``).

Every check reads source exclusively through the shared
:class:`~scripts.architecture_linter.facts.FactsProvider`; nothing here opens
files, walks the filesystem, re-parses source, or shells out.
"""

from __future__ import annotations

import re

from scripts.architecture_linter.checks.transport_platform_shared import (
    _TIERED,
    GROUP,
    _count_checks,
    _forbid_scan,
    _paths_under,
    _require_res,
    _require_subs,
    _src_python,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.models import Rule, Violation

_RID_THROTTLE = "transport-platform-github-throttle"


_THROTTLE_OWNER = "src/apm_cli/deps/github_rate_limit.py"


_THROTTLE_SIGNALS = re.compile(r"X-RateLimit-Remaining|Retry-After")


def _check_github_throttle(provider: FactsProvider) -> tuple[Violation, ...]:
    inv = frozenset(provider.inventory)
    findings: list[Violation] = []

    findings.extend(
        _require_res(
            provider,
            inv,
            _RID_THROTTLE,
            _THROTTLE_OWNER,
            (
                re.compile(r"^def classify_github_throttle\("),
                re.compile(r"^class GitHubThrottleError"),
            ),
            "github_rate_limit must own classify_github_throttle and GitHubThrottleError",
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            _RID_THROTTLE,
            _src_python(provider, exclude={_THROTTLE_OWNER}),
            _THROTTLE_SIGNALS,
            "GitHub throttle signals must be classified only by deps/github_rate_limit.py",
            exempt=True,
        )
    )
    return tuple(findings)


_RID_FRESHNESS = "transport-platform-ref-freshness"


_FRESHNESS_DUP = re.compile(
    r"ctx\.update_refs\s+or\s+ctx\.refresh"
    r"|def [A-Za-z0-9_]*ref_freshness"
    r"|class [A-Za-z0-9_]*RefFreshness"
)


_L2_BARE_REV_PARSE = re.compile(r"L2BareRevParse")


def _check_ref_freshness(provider: FactsProvider) -> tuple[Violation, ...]:
    inv = frozenset(provider.inventory)
    findings: list[Violation] = []

    findings.extend(
        _require_res(
            provider,
            inv,
            _RID_FRESHNESS,
            _TIERED,
            (
                re.compile(r"^class RefFreshnessPolicy\(Enum\):"),
                re.compile(r"^def ref_freshness_policy_for_install\("),
                re.compile(r"^    if freshness_policy\.allows_bare_cache:"),
            ),
            "tiered_ref_resolver must own RefFreshnessPolicy and its install policy",
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_FRESHNESS,
            "src/apm_cli/install/phases/resolve.py",
            ("ref_freshness_policy_for_install(ctx)",),
            "resolve phase must consult RefFreshnessPolicy",
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_FRESHNESS,
            "src/apm_cli/install/helpers/ref_seed.py",
            ("ref_freshness_policy_for_install(ctx)",),
            "ref seeding must consult RefFreshnessPolicy",
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_FRESHNESS,
            "src/apm_cli/commands/outdated.py",
            ("freshness_policy=RefFreshnessPolicy.CURRENT_REMOTE",),
            "outdated command must request current-remote freshness explicitly",
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            _RID_FRESHNESS,
            _src_python(provider, exclude={_TIERED}),
            _L2_BARE_REV_PARSE,
            "L2 bare rev-parse freshness must stay owned by tiered_ref_resolver.py",
            exempt=False,
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            _RID_FRESHNESS,
            _src_python(provider, exclude={_TIERED}),
            _FRESHNESS_DUP,
            "Git ref freshness must route through RefFreshnessPolicy",
            exempt=True,
        )
    )
    return tuple(findings)


_RID_SEMVER = "transport-platform-git-semver-preflight"


_REF_REUSE = "src/apm_cli/install/helpers/ref_reuse.py"


_REF_RESOLVER = "src/apm_cli/marketplace/ref_resolver.py"


_TRANSPORT_SELECTION = re.compile(r"from .*transport_selection import|TransportSelector\(")


_SEMVER_REF_KIND = re.compile(r"""dep_ref\.ref_kind\s*==\s*["']semver["']""")


def _check_git_semver_preflight(provider: FactsProvider) -> tuple[Violation, ...]:
    inv = frozenset(provider.inventory)
    findings: list[Violation] = []

    # Transport selection routes through TransportSelector into RefResolver.
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_SEMVER,
            _REF_REUSE,
            (
                "transport_plan = transport_selector.select(",
                'transport_scheme = "ssh" if selected_scheme == "ssh" else "https"',
                "transport_scheme=transport_scheme",
            ),
            "ref_reuse must select transport through TransportSelector",
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_SEMVER,
            _REF_RESOLVER,
            ("build_ssh_url(",),
            "RefResolver must build SSH URLs through the transport seam",
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            _RID_SEMVER,
            (_REF_RESOLVER,),
            _TRANSPORT_SELECTION,
            "RefResolver must not re-import TransportSelector; ref_reuse owns selection",
            exempt=False,
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_SEMVER,
            "src/apm_cli/deps/git_reference_resolver.py",
            ("transport_plan = host._transport_selector.select(",),
            "git reference resolver must select transport through the host seam",
        )
    )

    # Git semver preflight eligibility is owned by ref_reuse.py.
    findings.extend(
        _count_checks(
            provider,
            inv,
            _RID_SEMVER,
            _REF_REUSE,
            (("re", r"^def is_git_semver_resolution_eligible\(", 1, "eq"),),
            "ref_reuse must own is_git_semver_resolution_eligible",
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_SEMVER,
            _REF_REUSE,
            ("if not is_git_semver_resolution_eligible(dep_ref):",),
            "ref_reuse must gate semver resolution on the eligibility owner",
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_SEMVER,
            "src/apm_cli/commands/install.py",
            ("is_git_semver_resolution_eligible(dep_ref)",),
            "install command must consult the semver eligibility owner",
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            _RID_SEMVER,
            ("src/apm_cli/commands/install.py",),
            _SEMVER_REF_KIND,
            "Semver preflight must not re-derive eligibility from dep_ref.ref_kind",
            exempt=False,
        )
    )
    return tuple(findings)


_RID_NET = "transport-platform-network-host-parsing"


_NET_OWNER = "src/apm_cli/utils/net.py"


_NET_DUP = re.compile(r"^def (_host_to_ip_literal|parse_host_address|is_loopback_host)\(")


def _check_network_host_parsing(provider: FactsProvider) -> tuple[Violation, ...]:
    inv = frozenset(provider.inventory)
    findings: list[Violation] = []

    findings.extend(
        _count_checks(
            provider,
            inv,
            _RID_NET,
            _NET_OWNER,
            (("re", r"^def (parse_host_address|is_loopback_host)\(", 2, "eq"),),
            "utils/net.py must own parse_host_address and is_loopback_host",
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            _RID_NET,
            _src_python(provider, exclude={_NET_OWNER}),
            _NET_DUP,
            "Network host parsing helpers must stay owned by utils/net.py",
            exempt=True,
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_NET,
            "src/apm_cli/core/script_executors.py",
            ("from ..utils.net import parse_host_address", "literal = parse_host_address(host)"),
            "script executors must parse host literals through utils/net.py",
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_NET,
            "src/apm_cli/install/mcp/warnings.py",
            ("from ...utils.net import parse_host_address", "ip = parse_host_address(bare)"),
            "MCP warnings must parse host literals through utils/net.py",
        )
    )
    return tuple(findings)


_RID_ARTIFACTORY_SHA = "transport-platform-artifactory-full-commit-sha"


_ARTIFACTORY_SHA_OWNER = "src/apm_cli/utils/github_host.py"
_ARTIFACTORY_SHA_CONSUMER = "src/apm_cli/deps/artifactory_orchestrator.py"
_ARTIFACTORY_SHA_DUP = re.compile(r"\{40\}|fullmatch\(")


def _check_artifactory_full_commit_sha(provider: FactsProvider) -> tuple[Violation, ...]:
    inv = frozenset(provider.inventory)
    findings: list[Violation] = []

    findings.extend(
        _count_checks(
            provider,
            inv,
            _RID_ARTIFACTORY_SHA,
            _ARTIFACTORY_SHA_OWNER,
            (("re", r"^def is_full_commit_sha\(", 1, "eq"),),
            "utils/github_host.py must own full commit SHA classification",
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_ARTIFACTORY_SHA,
            _ARTIFACTORY_SHA_OWNER,
            ("if is_full_commit_sha(ref):",),
            "Artifactory archive URL selection must use the full commit SHA owner",
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_ARTIFACTORY_SHA,
            _ARTIFACTORY_SHA_CONSUMER,
            (
                "from ..utils.github_host import default_host, is_full_commit_sha, "
                "is_github_hostname",
                "if is_full_commit_sha(ref):",
            ),
            "Artifactory metadata must use the full commit SHA owner",
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            _RID_ARTIFACTORY_SHA,
            (_ARTIFACTORY_SHA_CONSUMER,),
            _ARTIFACTORY_SHA_DUP,
            "Artifactory full commit SHA classification must route through utils/github_host.py",
            exempt=False,
        )
    )
    return tuple(findings)


_RID_TLS = "transport-platform-tls-trust-injection"


_TLS_INJECT = re.compile(r"truststore\.inject_into_ssl\(")


_TLS_ALLOWED: frozenset[str] = frozenset(
    {
        "src/apm_cli/core/tls_trust.py",
        "src/apm_cli/core/_child_tls/_apm_tls_bootstrap.py",
    }
)


def _check_tls_trust_injection(provider: FactsProvider) -> tuple[Violation, ...]:
    inv = frozenset(provider.inventory)
    return _forbid_scan(
        provider,
        inv,
        _RID_TLS,
        _src_python(provider, exclude=_TLS_ALLOWED),
        _TLS_INJECT,
        "TLS trust injection belongs to core/tls_trust.py and the child TLS bootstrap",
        exempt=True,
    )


_RID_RUNTIME_DEADLINE = "transport-platform-runtime-deadline-safety"


_RUNTIME_PREFIX = "src/apm_cli/runtime/"


_RUNTIME_ADAPTERS: tuple[str, ...] = ("_runtime.py",)


_RUNTIME_BASE = "src/apm_cli/runtime/base.py"


_RUNTIME_POPEN = re.compile(r"subprocess\.Popen")


def _check_runtime_deadline_safety(provider: FactsProvider) -> tuple[Violation, ...]:
    inv = frozenset(provider.inventory)
    findings: list[Violation] = []

    # AC7 -- no runtime adapter spawns its own process; base.py streams for all.
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            _RID_RUNTIME_DEADLINE,
            _paths_under(provider, _RUNTIME_PREFIX, _RUNTIME_ADAPTERS),
            _RUNTIME_POPEN,
            "Runtime adapters must reuse the deadline-aware base streamer",
            exempt=True,
        )
    )

    # AC7 -- the base streamer keeps both halves of the deadline contract.
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_RUNTIME_DEADLINE,
            _RUNTIME_BASE,
            ("time.monotonic", "_terminate_and_reap"),
            "Runtime streaming must enforce and reap on a wall-clock deadline",
        )
    )
    return tuple(findings)


RULES: tuple[Rule, ...] = (
    Rule(
        id=_RID_THROTTLE,
        group=GROUP,
        guard_ids=(_RID_THROTTLE,),
        description="GitHub API throttle classification stays owned by deps/github_rate_limit.py.",
        check=_check_github_throttle,
    ),
    Rule(
        id=_RID_FRESHNESS,
        group=GROUP,
        guard_ids=(_RID_FRESHNESS,),
        description="Git ref freshness routes through RefFreshnessPolicy in tiered_ref_resolver.py.",
        check=_check_ref_freshness,
    ),
    Rule(
        id=_RID_SEMVER,
        group=GROUP,
        guard_ids=(_RID_SEMVER,),
        description="Git semver preflight eligibility and transport selection stay in ref_reuse.py.",
        check=_check_git_semver_preflight,
    ),
    Rule(
        id=_RID_NET,
        group=GROUP,
        guard_ids=(_RID_NET,),
        description="Network host parsing and loopback classification stay owned by utils/net.py.",
        check=_check_network_host_parsing,
    ),
    Rule(
        id=_RID_ARTIFACTORY_SHA,
        group=GROUP,
        guard_ids=(_RID_ARTIFACTORY_SHA,),
        description="Artifactory full commit SHA classification stays owned by github_host.py.",
        check=_check_artifactory_full_commit_sha,
    ),
    Rule(
        id=_RID_TLS,
        group=GROUP,
        guard_ids=(),
        description="TLS trust injection stays scoped to core/tls_trust.py and the child bootstrap.",
        check=_check_tls_trust_injection,
    ),
    Rule(
        id=_RID_RUNTIME_DEADLINE,
        group=GROUP,
        guard_ids=(),
        description="Runtime adapters stream through the deadline-aware, reaping base streamer.",
        check=_check_runtime_deadline_safety,
    ),
)


COLLECTORS: tuple[object, ...] = ()


__all__ = ["COLLECTORS", "RULES"]
