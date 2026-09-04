"""Auth, credential, URL-path, and Windows-stable-path transport analyzers.

Ports three of the ten canonical owner decisions in
``.apm/architecture/owners/transport-auth-platform.json``:

* ``transport-platform-host-credential-resolution`` -- ``core/auth.py`` is the
  single owner of Git-subprocess credential and public-GitHub anonymous-first
  environments (legacy AC5 auth scrub, AC19 header-injection ban, AC20
  anonymous-first ownership, AC24 ADO context).
* ``transport-platform-url-path-security`` -- ``utils/path_security.py`` owns
  symlink-component containment and strict percent-encoded URL-path decoding
  (legacy symlink-component guard + AC10a).
* ``transport-platform-windows-stable-path`` -- ``install.ps1`` owns the
  Windows ``current/apm.exe`` stable path (legacy AC8 /
  check_windows_stable_path_owner).

Every check reads source exclusively through the shared
:class:`~scripts.architecture_linter.facts.FactsProvider`; nothing here opens
files, walks the filesystem, re-parses source, or shells out. Sibling module
:mod:`transport_platform_shared` carries the grep/require/forbid helpers all
transport-platform check families share.
"""

from __future__ import annotations

import ast
import re

from scripts.architecture_linter.checks.python_semantics import (
    direct_definitions,
    effective_definition,
)
from scripts.architecture_linter.checks.transport_platform_shared import (
    _SRC_PREFIX,
    GROUP,
    _count_checks,
    _count_sub,
    _forbid_scan,
    _load,
    _paths_under,
    _require_res,
    _require_subs,
    _src_python,
)
from scripts.architecture_linter.checks.tree_index import FUNCTION_NODES
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import EXEMPT_MARKER, checked_facts, violation
from scripts.architecture_linter.models import Rule, Violation

_RID_HOST_CRED = "transport-platform-host-credential-resolution"
_RID_ADO_VALIDATION = "transport-platform-ado-validation-bearer-fallback"
_RID_ADO_CALLER_CONFIG = "transport-platform-ado-validation-caller-config"
_RID_ADO_CLONE_FALLBACK = "transport-platform-ado-validation-clone-bearer-fallback"
_RID_ADO_HELPER_SUPPRESSION = "transport-platform-ado-validation-helper-suppression"
_RID_GIT_CHILD_ENV = "transport-platform-git-child-environment"
_RID_GIT_CLONE_HOOKS = "transport-platform-git-clone-hooks-disabled"
_RID_GIT_CLONE_TEMPLATES = "transport-platform-git-clone-templates-disabled"
_RID_GIT_DIAGNOSTIC = "transport-platform-git-diagnostic-redaction"
_RID_GIT_DIAGNOSTIC_DEBUG = "transport-platform-git-diagnostic-redaction-debug"
_RID_GIT_DIAGNOSTIC_OWNER = "transport-platform-git-diagnostic-sanitizer-ownership"
_RID_GIT_DIAGNOSTIC_DELEGATE = "transport-platform-git-diagnostic-sanitizer-ownership-downloader"
_RID_GIT_DIAGNOSTIC_TOKENS = "transport-platform-git-diagnostic-token-shapes"
_RID_GIT_DIAGNOSTIC_JWT = "transport-platform-git-diagnostic-token-shapes-jwt"
_RID_GIT_SINGLE_REMOTE = "transport-platform-git-single-remote-fetch"
_RID_GIT_URL_CREDENTIALS = "transport-platform-git-url-credentials-out-of-argv"
_RID_GIT_URL_HEADER = "transport-platform-git-url-header-specificity"
_RID_GIT_URL_HEADER_FENCE = "transport-platform-git-url-header-specificity-fence"
_RID_GIT_URL_HEADER_MALFORMED = (
    "transport-platform-git-url-header-specificity-fence-malformed-values"
)
_RID_GIT_URL_HEADER_MANAGED = "transport-platform-git-url-header-specificity-fence-managed-auth"
_RID_GIT_URL_ENFORCEMENT = "transport-platform-git-url-rewrite-enforcement"
_RID_GIT_URL_ONCE = "transport-platform-git-url-rewrite-once"
_RID_GIT_URL_ROUTING = "transport-platform-git-url-rewrite-routing"
_RID_GIT_URL_VALIDATION_ROUTING = "transport-platform-git-url-rewrite-routing-validation"
_RID_GIT_URL_REWRITE = "transport-platform-git-url-rewrite-safety"
_RID_GIT_URL_RECOVERY = "transport-platform-git-url-rewrite-recovery"
_RID_ARTIFACTORY_NETRC = "transport-platform-artifactory-netrc-isolation"


_AUTH_OWNER = "src/apm_cli/core/auth.py"
_ARTIFACTORY_NETRC_OWNER = "src/apm_cli/deps/artifactory_entry.py"
_ARTIFACTORY_NETRC_CONSUMER = "src/apm_cli/deps/download_strategies.py"


_PUBLIC_GH_CONSUMERS: tuple[str, ...] = (
    "src/apm_cli/deps/clone_engine.py",
    "src/apm_cli/deps/download_strategies.py",
    "src/apm_cli/deps/git_reference_resolver.py",
    "src/apm_cli/deps/github_downloader.py",
    "src/apm_cli/deps/github_downloader_validation.py",
)


_ADO_DIRECT_TOKEN = re.compile(r"(\._host|host)\.ado_token")


_ADO_DIRECT_FILES: tuple[str, ...] = (
    "src/apm_cli/deps/download_strategies.py",
    "src/apm_cli/deps/clone_engine.py",
    "src/apm_cli/deps/github_downloader_validation.py",
)


