"""req-lk-012 binds ``cowork://`` deployed-file rows like project rows (#3701).

Every deployed file carries a canonical ``sha256`` envelope in
``deployed_file_hashes``. ``cowork://`` rows previously recorded none, so
their cleanup had no recorded provenance at all.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from apm_cli.install.phases.lockfile import compute_deployed_hashes


@pytest.mark.req("req-lk-012")
def test_cowork_deployed_file_hash_is_canonical_sha256_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cowork = tmp_path / "onedrive"
    text = cowork / "demo" / "SKILL.md"
    text.parent.mkdir(parents=True)
    text.write_bytes(b"line one\r\nline two\n")
    monkeypatch.setenv("APM_COPILOT_COWORK_SKILLS_DIR", str(cowork))

    hashes = compute_deployed_hashes(
        ["cowork://skills/demo", "cowork://skills/demo/SKILL.md"], tmp_path / "project"
    )

    canonical = hashlib.sha256(b"line one\nline two\n").hexdigest()
    assert hashes == {"cowork://skills/demo/SKILL.md": f"sha256:{canonical}"}
