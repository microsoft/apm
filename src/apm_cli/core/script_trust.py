"""Trust gate for project-source lifecycle scripts.

Policy scripts (/etc/apm/policy.d) and user scripts (~/.apm/apm.yml)
originate from sources the developer already controls, so they run
without a gate.  The project apm.yml lifecycle: block, however, is
committed into a repository -- cloning an untrusted repo and running
apm install would otherwise execute attacker-controlled shell
commands with no consent.

To close that supply-chain hole, project-source scripts are skipped
unless the developer has explicitly trusted the exact lifecycle: subtree
of apm.yml (direnv / VS Code Workspace Trust model).  Trust is keyed
by the SHA-256 of the canonical JSON-serialised lifecycle: subtree, so
editing dependencies: or any other apm.yml key does NOT revoke trust,
but editing lifecycle: DOES.

Trust records live in $APM_HOME/scripts-trust.json (default
~/.apm/scripts-trust.json)::

    {"version": 1, "projects": {"<abs apm.yml path>": "<sha256>"}}
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path

_logger = logging.getLogger(__name__)

TRUST_FILE_VERSION = 1


def _trust_store_path() -> Path:
    """Return the path to the script trust store."""
    apm_home = os.environ.get("APM_HOME")
    base = Path(apm_home) if apm_home else Path.home() / ".apm"
    return base / "scripts-trust.json"


def script_file_fingerprint(path: Path) -> str | None:
    """Return the SHA-256 hex digest of the lifecycle: subtree of apm.yml.

    Returns None if the file is unreadable, has no lifecycle: key, or
    the lifecycle: value is falsy/empty.
    """
    from apm_cli.utils.yaml_io import load_yaml

    try:
        data = load_yaml(path)
    except Exception as e:
        _logger.debug("Cannot fingerprint apm.yml lifecycle %s: %s", path, e)
        return None

    lifecycle = (data or {}).get("lifecycle")
    if not lifecycle:
        return None

    canonical = json.dumps(lifecycle, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _load_trust_store() -> dict[str, str]:
    """Load the trusted-projects map (abs path -> sha256)."""
    store = _trust_store_path()
    try:
        data = json.loads(store.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    projects = data.get("projects")
    if not isinstance(projects, dict):
        return {}
    return {str(k): str(v) for k, v in projects.items() if isinstance(v, str)}


def _write_trust_store(projects: dict[str, str]) -> None:
    """Persist the trusted-projects map, creating parent dirs as needed."""
    store = _trust_store_path()
    store.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": TRUST_FILE_VERSION, "projects": projects}
    store.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def is_project_scripts_trusted(script_file: Path) -> bool:
    """Return True only when the lifecycle: subtree of script_file is trusted.

    The stored fingerprint must match the current lifecycle: subtree SHA-256,
    so any edit to lifecycle: revokes trust until re-approved.
    Editing other keys (e.g. dependencies:) does not affect trust.
    """
    fingerprint = script_file_fingerprint(script_file)
    if fingerprint is None:
        return False
    trusted = _load_trust_store().get(str(script_file.resolve()))
    return trusted == fingerprint


def trust_project_scripts(script_file: Path) -> str | None:
    """Record trust for the current lifecycle: subtree of script_file.

    Returns the recorded fingerprint, or None when the file cannot be
    read or has no lifecycle: key (nothing is recorded in that case).
    """
    fingerprint = script_file_fingerprint(script_file)
    if fingerprint is None:
        return None
    projects = _load_trust_store()
    projects[str(script_file.resolve())] = fingerprint
    _write_trust_store(projects)
    return fingerprint


def untrust_project_scripts(script_file: Path) -> bool:
    """Revoke trust for script_file. Returns True if a record was removed."""
    key = str(script_file.resolve())
    projects = _load_trust_store()
    if key not in projects:
        return False
    del projects[key]
    _write_trust_store(projects)
    return True