_AUTH_HEADER_DICTMERGE = re.compile(
    r"\.update\(\s*build_(authorization_header_git_env|ado_bearer_git_env)\("
    r"|\{\*\*[A-Za-z_][A-Za-z0-9_.]*,\s*\*\*build_(authorization_header_git_env|ado_bearer_git_env)\("
)


_PUBLIC_GH_METHODS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^    def uses_public_github_anonymous_first\("),
    re.compile(r"^    def build_public_github_anonymous_git_env\("),
    re.compile(r"^    def build_public_github_authenticated_git_env\("),
    re.compile(r"^    def build_native_git_credential_env\("),
    re.compile(r"^    def build_noninteractive_git_env\("),
)


_PUBLIC_GH_DUP = re.compile(r"^\s*def uses_public_github_anonymous_first\(")


_NONINTERACTIVE_BYPASS = re.compile(r"GitAuthEnvBuilder\.noninteractive_env\(")


_PERSISTENT_CACHE_BYPASS = re.compile(r"(_persistent_cache|persistent_git_cache)\.get_checkout\(")


def _check_host_credential_resolution(provider: FactsProvider) -> tuple[Violation, ...]:
    inv = frozenset(provider.inventory)
    findings: list[Violation] = []

    # AC5 -- AuthResolver must scrub inherited Git authorization state.
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_HOST_CRED,
            _AUTH_OWNER,
            ("_clear_git_auth_env(env)",),
            "AuthResolver must scrub inherited Git authorization state",
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_HOST_CRED,
            _AUTH_OWNER,
            (
                'if host_kind == "ado" and not token:',
                "suppress_credential_helpers = True",
            ),
            "AuthResolver must suppress native helpers for every tokenless ADO environment",
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_HOST_CRED,
            "src/apm_cli/core/host_providers.py",
            ('if host_kind == "ado":', "suppress_credential_helpers=True"),
            "ADO transport policy must reject native credential helpers",
        )
    )
    # AC24 -- ADO transport credentials must route through AuthResolver context.
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_HOST_CRED,
            _AUTH_OWNER,
            ("_clear_platform_token_env(env)", "clear_git_platform_token_env"),
            "AuthResolver must route platform-token clearing through the Git child owner",
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_HOST_CRED,
            "src/apm_cli/utils/git_env.py",
            ('"COPILOT_GITHUB_TOKEN"',),
            "The Git child environment owner must retain the platform-token vocabulary",
        )
    )
    _ado_consumers: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("src/apm_cli/deps/github_downloader.py", ("self.auth_resolver.git_env_for_remote(",)),
        (
            "src/apm_cli/deps/github_downloader_validation.py",
            (
                "downloader.auth_resolver.git_env_for_remote(",
                "downloader.auth_resolver.execute_with_bearer_fallback(",
                "downloader.auth_resolver.build_ado_bearer_git_env(",
            ),
        ),
        (
            "src/apm_cli/install/pipeline.py",
            ("probe_env = auth_resolver.git_env_for_remote(", "key = (host, dep.port, org)"),
        ),
        ("src/apm_cli/install/helpers/ref_reuse.py", ("hardened_git_env_for_context",)),
        (
            "src/apm_cli/marketplace/client.py",
            ("resolve_for_remote", "git_env_for_remote"),
        ),
        ("src/apm_cli/marketplace/builder.py", ("hardened_git_env_for_context",)),
        ("src/apm_cli/marketplace/auth_helpers.py", ('ctx.token or ctx.host_info.kind == "ado"',)),
        ("src/apm_cli/commands/marketplace/check.py", ("hardened_git_env_for_context",)),
        ("src/apm_cli/policy/discovery.py", ("auth_resolver.try_with_fallback(",)),
        (
            "src/apm_cli/install/validation.py",
            (
                "auth_resolver.execute_with_bearer_fallback(",
                "auth_resolver.build_ado_bearer_git_env(",
            ),
        ),
        (
            "src/apm_cli/deps/clone_engine.py",
            (
                "host.auth_resolver.git_env_for_remote(",
                "host.auth_resolver.execute_with_bearer_fallback(",
                "host.auth_resolver.build_ado_bearer_git_env(",
                "attempt.effective_url or attempt_url,\n                    base_env=host.git_env,",
            ),
        ),
        (
            "src/apm_cli/deps/git_reference_resolver.py",
            (
                "host.auth_resolver.git_env_for_remote(",
                "host.auth_resolver.execute_with_bearer_fallback(",
                "transport_attempt.effective_url or rewrite_candidate,\n"
                "                base_env=host.git_env,",
            ),
        ),
    )
    for path, needles in _ado_consumers:
        findings.extend(
            _require_subs(
                provider,
                inv,
                _RID_HOST_CRED,
                path,
                needles,
                "ADO transport credentials must route through AuthResolver context",
            )
        )
    # install/pipeline.py must not branch credential selection on the host kind.
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            _RID_HOST_CRED,
            ("src/apm_cli/install/pipeline.py",),
            re.compile(r"if is_generic or is_azure_devops_hostname\(host\):"),
            "ADO credential selection must not re-derive from raw host classification",
            exempt=False,
        )
    )
    # Direct ado_token field reads are forbidden (legacy scan: no exemption filter).
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            _RID_HOST_CRED,
            _ADO_DIRECT_FILES,
            _ADO_DIRECT_TOKEN,
            "ADO tokens must not be read directly off the host; use AuthResolver context",
            exempt=False,
        )
    )
    # AC19 -- dict-merging the build_*_git_env overlay re-introduces the #2368 clobber.
    findings.extend(_scan_auth_header_dictmerge(provider, inv))

    # AC20 -- public and noninteractive Git environments stay owned by AuthResolver.
    findings.extend(
        _require_res(
            provider,
            inv,
            _RID_HOST_CRED,
            _AUTH_OWNER,
            _PUBLIC_GH_METHODS,
            "AuthResolver must define the public/noninteractive Git env methods",
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_HOST_CRED,
            _AUTH_OWNER,
            ('self._append_git_config(env, "http.extraheader", "")',),
            "Native-helper validation must reset inherited Authorization headers",
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_HOST_CRED,
            _AUTH_OWNER,
            ("lazy_public_github",),
            "AuthResolver must retain the lazy public-github anonymous-first path",
        )
    )
    for consumer in _PUBLIC_GH_CONSUMERS:
        findings.extend(
            _require_subs(
                provider,
                inv,
                _RID_HOST_CRED,
                consumer,
                ("uses_public_github_anonymous_first(",),
                "Git transport consumers must consult AuthResolver anonymous-first",
            )
        )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_HOST_CRED,
            "src/apm_cli/deps/github_downloader_validation.py",
            ("build_native_git_credential_env(",),
            "Plain validation retries must obtain native-helper policy from AuthResolver",
        )
    )
    findings.extend(_check_persistent_cache_branch(provider, inv))
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            _RID_HOST_CRED,
            ("src/apm_cli/deps/github_downloader.py",),
            _PERSISTENT_CACHE_BYPASS,
            "Persistent cache checkout must route through _persistent_cache_checkout",
            exempt=False,
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            _RID_HOST_CRED,
            _src_python(provider, exclude={_AUTH_OWNER}),
            _PUBLIC_GH_DUP,
            "uses_public_github_anonymous_first must stay owned by core/auth.py",
            exempt=True,
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            _RID_HOST_CRED,
            _src_python(
                provider,
                exclude={_AUTH_OWNER, "src/apm_cli/deps/git_auth_env.py"},
            ),
            _NONINTERACTIVE_BYPASS,
            "noninteractive Git env must be built only by AuthResolver / git_auth_env.py",
            exempt=True,
        )
    )
    return tuple(findings)


