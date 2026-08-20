"""Shared fixtures for the red-team trust-gate suite.

Every test is hermetic: APM_HOME is redirected to a tmp dir so the real
~/.apm trust store is never touched, the policy directory is redirected to
an empty tmp dir (never /etc/apm/policy.d), and the org-policy chain is
neutered to None by default.  Tests that need org deny_all override the
discover_policy_with_chain patch explicitly.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


@pytest.fixture
def apm_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect APM_HOME (trust store + user apm.yml) to a tmp dir."""
    home = tmp_path / "apm_home"
    home.mkdir()
    monkeypatch.setenv("APM_HOME", str(home))
    monkeypatch.delenv("APM_NO_SCRIPTS", raising=False)
    return home


@pytest.fixture
def hermetic_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Neuter the policy dir and org-policy chain so only project/user load."""
    empty_policy = tmp_path / "no_policy_dir"
    monkeypatch.setattr(
        "apm_cli.core.lifecycle_scripts._get_policy_scripts_dir",
        lambda: empty_policy,
    )
    monkeypatch.setattr(
        "apm_cli.policy.discovery.discover_policy_with_chain",
        lambda *_a, **_k: None,
    )


def sentinel_command(target: Path) -> str:
    """A cross-platform command string that creates target when executed."""
    return f"{sys.executable} -c \"open(r'{target}', 'w').close()\""
