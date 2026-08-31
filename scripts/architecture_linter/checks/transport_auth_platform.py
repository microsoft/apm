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


_AUTH_OWNER = "src/apm_cli/core/auth.py"


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

    # AC24 -- ADO transport credentials must route through AuthResolver context.
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_HOST_CRED,
            _AUTH_OWNER,
            ("_clear_platform_token_env(env)", '"COPILOT_GITHUB_TOKEN"'),
            "AuthResolver must clear platform token env and own COPILOT_GITHUB_TOKEN",
        )
    )
    _ado_consumers: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("src/apm_cli/deps/github_downloader.py", ("self.auth_resolver.git_env_for_context(",)),
        (
            "src/apm_cli/deps/github_downloader_validation.py",
            ("downloader.auth_resolver.git_env_for_context(",),
        ),
        (
            "src/apm_cli/install/pipeline.py",
            ("probe_env = auth_resolver.git_env_for_context(", "key = (host, dep.port, org)"),
        ),
        ("src/apm_cli/install/helpers/ref_reuse.py", ("hardened_git_env_for_context",)),
        ("src/apm_cli/marketplace/client.py", ("hardened_git_env_for_context",)),
        ("src/apm_cli/marketplace/builder.py", ("hardened_git_env_for_context",)),
        ("src/apm_cli/marketplace/auth_helpers.py", ('ctx.token or ctx.host_info.kind == "ado"',)),
        ("src/apm_cli/commands/marketplace/check.py", ("hardened_git_env_for_context",)),
        ("src/apm_cli/policy/discovery.py", ("auth_resolver.try_with_fallback(",)),
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
            "src/apm_cli/commands/marketplace/__init__.py",
            ('decode_url_path_segments(parsed.path, context="marketplace URL path")',),
            "marketplace command must decode URL paths through path_security",
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
        id=_RID_HOST_CRED,
        group=GROUP,
        guard_ids=(_RID_HOST_CRED,),
        description="Host + credential resolution stays owned by core/auth.py (AuthResolver).",
        check=_check_host_credential_resolution,
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