def _check_artifactory_netrc_isolation(provider: FactsProvider) -> tuple[Violation, ...]:
    inv = frozenset(provider.inventory)
    findings: list[Violation] = []

    findings.extend(
        _count_checks(
            provider,
            inv,
            _RID_ARTIFACTORY_NETRC,
            _ARTIFACTORY_NETRC_OWNER,
            (
                ("re", r"^class _NoNetrcSession\(", 1, "eq"),
                ("sub", "with _NoNetrcSession() as session:", 1, "eq"),
            ),
            "artifactory_entry.py must own ambient netrc suppression",
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_ARTIFACTORY_NETRC,
            _ARTIFACTORY_NETRC_OWNER,
            (
                "self.trust_env = False",
                "def rebuild_auth(",
                "self.should_strip_auth(",
            ),
            "Artifactory requests must preserve redirects without ambient netrc auth",
        )
    )
    findings.extend(
        _count_checks(
            provider,
            inv,
            _RID_ARTIFACTORY_NETRC,
            _ARTIFACTORY_NETRC_CONSUMER,
            (("sub", "allow_netrc=False", 3, "eq"),),
            "Every Artifactory resilient GET path must suppress ambient netrc auth",
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_ARTIFACTORY_NETRC,
            _ARTIFACTORY_NETRC_CONSUMER,
            ("from .artifactory_entry import _NoNetrcSession",),
            "Download strategies must reuse the Artifactory netrc-isolation owner",
        )
    )
    return tuple(findings)


