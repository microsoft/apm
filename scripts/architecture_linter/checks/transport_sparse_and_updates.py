"""Sparse-checkout, symlink, and self-update transport analyzers.

Ports two of the ten canonical owner decisions in
``.apm/architecture/owners/transport-auth-platform.json``:

* ``transport-platform-sparse-symlink-validation`` -- ``utils/git_sparse.py``
  owns sparse-cone setup and dangling-symlink repair (legacy AC11a).
* ``transport-platform-self-update-resolution`` -- ``commands/self_update.py``
  owns self-update release -> installer ref + VERSION (legacy AC26).

Every check reads source exclusively through the shared
:class:`~scripts.architecture_linter.facts.FactsProvider`; nothing here opens
files, walks the filesystem, re-parses source, or shells out.
"""

from __future__ import annotations

import re

from scripts.architecture_linter.checks.transport_platform_shared import (
    _GH_DOWNLOADER,
    GROUP,
    _count_checks,
    _forbid_scan,
    _load,
    _require_subs,
    _src_python,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.groups.common import violation
from scripts.architecture_linter.models import Rule, Violation

_RID_SPARSE = "transport-platform-sparse-symlink-validation"


_GIT_SPARSE_OWNER = "src/apm_cli/utils/git_sparse.py"


_GIT_CACHE = "src/apm_cli/cache/git_cache.py"


_RAW_SPARSE_SET = re.compile(r'"sparse-checkout",\s*"set"')


def _check_sparse_symlink_validation(provider: FactsProvider) -> tuple[Violation, ...]:
    inv = frozenset(provider.inventory)
    findings: list[Violation] = []

    findings.extend(
        _count_checks(
            provider,
            inv,
            _RID_SPARSE,
            _GIT_SPARSE_OWNER,
            (
                ("re", r"^def apply_sparse_cone\(", 1, "eq"),
                ("re", r"^def repair_dangling_cone_symlinks\(", 1, "eq"),
                ("re", r"^def _literal_pathspec\(", 1, "eq"),
                ("sub", '"ls-tree",', 2, "eq"),
                ("sub", "_literal_pathspec(path)", 2, "eq"),
            ),
            "Sparse-cone materialization must stay owned by utils/git_sparse.py",
        )
    )
    findings.extend(
        _count_checks(
            provider,
            inv,
            _RID_SPARSE,
            _GIT_CACHE,
            (
                ("re", r"^    def _finalize_sparse_checkout\(", 1, "eq"),
                ("sub", "self._finalize_sparse_checkout(", 3, "eq"),
                ("sub", "repair_dangling_cone_symlinks(", 1, "eq"),
            ),
            "git_cache sparse checkout must route through the canonical owner",
        )
    )
    findings.extend(
        _count_checks(
            provider,
            inv,
            _RID_SPARSE,
            "src/apm_cli/deps/bare_cache.py",
            (("sub", "repair_dangling_cone_symlinks(", 1, "eq"),),
            "bare_cache must repair dangling cone symlinks through the owner",
        )
    )
    findings.extend(
        _count_checks(
            provider,
            inv,
            _RID_SPARSE,
            _GH_DOWNLOADER,
            (("sub", "repair_dangling_cone_symlinks(", 1, "eq"),),
            "github_downloader must repair dangling cone symlinks through the owner",
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_SPARSE,
            _GH_DOWNLOADER,
            ("return _repair(setup_env)", "return _repair(env)"),
            "github_downloader must return the repaired sparse environment",
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            _RID_SPARSE,
            _src_python(
                provider,
                exclude={_GIT_SPARSE_OWNER, "src/apm_cli/deps/git_file_transport.py"},
            ),
            _RAW_SPARSE_SET,
            "Raw 'sparse-checkout set' must route through utils/git_sparse.py",
            exempt=True,
        )
    )
    return tuple(findings)


_RID_SELF_UPDATE = "transport-platform-self-update-resolution"


_SELF_UPDATE_OWNER = "src/apm_cli/commands/self_update.py"


_SELF_UPDATE_DEFS = re.compile(
    r"^class _ResolvedSelfUpdateRelease:|^def _resolve_self_update_release\("
)

_RID_UNIX_INSTALL = "transport-platform-unix-install-ownership"


_UNIX_INSTALL_OWNER = "install.sh"


_BUNDLE_IDENTITY_DEFINITION = re.compile(
    r"^\s*(?:(?:async\s+)?def\s+|function\s+)?"
    r"apm_is_recognized_bundle\s*(?:\(\s*[^)]*\))?\s*(?:\{|:)"
)


_CANONICAL_BUNDLE_IDENTITY = (
    '[ -f "$1/apm" ] && [ ! -L "$1/apm" ] && { '
    '{ [ -f "$1/.apm-installed" ] && [ ! -L "$1/.apm-installed" ]; } || '
    '{ [ -f "$1/VERSION" ] && [ ! -L "$1/VERSION" ] && '
    '[ -d "$1/_internal" ] && [ ! -L "$1/_internal" ]; } }'
)


_LOCAL_BUNDLE_IDENTITY = re.compile(
    r"\$_(?:candidate_lib|apm_lib_dir)/(?:\.apm-installed|VERSION|_internal)"
)


def _shell_function_body(
    lines: tuple[str, ...], name: str
) -> tuple[int, int, tuple[str, ...]] | None:
    """Return a top-level shell function's line range and body."""
    definition = re.compile(rf"^{re.escape(name)}\(\)\s*\{{\s*$")
    for start, line in enumerate(lines):
        if definition.search(line) is None:
            continue
        for end in range(start + 1, len(lines)):
            if re.fullmatch(r"}\s*", lines[end]) is not None:
                return start, end, lines[start + 1 : end]
        return None
    return None


def _normalized_shell(lines: tuple[str, ...]) -> str:
    """Collapse shell layout while preserving the predicate's exact tokens."""
    return " ".join(" ".join(lines).split())


def _check_unix_bundle_identity(
    provider: FactsProvider, inventory: frozenset[str]
) -> tuple[Violation, ...]:
    """Keep recognition semantics canonical across discovery and deletion."""
    facts, failures = _load(
        provider,
        inventory,
        _RID_UNIX_INSTALL,
        _UNIX_INSTALL_OWNER,
    )
    if failures:
        return failures

    lines = facts.lines
    findings: list[Violation] = []
    helper_definitions = [
        number
        for number, line in enumerate(lines, start=1)
        if _BUNDLE_IDENTITY_DEFINITION.search(line) is not None
    ]
    if len(helper_definitions) != 1:
        findings.append(
            violation(
                _RID_UNIX_INSTALL,
                _UNIX_INSTALL_OWNER,
                "Unix bundle identity must have exactly one canonical "
                "apm_is_recognized_bundle definition",
            )
        )

    helper = _shell_function_body(lines, "apm_is_recognized_bundle")
    probe = _shell_function_body(lines, "apm_probe_installation")
    validator = _shell_function_body(lines, "apm_lib_dir_validate")
    ownership_begin = [
        index for index, line in enumerate(lines) if line.strip() == "# INSTALL_OWNERSHIP_BEGIN"
    ]
    ownership_end = [
        index for index, line in enumerate(lines) if line.strip() == "# INSTALL_OWNERSHIP_END"
    ]

    if (
        helper is None
        or probe is None
        or len(ownership_begin) != 1
        or len(ownership_end) != 1
        or not (
            ownership_begin[0] < helper[0] < probe[0] < ownership_end[0]
            and helper[1] < ownership_end[0]
        )
    ):
        findings.append(
            violation(
                _RID_UNIX_INSTALL,
                _UNIX_INSTALL_OWNER,
                "apm_is_recognized_bundle must stay inside INSTALL_OWNERSHIP "
                "before apm_probe_installation",
            )
        )

    if helper is not None and _normalized_shell(helper[2]) != _CANONICAL_BUNDLE_IDENTITY:
        findings.append(
            violation(
                _RID_UNIX_INSTALL,
                _UNIX_INSTALL_OWNER,
                "apm_is_recognized_bundle must preserve regular launcher plus "
                "regular marker-or-legacy VERSION/internal identity semantics",
                line=helper[0] + 1,
            )
        )

    discovery_route = 'if ! apm_is_recognized_bundle "$_candidate_lib"; then'
    if probe is None or sum(line.strip() == discovery_route for line in probe[2]) != 1:
        findings.append(
            violation(
                _RID_UNIX_INSTALL,
                _UNIX_INSTALL_OWNER,
                "apm_probe_installation must route bundle identity through "
                "apm_is_recognized_bundle",
            )
        )

    nonempty_guard = 'if [ -d "$_apm_lib_dir" ] && [ "$(ls -A "$_apm_lib_dir" 2>/dev/null)" ]; then'
    deletion_route = 'if ! apm_is_recognized_bundle "$_apm_lib_dir"; then'
    validator_lines = () if validator is None else validator[2]
    guarded_route = any(
        line.strip() == nonempty_guard
        and index + 1 < len(validator_lines)
        and validator_lines[index + 1].strip() == deletion_route
        for index, line in enumerate(validator_lines)
    )
    if not guarded_route:
        findings.append(
            violation(
                _RID_UNIX_INSTALL,
                _UNIX_INSTALL_OWNER,
                "apm_lib_dir_validate must route non-empty deletion identity "
                "through apm_is_recognized_bundle",
            )
        )

    helper_range = range(helper[0], helper[1] + 1) if helper is not None else range(0)
    helper_line_indexes = frozenset(helper_range)
    for index, line in enumerate(lines):
        if index in helper_line_indexes:
            continue
        match = _LOCAL_BUNDLE_IDENTITY.search(line)
        if match is not None:
            findings.append(
                violation(
                    _RID_UNIX_INSTALL,
                    _UNIX_INSTALL_OWNER,
                    "Discovery and deletion must not re-derive or special-case "
                    "bundle identity outside apm_is_recognized_bundle",
                    line=index + 1,
                    column=match.start() + 1,
                )
            )

    return tuple(findings)


def _check_unix_install_ownership(provider: FactsProvider) -> tuple[Violation, ...]:
    """Keep destination policy and unprivileged replacement in the Unix installer."""
    inv = frozenset(provider.inventory)
    findings = list(
        _count_checks(
            provider,
            inv,
            _RID_UNIX_INSTALL,
            "install.sh",
            (
                ("re", r"^apm_resolve_install_paths\(\)", 1, "eq"),
                (
                    "sub",
                    "apm_resolve_install_paths /usr/local/bin/apm /opt/homebrew/bin/apm",
                    3,
                    "eq",
                ),
                ("re", r"^apm_require_owned_bundle$", 1, "eq"),
                ("sub", 'apm_require_writable_directory "$APM_INSTALL_DIR"', 1, "eq"),
                ("sub", 'apm_require_writable_directory "$(dirname "$APM_LIB_DIR")"', 1, "eq"),
            ),
            "Unix early bootstrap, pip fallback and replacement must route through ownership preflight",
        )
    )
    findings.extend(_check_unix_bundle_identity(provider, inv))
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            _RID_UNIX_INSTALL,
            ("install.sh",),
            re.compile(r"^\s*(?:(?:command|env)\s+)?sudo\s"),
            "Unix installation must never invoke sudo automatically",
            exempt=False,
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            _RID_UNIX_INSTALL,
            tuple(
                path
                for path in provider.inventory
                if (path.startswith("src/") and path.endswith(".py"))
                or (path.endswith(".sh") and path != "install.sh")
            ),
            re.compile(
                r"^\s*(?:(?:async\s+)?def\s+|function\s+)?"
                r"apm_(?:is_recognized_bundle|resolve_install_paths|probe_installation|"
                r"require_owned_bundle)\s*[({]"
            ),
            "Unix installation destination and ownership decisions belong only to install.sh",
            exempt=False,
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_UNIX_INSTALL,
            _SELF_UPDATE_OWNER,
            ('env["APM_SELF_UPDATE_SOURCE"] = os.path.abspath(',),
            "Self-update must pass running identity to the installer destination owner",
        )
    )
    return tuple(findings)


