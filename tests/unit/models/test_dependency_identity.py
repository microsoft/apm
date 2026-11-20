"""Owner tests for dependency identity keys (dependency-identity-materialization).

Table-driven at ``apm_cli.models.dependency.identity`` so lockfile/dedup key
derivation is defended by exact expected keys (see tests/mutation/floors.json).
"""

from __future__ import annotations

import pytest

from apm_cli.models.dependency.identity import (
    build_canonical_dependency_string,
    build_dependency_unique_key,
    normalize_package_repo_url,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _default_github_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_HOST", raising=False)


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"host": "github.com"}, "owner/repo"),
        ({"host": "gitlab.example.com"}, "Owner/Repo"),
        ({"host": "github.com", "source": "registry"}, "owner/repo"),
        ({"host": "gitlab.example.com", "registry_prefix": "proxy"}, "owner/repo"),
        ({"host": "github.com", "is_local": True}, "Owner/Repo"),
        ({"host": "github.com", "source": "local"}, "Owner/Repo"),
        ({"host": "github.com", "is_marketplace": True}, "Owner/Repo"),
    ],
)
def test_normalize_package_repo_url_lowercases_only_case_insensitive_identities(
    kwargs: dict, expected: str
) -> None:
    assert normalize_package_repo_url("Owner/Repo", **kwargs) == expected


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({}, "owner/repo"),
        ({"host": "github.com"}, "owner/repo"),
        ({"host": "GitHub.com"}, "owner/repo"),
        ({"host": "github.com", "port": 8443}, "github.com:8443/owner/repo"),
        ({"port": 8443}, "github.com:8443/owner/repo"),
        ({"host": "GHE.Example.com"}, "ghe.example.com/Owner/Repo"),
        ({"host": "ghe.example.com", "port": 7999}, "ghe.example.com:7999/Owner/Repo"),
        ({"host": "ghe.example.com", "registry_prefix": "art"}, "owner/repo"),
        (
            {"host": "github.com", "is_virtual": True, "virtual_path": "skills/A"},
            "owner/repo/skills/A",
        ),
        ({"host": "github.com", "is_virtual": True, "virtual_path": None}, "owner/repo"),
        ({"host": "github.com", "is_virtual": False, "virtual_path": "x"}, "owner/repo"),
        ({"source": "local", "local_path": "./pkgs/A"}, "./pkgs/A"),
        (
            {"source": "local", "local_path": "./A", "anchored_local_path": "/abs/A"},
            "local:/abs/A",
        ),
        ({"source": "local", "local_path": None}, "Owner/Repo"),
        ({"host": "github.com", "is_marketplace": True}, "Owner/Repo"),
    ],
)
def test_build_dependency_unique_key_table(kwargs: dict, expected: str) -> None:
    assert build_dependency_unique_key("Owner/Repo", **kwargs) == expected


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({}, "Owner/Repo"),
        ({"is_local": True, "local_path": "./A"}, "./A"),
        ({"is_local": True, "local_path": None}, "Owner/Repo"),
        ({"is_local": False, "local_path": "./A"}, "Owner/Repo"),
        ({"is_virtual": True, "virtual_path": "skills/x"}, "Owner/Repo/skills/x"),
        ({"is_virtual": True, "virtual_path": None}, "Owner/Repo"),
        ({"is_virtual": False, "virtual_path": "skills/x"}, "Owner/Repo"),
    ],
)
def test_build_canonical_dependency_string_is_host_blind(kwargs: dict, expected: str) -> None:
    assert build_canonical_dependency_string("Owner/Repo", **kwargs) == expected
