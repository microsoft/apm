"""Credential-file refusal for ``apm pack`` (OpenAPM req-sc-007).

The one owner of the credential-file pattern set. Every bundle format and the
``apm publish`` auto-pack call ``refuse_credential_files`` on their final file
list before anything is written, dry runs included, so a package never carries
a matching file.
"""

from __future__ import annotations

from collections.abc import Iterable
from fnmatch import fnmatchcase
from pathlib import PurePosixPath

CREDENTIAL_FILE_PATTERNS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "id_rsa",
    "id_ed25519",
)


def is_credential_file(path: str) -> bool:
    """Return True when the file name of *path* matches a credential pattern."""
    name = PurePosixPath(path.replace("\\", "/")).name.lower()
    return any(fnmatchcase(name, pattern) for pattern in CREDENTIAL_FILE_PATTERNS)


def refuse_credential_files(paths: Iterable[str]) -> None:
    """Raise ``ValueError`` naming every path that matches a credential pattern."""
    matches = sorted({p.replace("\\", "/") for p in paths if is_credential_file(p)})
    if not matches:
        return
    patterns = ", ".join(CREDENTIAL_FILE_PATTERNS)
    listing = "\n".join(f"  - {m}" for m in matches)
    raise ValueError(
        f"Refusing to pack {len(matches)} credential file(s) matching ({patterns}):\n"
        f"{listing}\n"
        "Remove these files from the package sources, then try again."
    )