def _check_git_child_environment(provider: FactsProvider) -> tuple[Violation, ...]:
    inv = frozenset(provider.inventory)
    findings: list[Violation] = []
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_GIT_CHILD_ENV,
            "src/apm_cli/utils/git_env.py",
            (
                '"GIT_DIR"',
                '"GIT_CONFIG"',
                '"GIT_WORK_TREE"',
                '"GIT_OBJECT_DIRECTORY"',
                "def clone_git_worktree(",
                "def git_network_env(",
                "def git_clone_env(",
                "def init_git_remote_worktree(",
                "def git_no_hooks_args(",
                "def git_no_templates_args(",
                "def git_remote_refs(",
                "def redact_git_diagnostic(",
                "github_pat_",
                "gl(?:agent|cbt|ft|pat|ptt|rt|soat)",
                "eyJ[A-Za-z0-9_-]",
                "AZDO",
                "{52}",
                "def clear_git_auth_env(",
                "def clear_git_platform_token_env(",
                "def set_git_authorization_header(",
                "clear_git_auth_env(env, remove_helpers=True)",
                "def _append_git_url_rewrites(",
                "def _merge_parent_git_config_snapshot(",
                "snapshot = _merge_parent_git_config_snapshot(",
                "def _materialize_git_config_snapshot(",
                "def _build_git_auth_fence(",
                "def _is_valid_http_extraheader_value(",
                'if any(character in value for character in ("\\r", "\\n", "\\0")):',
                "def _urlmatched_header_group(",
                "def _http_config_scope(",
                "def _validated_git_url_rewrite_policy(",
                "def _git_url_host(",
                "def resolve_git_url_rewrite(",
                "effective_url, snapshot = _validated_git_url_rewrite_policy(",
                "if source_host and target_is_network and source_host != target_host:",
                "and not _is_scope_sensitive_network_config(entry)",
                'env["GIT_TRACE_REDACT"] = "1"',
                "_REMOTE_HELPER_RE",
                '"--get-urlmatch"',
                "        _build_git_auth_fence(",
                "intent_snapshot=intent_snapshot",
                "managed_auth_intent=managed_auth_intent",
                'env[_MANAGED_GIT_AUTH_INTENT_ENV] = "1"',
                'managed_auth_intent=env.get(_MANAGED_GIT_AUTH_INTENT_ENV) == "1"',
                "if not managed and not reset_headers and not helper_reset:",
                'f"http.{_http_config_scope(auth_fence.remote_url)}.extraheader"',
                "suppress_helpers=helper_reset or bool(managed)",
                "clone_env = git_clone_env(",
                'return "-c", "core.hooksPath=/dev/null"',
                'return ("--template=",)',
                "def validate_git_url_rewrite_safety(",
            ),
            "utils/git_env.py must own repository-state and URL rewrite safety",
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_GIT_CHILD_ENV,
            "src/apm_cli/deps/git_file_transport.py",
            (
                "from ..utils.git_env import git_subprocess_env, redact_git_diagnostic",
                "safe_stderr = redact_git_diagnostic(result.stderr.strip())",
            ),
            "Sparse Git errors must route through canonical credential redaction",
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            _RID_GIT_CHILD_ENV,
            _src_python(provider, exclude={"src/apm_cli/utils/git_env.py"}),
            re.compile(r"^\s*def _?redact_git_stderr\("),
            "Git diagnostic credential redaction must stay owned by utils/git_env.py",
            exempt=False,
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            _RID_GIT_CHILD_ENV,
            _src_python(
                provider,
                exclude={
                    "src/apm_cli/deps/clone_engine.py",
                    "src/apm_cli/deps/git_reference_resolver.py",
                    "src/apm_cli/deps/github_downloader.py",
                    "src/apm_cli/utils/git_env.py",
                },
            ),
            re.compile(r"^\s*def _?sanitize_git_error\("),
            "Git diagnostic sanitizers must delegate to utils/git_env.py",
            exempt=False,
        )
    )
    for path, needles in (
        (
            "src/apm_cli/deps/github_downloader.py",
            (
                "return redact_git_diagnostic(error_message)",
                "redact_git_diagnostic(result.stderr.strip())",
            ),
        ),
        (
            "src/apm_cli/install/validation.py",
            ("stderr_snippet = redact_git_diagnostic(raw_stderr)",),
        ),
        (
            "src/apm_cli/deps/bare_cache.py",
            ('error_msg += f" Last error: {git_subprocess_error_text(last_error)}"',),
        ),
        (
            "src/apm_cli/cache/git_cache.py",
            ("return redact_git_diagnostic(value)",),
        ),
        (
            "src/apm_cli/marketplace/_git_utils.py",
            ("return redact_git_diagnostic(text)",),
        ),
    ):
        findings.extend(
            _require_subs(
                provider,
                inv,
                _RID_GIT_CHILD_ENV,
                path,
                needles,
                "Git diagnostic rendering must route through utils/git_env.py",
            )
        )
    for path, pattern in (
        (
            "src/apm_cli/deps/github_downloader.py",
            re.compile(r"\{result\.stderr(?:\.strip\(\))?\}"),
        ),
        (
            "src/apm_cli/install/validation.py",
            re.compile(r"stderr_snippet\s*=\s*raw_stderr"),
        ),
        (
            "src/apm_cli/deps/github_downloader_validation.py",
            re.compile(r"(?:_sanitize_git_error\()?str\(exc\)"),
        ),
        (
            "src/apm_cli/deps/bare_cache.py",
            re.compile(r"detail\s*=\s*str\(last_error\)"),
        ),
    ):
        findings.extend(
            _forbid_scan(
                provider,
                inv,
                _RID_GIT_CHILD_ENV,
                (path,),
                pattern,
                "Raw Git diagnostics must be redacted by utils/git_env.py",
                exempt=False,
            )
        )
    findings.extend(_check_git_diagnostic_ownership(provider))
    git_diagnostic_consumers = tuple(
        path
        for path in _src_python(provider, exclude={"src/apm_cli/utils/git_env.py"})
        if path.startswith(
            (
                "src/apm_cli/cache/",
                "src/apm_cli/deps/",
                "src/apm_cli/marketplace/",
            )
        )
        or path
        in {
            "src/apm_cli/commands/marketplace/doctor.py",
            "src/apm_cli/install/pipeline.py",
            "src/apm_cli/install/validation.py",
        }
    )
    for pattern in (
        re.compile(r'print\(f"\[DEBUG\] \{message\}"'),
        re.compile(r"super\(\).__init__\(result\.stderr"),
        re.compile(r'f"git show failed: \{stderr\}"'),
        re.compile(r"git output: \{stderr_text(?:\.strip\(\))?\}"),
    ):
        findings.extend(
            _forbid_scan(
                provider,
                inv,
                _RID_GIT_CHILD_ENV,
                git_diagnostic_consumers,
                pattern,
                "Raw Git stderr and debug rendering must route through utils/git_env.py",
                exempt=False,
            )
        )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_GIT_CHILD_ENV,
            "src/apm_cli/cache/git_cache.py",
            (
                "_FALLBACK_REFSPECS = (",
                "fallback_fetch_args += [url, *_FALLBACK_REFSPECS]",
            ),
            "Fallback fetches must stay bound to the validated remote URL",
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            _RID_GIT_CHILD_ENV,
            ("src/apm_cli/cache/git_cache.py",),
            re.compile(r"""["']fetch["'][^\n]*["']--all["']"""),
            "Network fallback must not fetch every configured remote",
            exempt=False,
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_GIT_CHILD_ENV,
            "src/apm_cli/deps/clone_engine.py",
            (
                "candidate_url=rewrite_candidate",
                "if attempt.requested_url is not None:",
                "url = attempt.requested_url",
            ),
            "Git must apply configured URL rewrites exactly once from the requested URL",
        )
    )
    for path in (
        "src/apm_cli/cache/git_cache.py",
        "src/apm_cli/deps/bare_cache.py",
        "src/apm_cli/deps/git_file_transport.py",
        "src/apm_cli/deps/github_downloader.py",
    ):
        findings.extend(
            _require_subs(
                provider,
                inv,
                _RID_GIT_CHILD_ENV,
                path,
                ("git_no_hooks_args(",),
                "Dependency Git checkouts must disable repository-controlled hooks",
            )
        )
    for path in (
        "src/apm_cli/cache/git_cache.py",
        "src/apm_cli/deps/bare_cache.py",
        "src/apm_cli/deps/git_file_transport.py",
        "src/apm_cli/deps/github_downloader_validation.py",
    ):
        findings.extend(
            _require_subs(
                provider,
                inv,
                _RID_GIT_CHILD_ENV,
                path,
                ("git_no_templates_args(",),
                "Dependency Git repositories must suppress template-provided config",
            )
        )
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            _RID_GIT_CHILD_ENV,
            _src_python(provider, exclude={"src/apm_cli/utils/git_env.py"}),
            re.compile(r"core\.hooksPath=/dev/null"),
            "Git hook suppression must route through utils/git_env.git_no_hooks_args",
            exempt=False,
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            _RID_GIT_CHILD_ENV,
            _src_python(provider, exclude={"src/apm_cli/utils/git_env.py"}),
            re.compile(r"""["']--template=["']"""),
            "Git template suppression must route through utils/git_env.git_no_templates_args",
            exempt=False,
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            _RID_GIT_CHILD_ENV,
            _src_python(provider, exclude={"src/apm_cli/core/auth.py"}),
            re.compile(r"AuthResolver\._clear_(?:git_auth|platform_token)_env\("),
            "Git auth-channel clearing must route through utils/git_env.py",
            exempt=False,
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            _RID_GIT_CHILD_ENV,
            _src_python(
                provider,
                exclude={
                    "src/apm_cli/core/auth.py",
                    "src/apm_cli/utils/github_host.py",
                },
            ),
            re.compile(r"set_authorization_header_git_env\("),
            "Only AuthResolver may select and install a managed Git auth header",
            exempt=False,
        )
    )
    for path, needle in (
        ("src/apm_cli/cache/git_cache.py", "git_clone_env(url, env"),
        (
            "src/apm_cli/deps/bare_cache.py",
            "                    remote_env = git_network_env(url, env, git_dir=target)",
        ),
        ("src/apm_cli/deps/git_file_transport.py", "git_network_env("),
        ("src/apm_cli/deps/git_reference_resolver.py", "git_remote_refs("),
        ("src/apm_cli/deps/github_downloader.py", "git_network_env("),
        ("src/apm_cli/deps/github_downloader_validation.py", "git_network_env("),
        ("src/apm_cli/install/pipeline.py", "git_remote_refs("),
        ("src/apm_cli/install/validation.py", "git_remote_refs("),
        ("src/apm_cli/marketplace/ref_resolver.py", "git_remote_refs("),
        ("src/apm_cli/policy/_gitlab.py", "git_remote_refs("),
    ):
        findings.extend(
            _require_subs(
                provider,
                inv,
                _RID_GIT_CHILD_ENV,
                path,
                (needle,),
                "Network Git operations must route through utils/git_env.git_network_env",
            )
        )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_GIT_CHILD_ENV,
            "src/apm_cli/core/auth.py",
            (
                "from ..utils.git_env import validate_git_url_rewrite_safety",
                "validate_git_url_rewrite_safety(remote_url, env)",
                "from apm_cli.utils.git_env import clear_git_auth_env",
                "from apm_cli.utils.git_env import clear_git_platform_token_env",
            ),
            "AuthResolver must route URL rewrite safety through utils/git_env.py",
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_GIT_CHILD_ENV,
            "src/apm_cli/marketplace/client.py",
            ('reason = f"{reason}; {exc.recovery_hint}"',),
            "Marketplace rewrite errors must preserve the canonical recovery command",
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_GIT_CHILD_ENV,
            "src/apm_cli/utils/github_host.py",
            (
                "from apm_cli.utils.git_env import set_git_authorization_header",
                "set_git_authorization_header(env, scheme, credential)",
            ),
            "Git authorization config replacement must route through utils/git_env.py",
        )
    )
    for path in (
        "src/apm_cli/deps/clone_engine.py",
        "src/apm_cli/deps/download_strategies.py",
        "src/apm_cli/deps/github_downloader.py",
        "src/apm_cli/deps/github_downloader_validation.py",
        "src/apm_cli/install/pipeline.py",
        "src/apm_cli/install/validation.py",
    ):
        findings.extend(
            _require_subs(
                provider,
                inv,
                _RID_GIT_CHILD_ENV,
                path,
                ("git_env_for_remote(",),
                "Credential environments must apply AuthResolver remote transport policy",
            )
        )
    for path, needles in (
        (
            "src/apm_cli/deps/git_auth_env.py",
            (
                "git_subprocess_env(self._token_manager.setup_environment())",
                "env = git_subprocess_env(base_git_env)",
            ),
        ),
        (
            "src/apm_cli/cache/git_cache.py",
            (
                "subprocess_env = git_subprocess_env(env)",
                '"--git-dir", str(bare_dir)',
            ),
        ),
        (
            "src/apm_cli/deps/github_downloader.py",
            ("checkout_git_worktree(", "git_network_env("),
        ),
        (
            "src/apm_cli/deps/bare_cache.py",
            ("env = git_subprocess_env(env)", "clone_git_worktree("),
        ),
        (
            "src/apm_cli/deps/github_downloader_validation.py",
            ("probe_env = git_subprocess_env(env)",),
        ),
        (
            "src/apm_cli/deps/git_file_transport.py",
            ("env=git_subprocess_env(self._git_env)",),
        ),
        (
            "src/apm_cli/deps/git_reference_resolver.py",
            ("git_resolve_commit(", "git_worktree_head("),
        ),
        (
            "src/apm_cli/deps/transport_selection.py",
            (
                "configured_git_url_policy",
                "resolve_git_url_rewrite",
                "validate_resolved_git_url_rewrite",
            ),
        ),
    ):
        findings.extend(
            _require_subs(
                provider,
                inv,
                _RID_GIT_CHILD_ENV,
                path,
                needles,
                "Git repository operations must route through utils/git_env.py",
            )
        )
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            _RID_GIT_CHILD_ENV,
            _paths_under(provider, "src/apm_cli/deps/", (".py",)),
            re.compile(
                r"\.git\.checkout\("
                r"|\brepo\.(commit|head|active_branch|refs|tags)\b"
                r"|git\.cmd\.Git\(str\("
                r"|\.clone_from\("
            ),
            "Repository-scoped GitPython operations must use utils/git_env",
            exempt=False,
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            _RID_GIT_CHILD_ENV,
            ("src/apm_cli/cache/git_cache.py",),
            re.compile(r"env if env is not None else git_subprocess_env\(\)"),
            "Git cache must sanitize explicit environments as well as ambient ones",
            exempt=False,
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            _RID_GIT_CHILD_ENV,
            ("src/apm_cli/deps/github_downloader.py",),
            re.compile(r"env\s*=\s*\{\*\*os\.environ"),
            "Git downloader subprocess environments must use utils/git_env.py",
            exempt=False,
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            _RID_GIT_CHILD_ENV,
            _src_python(provider, exclude={"src/apm_cli/utils/git_env.py"}),
            re.compile(
                r"^\s*def (?:git_network_env|has_https_to_http_url_rewrite"
                r"|validate_git_url_rewrite_safety)\("
            ),
            "Git URL rewrite safety must stay owned by utils/git_env.py",
            exempt=False,
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            _RID_GIT_CHILD_ENV,
            (
                "src/apm_cli/deps/clone_engine.py",
                "src/apm_cli/deps/git_reference_resolver.py",
                "src/apm_cli/install/pipeline.py",
                "src/apm_cli/install/validation.py",
            ),
            re.compile(
                r"(?<![A-Za-z0-9_])token\s*=\s*"
                r"(?:dep_token|_url_token|token(?:\s+or)?\b)"
            ),
            "Git credentials must use headers and stay out of URL arguments",
            exempt=False,
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_GIT_CHILD_ENV,
            "src/apm_cli/deps/download_strategies.py",
            (
                "tokenless_url_builder = partial(",
                'token="",',
                "build_repo_url_fn=tokenless_url_builder",
            ),
            "Git file transport must keep managed credentials out of remote URLs",
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_GIT_CHILD_ENV,
            "src/apm_cli/install/validation.py",
            (
                "transport_plan = ado_downloader._transport_selector.select(",
                "cli_pref=resolved_pref",
                "allow_fallback=resolved_fallback",
                "except (GitUrlRewriteError, GitUrlRewriteProbeError):",
            ),
            "Positional validation must reuse canonical transport and rewrite policy",
        )
    )
    return tuple(findings)


