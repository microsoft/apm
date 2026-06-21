"""Tests for the ``exec_status`` (trust_state) field on LockedDependency.

``exec_status`` records the resolved executable-trust state of a package
(issue #1873): one of ``deployed`` | ``gated_pending_approval`` | ``denied``
| ``absent``. Absence on disk is meaningful (the package never declared an
executable), so the field is OMITTED from the serialized entry when unset.
"""

from __future__ import annotations

import pytest

from apm_cli.deps.lockfile import LockedDependency


def test_exec_status_defaults_to_none():
    dep = LockedDependency(repo_url="owner/repo")
    assert dep.exec_status is None


def test_exec_status_roundtrips():
    dep = LockedDependency(repo_url="owner/repo", exec_status="gated_pending_approval")
    data = dep.to_dict()
    assert data["exec_status"] == "gated_pending_approval"
    restored = LockedDependency.from_dict(data)
    assert restored.exec_status == "gated_pending_approval"


def test_exec_status_omitted_when_absent():
    dep = LockedDependency(repo_url="owner/repo")
    assert "exec_status" not in dep.to_dict()


def test_exec_status_not_treated_as_unknown_forward_field():
    dep = LockedDependency.from_dict({"repo_url": "owner/repo", "exec_status": "deployed"})
    assert dep.exec_status == "deployed"
    assert "exec_status" not in dep._unknown_fields


def test_invalid_exec_status_rejected():
    with pytest.raises(ValueError, match="Unsupported lockfile exec_status"):
        LockedDependency.from_dict({"repo_url": "owner/repo", "exec_status": "forged"})
