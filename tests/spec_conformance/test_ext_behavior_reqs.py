"""Behavioral receipts for OpenAPM vendor-extension keys (req-ext-001).

The manifest and policy clauses already hold and are pinned by unmarked
tests here; req-ext-001 reports the strict xfails until every clause holds.
The lockfile clause lives in test_lockfile_behavior_reqs.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apm_cli.models.apm_package import APMPackage
from apm_cli.policy.parser import load_policy

pytestmark = [pytest.mark.component, pytest.mark.promise("req-ext-001")]

_BASE = {"name": "p", "version": "1.0.0"}

_POLICY_PLAIN = """\
name: org-policy
version: 1.0.0
enforcement: block
dependencies:
  deny:
    - evil/*
mcp:
  transport:
    allow: [stdio]
"""

_POLICY_EXTENDED = """\
name: org-policy
version: 1.0.0
x-acme-owner: security-team
enforcement: block
dependencies:
  x-acme-note: reviewed
  deny:
    - evil/*
mcp:
  x-acme-ticket: SEC-1
  transport:
    allow: [stdio]
"""


def _load(tmp_path: Path, data: dict) -> APMPackage:
    return APMPackage.from_mapping(data, package_path=tmp_path, create_config=False)


def test_policy_vendor_keys_are_ignored_without_errors(tmp_path: Path) -> None:
    """x- keys at the top level and in sub-blocks change nothing the policy enforces."""
    plain, _ = load_policy(_POLICY_PLAIN)
    extended, warnings = load_policy(_POLICY_EXTENDED)
    assert extended == plain
    assert all("dependencies" not in w and "mcp" not in w for w in warnings)


@pytest.mark.req("req-pl-009")
def test_policy_vendor_keys_do_not_warn_but_unknown_keys_do() -> None:
    """x- top-level policy keys never warn; other unknown keys still do; a deny holds."""
    policy, warnings = load_policy(_POLICY_EXTENDED + "x-: bare\nbogus-key: 1\n")
    assert warnings == [
        "Unknown top-level policy key: 'bogus-key'",
        "Unknown top-level policy key: 'x-'",
    ]
    assert policy.dependencies.deny == ("evil/*",)
    assert policy.enforcement == "block"


def test_manifest_top_level_and_mcp_vendor_keys_parse_without_effect(tmp_path: Path) -> None:
    """Top-level and dependencies.mcp[] x- keys parse and leave semantics unchanged."""
    server = {"name": "io.github.acme/srv", "registry": False, "transport": "stdio"}
    plain = _load(tmp_path, {**_BASE, "dependencies": {"mcp": [{**server, "command": "srv"}]}})
    extended = _load(
        tmp_path,
        {
            **_BASE,
            "x-acme-build": {"lane": "ci"},
            "dependencies": {"mcp": [{**server, "command": "srv", "x-acme-tier": 1}]},
        },
    )
    assert extended.extensions == {"x-acme-build": {"lane": "ci"}}
    (plain_mcp,) = plain.get_mcp_dependencies()
    (extended_mcp,) = extended.get_mcp_dependencies()
    assert (extended_mcp.name, extended_mcp.transport, extended_mcp.command) == (
        plain_mcp.name,
        plain_mcp.transport,
        plain_mcp.command,
    )


@pytest.mark.req("req-ext-001")
@pytest.mark.parametrize(
    "entry",
    [
        {"git": "https://github.com/acme/x", "ref": "v1"},
        {"path": "./vendor/a"},
    ],
    ids=["git", "path"],
)
def test_apm_dependency_entry_vendor_keys_are_ignored(tmp_path: Path, entry: dict) -> None:
    """An x- key inside a dependencies.apm[] mapping neither errors nor changes the entry.

    A non-x- unknown key is still refused (#4260).
    """
    (plain,) = _load(tmp_path, {**_BASE, "dependencies": {"apm": [entry]}}).get_apm_dependencies()
    extended_entry = {**entry, "x-acme-pin": "keep"}
    (extended,) = _load(
        tmp_path, {**_BASE, "dependencies": {"apm": [extended_entry]}}
    ).get_apm_dependencies()
    assert extended.to_canonical() == plain.to_canonical()
    assert extended.reference == plain.reference
    with pytest.raises(ValueError, match="bogus-pin"):
        _load(tmp_path, {**_BASE, "dependencies": {"apm": [{**entry, "bogus-pin": "no"}]}})