def _check_git_diagnostic_ownership(provider: FactsProvider) -> tuple[Violation, ...]:
    """Require one redaction owner and only explicit thin compatibility delegates."""
    findings: list[Violation] = []
    downloader_path = "src/apm_cli/deps/github_downloader.py"
    downloader_sanitizers = 0
    for path in _src_python(provider):
        _facts, failures = checked_facts(
            provider,
            path,
            _RID_GIT_CHILD_ENV,
            require_python=True,
        )
        if failures:
            findings.extend(failures)
            continue
        index = provider.tree_index(path)
        if index is None:
            continue
        for definition in index.functions():
            name = getattr(definition, "name", "")
            if name == "redact_git_diagnostic" and path != "src/apm_cli/utils/git_env.py":
                findings.append(
                    violation(
                        _RID_GIT_CHILD_ENV,
                        path,
                        "Git diagnostic redaction must stay owned by utils/git_env.py",
                        line=definition.lineno,
                    )
                )
                continue
            if name not in {"sanitize_git_error", "_sanitize_git_error", "_redact_git_stderr"}:
                continue
            statements = list(getattr(definition, "body", ()))
            if (
                len(statements) == 1
                and isinstance(statements[0], ast.Expr)
                and isinstance(statements[0].value, ast.Constant)
                and statements[0].value.value is Ellipsis
            ):
                continue
            if path == downloader_path and name == "_sanitize_git_error":
                downloader_sanitizers += 1
                if (
                    statements
                    and isinstance(statements[0], ast.Expr)
                    and isinstance(statements[0].value, ast.Constant)
                    and isinstance(statements[0].value.value, str)
                ):
                    statements = statements[1:]
                thin_delegate = (
                    len(statements) == 1
                    and isinstance(statements[0], ast.Return)
                    and isinstance(statements[0].value, ast.Call)
                    and isinstance(statements[0].value.func, ast.Name)
                    and statements[0].value.func.id == "redact_git_diagnostic"
                    and len(statements[0].value.args) == 1
                    and isinstance(statements[0].value.args[0], ast.Name)
                    and statements[0].value.args[0].id == "error_message"
                    and not statements[0].value.keywords
                )
                if thin_delegate:
                    continue
            findings.append(
                violation(
                    _RID_GIT_CHILD_ENV,
                    path,
                    "Git diagnostic sanitizers must be thin delegates to utils/git_env.py",
                    line=definition.lineno,
                )
            )
    if downloader_sanitizers != 1:
        findings.append(
            violation(
                _RID_GIT_CHILD_ENV,
                downloader_path,
                "github_downloader must define exactly one thin _sanitize_git_error delegate",
            )
        )
    return tuple(findings)


