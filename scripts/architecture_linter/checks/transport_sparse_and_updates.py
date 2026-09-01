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
    _require_subs,
    _src_python,
)
from scripts.architecture_linter.facts import FactsProvider
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
