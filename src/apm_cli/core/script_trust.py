"""Trust gate for project-source lifecycle scripts.

Policy scripts (``/etc/apm/policy.d``) and user scripts (``~/.apm/scripts``)
originate from sources the developer already controls, so they run
without a gate.  A project ``apm-scripts.yml`` file, however, is
committed into a repository -- cloning an untrusted repo and running
``apm install`` would otherwise execute attacker-controlled shell
commands with no consent.

To close that supply-chain hole, project-source scripts are **skipped**
unless the developer has explicitly trusted the exact contents of the
project script file (direnv / VS Code Workspace Trust model).  Trust is
keyed by the SHA-256 of the script file, so any edit to the committed
scripts re-arms the gate and must be re-trusted.

Trust records live in ``$APM_HOME/scripts-trust.json`` (default
``~/.apm/scripts-trust.json``)::

    {"version": 1, "projects": {"<abs apm-scripts.yml path>": "<sha256>"}}
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
    """Return the SHA-256 hex digest of *path*, or None if unreadable."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as e:
        _logger.debug("Cannot fingerprint script file %s: %s", path, e)
        return None


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
    """Return True only when *script_file* contents are explicitly trusted.

    The stored fingerprint must match the file's current SHA-256, so any
    edit to the committed scripts revokes trust until re-approved.
    """
    fingerprint = script_file_fingerprint(script_file)
    if fingerprint is None:
        return False
    trusted = _load_trust_store().get(str(script_file.resolve()))
    return trusted == fingerprint


def trust_project_scripts(script_file: Path) -> str | None:
    """Record trust for the current contents of *script_file*.

    Returns the recorded fingerprint, or None when the file cannot be
    read (nothing is recorded in that case).
    """
    fingerprint = script_file_fingerprint(script_file)
    if fingerprint is None:
        return None
    projects = _load_trust_store()
    projects[str(script_file.resolve())] = fingerprint
    _write_trust_store(projects)
    return fingerprint


def untrust_project_scripts(script_file: Path) -> bool:
    """Revoke trust for *script_file*. Returns True if a record was removed."""
    key = str(script_file.resolve())
    projects = _load_trust_store()
    if key not in projects:
        return False
    del projects[key]
    _write_trust_store(projects)
    return True
