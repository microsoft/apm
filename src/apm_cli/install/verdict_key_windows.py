"""Windows half of the verdict key's privacy check (QL D1 Windows ruling, #4977).

The key file must be owned by the current user's SID, must not be a reparse
point, and its DACL (explicit and inherited ACEs) may allow access only to
that SID, LocalSystem and BUILTIN\\Administrators. Any API failure is "not
private". Creation strips inheritance and grants only the current user.
"""

from __future__ import annotations

from pathlib import Path


def private_to_current_user(path: Path) -> bool:  # pragma: no cover - Windows only
    """B2 implements the owner/DACL check; until then no key is private on Windows."""
    return False


def restrict_to_current_user(path: Path) -> None:  # pragma: no cover - Windows only
    """B2 implements the DACL restriction."""
    return None
