"""Ownership check for archive extraction limits (#4235).

``utils/archive.py`` alone reads archive members. ``safe_extract_tar`` and
``safe_extract_zip`` apply the same limits to every format: an entry-count
cap, an uncompressed-size cap, and refusal of links, special files, absolute
or traversing paths and duplicate or conflicting entries. ``read_zip_member``
is the single in-memory member reader under the same cap.

Closed world, keyed on call shape rather than variable names:

* only an allowlisted module may open an archive for reading, and it must
  hand the opened archive to the owner (no member ``.open(`` / ``.read(``);
* only an allowlisted module may open an archive for writing;
* no module outside the owner calls a tarfile ``extract*`` method.
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

# Modules that open an archive for reading and hand it to the owner.
_READERS = frozenset(
    {
        "src/apm_cli/bundle/local_bundle.py",
        "src/apm_cli/bundle/unpacker.py",
        "src/apm_cli/deps/download_strategies.py",
        _REGISTRY_EXTRACTOR,
    }
)
# Modules that only produce archives.
_WRITERS = frozenset(
    {
        "src/apm_cli/bundle/reproducible_archive.py",
        "src/apm_cli/commands/publish.py",
    }
)
_ARCHIVE_OPEN = re.compile(r"\bZipFile\(|\btarfile\.open\(")
_TAR_EXTRACT = re.compile(r"\.extractfile\(|\.extractall\(|\.extract\(")
_MEMBER_READ = re.compile(r"(?<!LockFile)\.read\(|(?<!tarfile)\.open\(")


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
                ("sub", "claims.claim(", 2, "eq"),
                ("sub", "declared_total > size_limit", 1, "eq"),
                ("sub", "file_type not in _ZIP_ALLOWED_FILE_TYPES", 1, "eq"),
                ("sub", "total_uncompressed = _stream_member(", 1, "eq"),
                ("sub", "_write_planned(plan, ", 2, "eq"),
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
    others = _src_python(provider, exclude={_OWNER})
    findings.extend(
        _forbid_scan(
            provider,
            inventory,
            _RULE_ID,
            tuple(path for path in others if path not in _READERS | _WRITERS),
            _ARCHIVE_OPEN,
            "Only allowlisted modules may open archives; read them through utils/archive.py",
            exempt=True,
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inventory,
            _RULE_ID,
            others,
            _TAR_EXTRACT,
            "Only utils/archive.py may extract archive members",
            exempt=True,
        )
    )
    findings.extend(
        _forbid_scan(
            provider,
            inventory,
            _RULE_ID,
            tuple(sorted(_READERS)),
            _MEMBER_READ,
            "Archive readers hand the archive to utils/archive.py instead of reading members",
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