def _scan_auth_header_dictmerge(
    provider: FactsProvider, inv: frozenset[str]
) -> tuple[Violation, ...]:
    """AC19: scan src/apm_cli for build_* overlay dict-merges, skipping comment lines."""
    findings: list[Violation] = []
    for path in _src_python(provider):
        facts, failures = checked_facts(provider, path, _RID_HOST_CRED, require_python=True)
        if failures:
            findings.extend(failures)
            continue
        for number, line in enumerate(facts.lines, start=1):
            if EXEMPT_MARKER in line or line.lstrip().startswith("#"):
                continue
            match = _AUTH_HEADER_DICTMERGE.search(line)
            if match is not None:
                findings.append(
                    violation(
                        _RID_HOST_CRED,
                        path,
                        "Authorization-header injection must use the in-place setter, "
                        "not a build_* overlay dict-merge (re-introduces the #2368 clobber)",
                        line=number,
                        column=match.start() + 1,
                    )
                )
    return tuple(findings)


def _check_persistent_cache_branch(
    provider: FactsProvider, inv: frozenset[str]
) -> tuple[Violation, ...]:
    """AC20: the _persistent_cache_checkout body owns anonymous->authenticated fallback."""
    path = "src/apm_cli/deps/github_downloader.py"
    facts, failures = _load(provider, inv, _RID_HOST_CRED, path, parse=True)
    if failures:
        return failures
    index = provider.tree_index(path)
    if index is None:
        return (
            violation(
                _RID_HOST_CRED,
                path,
                "github_downloader persistent checkout owner could not be inspected",
            ),
        )
    owners = direct_definitions(
        index,
        "GitHubPackageDownloader",
        kinds=(ast.ClassDef,),
    )
    owner = effective_definition(
        index,
        "GitHubPackageDownloader",
        kinds=(ast.ClassDef,),
    )
    definitions = (
        ()
        if owner is None
        else direct_definitions(
            index,
            "_persistent_cache_checkout",
            parent=owner,
            kinds=FUNCTION_NODES,
        )
    )
    if owner is None or not definitions:
        return (
            violation(
                _RID_HOST_CRED,
                path,
                "github_downloader must own _persistent_cache_checkout for public-github fallback",
            ),
        )
    definition = definitions[-1]
    body = facts.lines[definition.lineno - 1 : definition.end_lineno or definition.lineno]
    body_text = "\n".join(body)
    problems: list[str] = []
    if len(owners) != 1:
        problems.append(
            f"GitHubPackageDownloader must be defined exactly once, found {len(owners)}"
        )
    if len(definitions) != 1:
        problems.append(
            f"_persistent_cache_checkout must be defined exactly once, found {len(definitions)}"
        )
    if "self.auth_resolver.try_with_fallback(" not in body_text:
        problems.append("must call self.auth_resolver.try_with_fallback(")
    if "self.auth_resolver.build_public_github_authenticated_git_env(" not in body_text:
        problems.append("must call self.auth_resolver.build_public_github_authenticated_git_env(")
    if _count_sub(facts, "self._persistent_cache_checkout(") < 2:
        problems.append("_persistent_cache_checkout must be invoked at least twice")
    if problems:
        return (
            violation(
                _RID_HOST_CRED,
                path,
                "public-github persistent cache branch drifted: " + "; ".join(problems),
                line=definition.lineno,
            ),
        )
    return ()


