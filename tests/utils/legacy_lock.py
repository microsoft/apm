"""Rewrite a lockfile as a pre-0.37 APM left it, for legacy-render migration tests.

A pre-0.37 lock is the evidence that APM wrote a file's current bytes
(``core/legacy_lock_evidence.py``): such an APM recorded the hash of what it
deployed and kept no receipts. Tests that seed a file as an older APM
rendered it record that evidence here instead of hand-editing a file APM
would treat as a user edit (#4626).
"""

from __future__ import annotations

from pathlib import Path

import yaml

from apm_cli.utils.content_hash import compute_file_hash
from apm_cli.utils.yaml_io import dump_yaml


def record_as_pre_037_lock(lock_path: Path, deploy_root: Path, *, version: str = "0.35.0") -> None:
    """Stamp *lock_path* with an older APM *version* and the on-disk hash of every claim."""
    lock = yaml.safe_load(lock_path.read_text(encoding="utf-8"))
    lock["apm_version"] = version
    for dependency in lock.get("dependencies", []):
        hashes = dependency.get("deployed_file_hashes") or {}
        for claim in hashes:
            path = Path(claim) if Path(claim).is_absolute() else deploy_root / claim
            if path.is_file():
                hashes[claim] = compute_file_hash(path)
    dump_yaml(lock, lock_path)
