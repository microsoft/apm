"""Ownership check for archive extraction limits (#4235).

``utils/archive.py`` alone reads archive members. ``safe_extract_tar`` and
``safe_extract_zip`` apply the same limits to every format: an entry-count
cap, a streamed uncompressed-size cap, and refusal of links, special files,
absolute or traversing paths and duplicate entries. ``read_zip_member`` is the
single in-memory member reader under the same cap. Every other module hands
an opened archive to the owner instead of reading members itself.
"""

from __future__ import annotations

import re

from scripts.architecture_linter.checks.transport_platform_shared import (
    GROUP,
    _count_checks,
    _forbid_scan,
    _src_python,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.models import Rule, Violation

_RULE_ID = "transport-platform-archive-extraction-limits"
_OWNER = "src/apm_cli/utils/archive.py"
_REGISTRY_EXTRACTOR = "src/apm_cli/deps/registry/extractor.py"
# Member reads; ``archive.open(info, "w")`` writes a member and stays allowed.
_MEMBER_READ = re.compile(
    r"""\.extractfile\(|\.extractall\(|\.extract\(|\bzf\.read\(|\.open\(info\b(?!,\s*["']w["'])"""
)


def _check_archive_extraction_limits(provider: FactsProvider) -> tuple[Violation, ...]:
    """Keep archive member reads, and their limits, in one owner."""
    inventory = frozenset(provider.inventory)
    findings: list[Violation] = []
    findings.extend(
        _count_checks(
            provider,
            inventory,
            _RULE_ID,
            _OWNER,
            (
                ("re", r"^def safe_extract_tar\(", 1, "eq"),
                ("re", r"^def safe_extract_zip\(", 1, "eq"),
                ("re", r"^def read_zip_member\(", 1, "eq"),
                ("sub", "_claim_member(seen, ", 2, "eq"),
                ("sub", "total_uncompressed = _stream_member(", 1, "eq"),
                ("sub", "_write_planned(plan, ", 2, "eq"),
                ("sub", "_archive_member_target(", 3, "eq"),
            ),
            "tar and zip extraction must validate every entry, then write through one size-capped writer",
        )
    )
    findings.extend(
        _count_checks(
            provider,
            inventory,
            _RULE_ID,
            _REGISTRY_EXTRACTOR,
            (
                ("sub", "safe_extract_tar(tar, dest_root, error_type=UnsafeTarballError)", 1, "eq"),
                ("sub", "safe_extract_zip(zf, dest_root, error_type=UnsafeTarballError)", 1, "eq"),
            ),
            "registry archives must extract through the archive-limits owner for every format",
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inventory,
            _RULE_ID,
            _src_python(provider, exclude={_OWNER}),
            _MEMBER_READ,
            "Only utils/archive.py may read archive members; hand the archive to the owner",
            exempt=True,
        )
    )
    return tuple(findings)


RULES: tuple[Rule, ...] = (
    Rule(
        id=_RULE_ID,
        group=GROUP,
        guard_ids=(_RULE_ID,),
        description="Archive members are read only through the utils/archive.py limits owner.",
        check=_check_archive_extraction_limits,
    ),
)

COLLECTORS: tuple[object, ...] = ()

__all__ = ["COLLECTORS", "RULES"]