def _check_self_update_resolution(provider: FactsProvider) -> tuple[Violation, ...]:
    inv = frozenset(provider.inventory)
    findings: list[Violation] = []

    findings.extend(
        _count_checks(
            provider,
            inv,
            _RID_SELF_UPDATE,
            _SELF_UPDATE_OWNER,
            (
                (
                    "re",
                    r"^class _ResolvedSelfUpdateRelease:|^def _resolve_self_update_release\(",
                    2,
                    "eq",
                ),
            ),
            "self_update.py must own _ResolvedSelfUpdateRelease and its resolver",
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inv,
            _RID_SELF_UPDATE,
            _src_python(provider, exclude={_SELF_UPDATE_OWNER}),
            _SELF_UPDATE_DEFS,
            "Self-update release resolution must stay owned by commands/self_update.py",
            exempt=True,
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_SELF_UPDATE,
            _SELF_UPDATE_OWNER,
            (
                "release = _resolve_self_update_release(latest_version)",
                "resolved_ref = release.tag if release is not None else _INSTALL_SCRIPT_REF",
                "env[_ENV_VERSION] = release.tag",
                "_get_update_installer_url(release)",
                "_build_self_update_installer_env(release)",
            ),
            "Self-update installer URL and VERSION must share _ResolvedSelfUpdateRelease",
        )
    )
    findings.extend(
        _require_subs(
            provider,
            inv,
            _RID_SELF_UPDATE,
            "src/apm_cli/utils/version_checker.py",
            ("return _normalize_release_tag(pinned)",),
            "version_checker must normalize the pinned release tag through the shared owner",
        )
    )
    return tuple(findings)


RULES: tuple[Rule, ...] = (
    Rule(
        id=_RID_UNIX_INSTALL,
        group=GROUP,
        guard_ids=(_RID_UNIX_INSTALL,),
        description="Unix installer owns destination selection and unprivileged replacement.",
        check=_check_unix_install_ownership,
    ),
    Rule(
        id=_RID_SPARSE,
        group=GROUP,
        guard_ids=(_RID_SPARSE,),
        description="Sparse-cone setup and symlink repair stay owned by utils/git_sparse.py.",
        check=_check_sparse_symlink_validation,
    ),
    Rule(
        id=_RID_SELF_UPDATE,
        group=GROUP,
        guard_ids=(_RID_SELF_UPDATE,),
        description="Self-update release -> installer ref + VERSION share one resolver owner.",
        check=_check_self_update_resolution,
    ),
)


COLLECTORS: tuple[object, ...] = ()


__all__ = ["COLLECTORS", "RULES"]
