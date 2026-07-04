"""Unit tests for the shared pack provenance helper (#2013).

Exercises :func:`apm_cli.bundle.attest.verify_attested_file` in isolation so
the R0801-keystone both pack formats share has direct branch coverage:

* no recorded hash -> tolerated (no raise), debug diagnostic emitted;
* matching hash    -> tolerated (no raise);
* mismatched hash  -> hard ``ValueError`` naming ``apm.lock.yaml``.
"""

import logging
from pathlib import Path

import pytest

from apm_cli.bundle.attest import verify_attested_file
from apm_cli.utils.content_hash import compute_file_hash


def _write(tmp_path: Path, content: str) -> Path:
    target = tmp_path / "deployed.md"
    target.write_text(content, encoding="utf-8")
    return target


def test_verify_attested_file_matching_hash_passes(tmp_path: Path) -> None:
    source = _write(tmp_path, "attested body")
    verify_attested_file(
        source,
        compute_file_hash(source),
        dep_label="acme/bundle",
        rel_display=".github/skills/alpha/SKILL.md",
    )


def test_verify_attested_file_mismatch_raises(tmp_path: Path) -> None:
    source = _write(tmp_path, "tampered body")
    with pytest.raises(ValueError, match=r"does not match the hash recorded in apm\.lock\.yaml"):
        verify_attested_file(
            source,
            "sha256:" + "0" * 64,
            dep_label="acme/bundle",
            rel_display=".github/skills/alpha/SKILL.md",
        )


def test_verify_attested_file_no_hash_is_tolerated(tmp_path: Path, caplog) -> None:
    source = _write(tmp_path, "unverified body")
    with caplog.at_level(logging.DEBUG, logger="apm_cli.bundle.attest"):
        verify_attested_file(
            source,
            None,
            dep_label="acme/bundle",
            rel_display=".github/skills/alpha/SKILL.md",
        )
    assert "without integrity" in caplog.text
