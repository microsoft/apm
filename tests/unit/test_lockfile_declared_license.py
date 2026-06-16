"""Tests for the ``declared_license`` field on LockedDependency (U6).

``declared_license`` records the SPDX expression that a dependency's manifest
*declared* at resolve time. It is a provenance passthrough -- APM never reads
the LICENSE file or concludes a license. Absence is meaningful (unknown), so
the field is OMITTED from the serialized lockfile entry when not declared; it
is never stored as a sentinel.
"""

from __future__ import annotations

from apm_cli.deps.lockfile import LockedDependency


def test_declared_license_defaults_to_none():
    dep = LockedDependency(repo_url="owner/repo")
    assert dep.declared_license is None


def test_declared_license_roundtrips_through_to_dict_from_dict():
    dep = LockedDependency(repo_url="owner/repo", declared_license="MIT")
    data = dep.to_dict()
    assert data["declared_license"] == "MIT"
    restored = LockedDependency.from_dict(data)
    assert restored.declared_license == "MIT"


def test_declared_license_omitted_when_absent():
    dep = LockedDependency(repo_url="owner/repo")
    data = dep.to_dict()
    assert "declared_license" not in data


def test_declared_license_not_treated_as_unknown_forward_field():
    # A future reader must keep declared_license as a first-class known key,
    # not capture it into _unknown_fields (which would re-emit it blindly).
    dep = LockedDependency.from_dict({"repo_url": "owner/repo", "declared_license": "Apache-2.0"})
    assert dep.declared_license == "Apache-2.0"
    assert "declared_license" not in dep._unknown_fields


def test_declared_license_special_token_preserved_verbatim():
    dep = LockedDependency(repo_url="owner/repo", declared_license="UNLICENSED")
    assert LockedDependency.from_dict(dep.to_dict()).declared_license == "UNLICENSED"