_RID_URL_PATH = "transport-platform-url-path-security"


_PATH_SECURITY_OWNER = "src/apm_cli/utils/path_security.py"


_SYMLINK_DEF = re.compile(r"^def has_symlink_component\(")


_UNQUOTE = re.compile(r"unquote(_to_bytes)?\(")


_URL_PATH_DECODER_CONSUMERS: tuple[str, ...] = (
    "src/apm_cli/marketplace/yml_schema.py",
    "src/apm_cli/models/dependency/reference.py",
    "src/apm_cli/commands/marketplace/__init__.py",
)


def _check_url_path_security(provider: FactsProvider) -> tuple[Violation, ...]:
    inv = frozenset(provider.inventory)
    findings: list[Violation] = []

    # Symlink-component containment: owner defines it exactly once, no duplicates.
    findings.extend(
        _count_checks(
            provider,
            inv,
            _RID_URL_PATH,
            _PATH_SECURITY_OWNER,
            (("re", r"^def has_symlink_component\(", 1, "eq"),),
            "has_symlink_component must be owned by utils/path_security.py",
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            _RID_URL_PATH,
            _src_python(provider, exclude={_PATH_SECURITY_OWNER}),
            _SYMLINK_DEF,
            "Symlink-component containment must route through utils/path_security.py",
            exempt=False,
        )
    )

    # AC10a -- strict percent-encoded URL-path decoding authority.
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_URL_PATH,
            _PATH_SECURITY_OWNER,
            ("def parse_url_path_segments(",),
            "path_security.py must own parse_url_path_segments",
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_URL_PATH,
            "src/apm_cli/marketplace/yml_schema.py",
            (
                "decode_url_path_segments(parsed.path, context=context)",
                'decode_url_path_segments(parsed.path, context="sourceBase")',
            ),
            "yml_schema must decode URL paths through path_security",
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_URL_PATH,
            "src/apm_cli/models/dependency/reference.py",
            ("parse_url_path_segments(",),
            "dependency reference must decode URL paths through path_security",
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_URL_PATH,
            "src/apm_cli/marketplace/source_identity.py",
            ("decode_url_path_segments(path, context=context)",),
            "marketplace source owner must decode URL paths through path_security",
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            _RID_URL_PATH,
            _URL_PATH_DECODER_CONSUMERS,
            _UNQUOTE,
            "Strict percent-encoded URL paths must use path_security parsing, not raw unquote",
            exempt=True,
        )
    )
    return tuple(findings)


_RID_WINDOWS = "transport-platform-windows-stable-path"


_INSTALL_PS1 = "install.ps1"


_WINDOWS_OWNER_STATEMENTS: tuple[str, ...] = (
    '$currentDir = Join-Path $installRoot "current"',
    '$currentExe = Join-Path $currentDir "apm.exe"',
    "Add-ToUserPath -PathEntry $currentDir",
)


_LITERAL_STABLE_EXE = re.compile(r"\bcurrent[\\/]apm\.exe")


_JOIN_PATH_CALL = re.compile(r"Join-Path", re.IGNORECASE)


_QUOTED_CURRENT = re.compile(r"""['"]current['"]""")


_WINDOWS_GUARDED: tuple[tuple[str, tuple[str, ...]], ...] = (
    (_SRC_PREFIX, (".py",)),
    (".github/workflows/", (".yml", ".yaml")),
    ("scripts/windows/", (".ps1",)),
)


def _windows_duplicate_line(line: str) -> bool:
    if EXEMPT_MARKER in line:
        return False
    if _LITERAL_STABLE_EXE.search(line):
        return True
    return bool(_JOIN_PATH_CALL.search(line) and _QUOTED_CURRENT.search(line))


def _check_windows_stable_path(provider: FactsProvider) -> tuple[Violation, ...]:
    inv = frozenset(provider.inventory)
    findings: list[Violation] = []

    # Owner presence: install.ps1 must carry each verbatim owner statement.
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_WINDOWS,
            _INSTALL_PS1,
            _WINDOWS_OWNER_STATEMENTS,
            "install.ps1 must own the Windows stable current/apm.exe path",
        )
    )

    # Duplicate derivation scan across guarded production locations.
    for prefix, suffixes in _WINDOWS_GUARDED:
        for path in _paths_under(provider, prefix, suffixes):
            name = path.rsplit("/", 1)[-1]
            if name.startswith("test-"):
                continue
            facts, failures = checked_facts(
                provider, path, _RID_WINDOWS, require_python=path.endswith(".py")
            )
            if failures:
                findings.extend(failures)
                continue
            for number, line in enumerate(facts.lines, start=1):
                if _windows_duplicate_line(line):
                    findings.append(
                        violation(
                            _RID_WINDOWS,
                            path,
                            "duplicate Windows stable-path derivation; install.ps1 is the owner",
                            line=number,
                        )
                    )
    return tuple(findings)


RULES: tuple[Rule, ...] = (
    Rule(
        id=_RID_ARTIFACTORY_NETRC,
        group=GROUP,
        guard_ids=(_RID_ARTIFACTORY_NETRC,),
        description="Artifactory HTTP requests exclude ambient netrc credentials.",
        check=_check_artifactory_netrc_isolation,
    ),
    Rule(
        id=_RID_HOST_CRED,
        group=GROUP,
        guard_ids=(
            _RID_ADO_VALIDATION,
            _RID_ADO_CALLER_CONFIG,
            _RID_ADO_CLONE_FALLBACK,
            _RID_ADO_HELPER_SUPPRESSION,
            _RID_HOST_CRED,
        ),
        description="Host + credential resolution stays owned by core/auth.py (AuthResolver).",
        check=_check_host_credential_resolution,
    ),
    Rule(
        id=_RID_GIT_CHILD_ENV,
        group=GROUP,
        guard_ids=(
            _RID_GIT_CHILD_ENV,
            _RID_GIT_CLONE_HOOKS,
            _RID_GIT_CLONE_TEMPLATES,
            _RID_GIT_DIAGNOSTIC,
            _RID_GIT_DIAGNOSTIC_DEBUG,
            _RID_GIT_DIAGNOSTIC_OWNER,
            _RID_GIT_DIAGNOSTIC_DELEGATE,
            _RID_GIT_DIAGNOSTIC_TOKENS,
            _RID_GIT_DIAGNOSTIC_JWT,
            _RID_GIT_SINGLE_REMOTE,
            _RID_GIT_URL_CREDENTIALS,
            _RID_GIT_URL_HEADER,
            _RID_GIT_URL_HEADER_FENCE,
            _RID_GIT_URL_HEADER_MALFORMED,
            _RID_GIT_URL_HEADER_MANAGED,
            _RID_GIT_URL_ENFORCEMENT,
            _RID_GIT_URL_ONCE,
            _RID_GIT_URL_ROUTING,
            _RID_GIT_URL_VALIDATION_ROUTING,
            _RID_GIT_URL_RECOVERY,
            _RID_GIT_URL_REWRITE,
        ),
        description=("Git child processes cannot inherit repository state or unsafe URL rewrites."),
        check=_check_git_child_environment,
    ),
    Rule(
        id=_RID_URL_PATH,
        group=GROUP,
        guard_ids=(_RID_URL_PATH,),
        description="Symlink containment and strict URL-path decoding stay in path_security.py.",
        check=_check_url_path_security,
    ),
    Rule(
        id=_RID_WINDOWS,
        group=GROUP,
        guard_ids=(_RID_WINDOWS,),
        description="install.ps1 is the sole owner of the Windows stable current/apm.exe path.",
        check=_check_windows_stable_path,
    ),
)


COLLECTORS: tuple[object, ...] = ()


__all__ = ["COLLECTORS", "RULES"]
