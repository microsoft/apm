"""Installed-CLI proof: the lockfile records content hashes for every git-sourced entry.

A hermetic git world (URL-rewritten local remotes) installs a direct git
dependency, its transitive git dependency, a marketplace entry that resolves
to git, and a user-scope (``-g``) git dependency. Every git-sourced lockfile
entry carries a ``content_hash`` in the ``sha256:<64 hex>`` envelope, and
``audit --ci`` and ``install --frozen`` reach the same verdict on that lock.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import load_yaml
from tests.integration.test_marketplace_tag_pattern_lifecycle import (
    _HOST,
    _MARKETPLACE,
    _OWNER,
    _PACKAGE,
    _declare_range,
    _new_scenario,
    _register_marketplace,
    _run,
    _Scenario,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
    pytest.mark.promise("P-OWN-package-content-hash", "req-lk-015", "req-lk-016", "P-INV-I3"),
]

_ENVELOPE = re.compile(r"sha256:[0-9a-f]{64}")
_SKILL = "---\nname: {name}\ndescription: {name} skill\n---\n# {name}\n"


def _publish(scenario: _Scenario, name: str, *, dependencies=()) -> str:
    package = scenario.packages.create(
        name, version="1.0.0", targets=("copilot",), dependencies=dependencies
    )
    scenario.packages.add_skill(package, name, _SKILL.format(name=name))
    repository = scenario.repositories.create(name, source_tree=package.root)
    commit = scenario.repositories.commit(repository, message=f"seed {name}")
    remote = f"https://{_HOST}/{_OWNER}/{name}"
    scenario.repositories.install_url_rewrite(repository, remote)
    return f"{remote}#{commit.sha}"


def _git_entries_by_kind(lock_path: Path) -> dict[str, dict[str, dict]]:
    """Every non-local lock row, keyed by kind (direct, transitive, marketplace) and name."""
    kinds: dict[str, dict[str, dict]] = {}
    for entry in (load_yaml(lock_path) or {}).get("dependencies") or []:
        if entry.get("source") == "local" or entry.get("local_path"):
            continue
        if entry.get("discovered_via"):
            kind = "marketplace"
        elif entry.get("resolved_by"):
            kind = "transitive"
        else:
            kind = "direct"
        kinds.setdefault(kind, {})[entry["name"]] = entry
    return kinds


def test_every_git_lock_entry_records_a_content_hash(tmp_path: Path, apm_binary_path: Path) -> None:
    scenario = _new_scenario(tmp_path / "hashes", apm_binary_path)
    leaf = _publish(scenario, "hash-leaf")
    parent = _publish(scenario, "hash-parent", dependencies=(leaf,))
    user_pkg = _publish(scenario, "hash-user")

    consumer = scenario.consumers.create("hash-consumer", targets=("copilot",))
    _register_marketplace(scenario, consumer)
    _declare_range(scenario, consumer, "^1.0.0")
    scenario.consumers.replace_apm_dependencies(
        consumer,
        (
            {"name": _PACKAGE, "marketplace": _MARKETPLACE, "version": "^1.0.0"},
            parent,
        ),
    )
    installed = _run(
        scenario,
        consumer.root,
        ("install", "--target", "copilot", "--no-policy", "--parallel-downloads", "0"),
        "hashes-project-install",
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr

    user = _run(
        scenario,
        consumer.root,
        ("install", "-g", user_pkg, "--target", "copilot", "--no-policy"),
        "hashes-user-install",
    )
    assert user.returncode == 0, user.stdout + user.stderr

    by_scope = {
        "project": _git_entries_by_kind(consumer.root / "apm.lock.yaml"),
        "user": _git_entries_by_kind(scenario.isolated.config_root / "apm.lock.yaml"),
    }
    names = {
        scope: {kind: sorted(entries) for kind, entries in kinds.items()}
        for scope, kinds in by_scope.items()
    }
    assert names == {
        "project": {
            "direct": ["hash-parent"],
            "marketplace": [_PACKAGE],
            "transitive": ["hash-leaf"],
        },
        "user": {"direct": ["hash-user"]},
    }, names
    assert by_scope["project"]["marketplace"][_PACKAGE]["discovered_via"] == _MARKETPLACE
    unrecorded = [
        (scope, kind, name, field)
        for scope, kinds in by_scope.items()
        for kind, entries in kinds.items()
        for name, entry in entries.items()
        for field, valid in (
            ("resolved_commit", bool(entry.get("resolved_commit"))),
            ("content_hash", bool(_ENVELOPE.fullmatch(str(entry.get("content_hash") or "")))),
        )
        if not valid
    ]
    assert unrecorded == [], unrecorded

    audit = _run(scenario, consumer.root, ("audit", "--ci", "--no-policy"), "hashes-audit-ci")
    frozen = _run(
        scenario,
        consumer.root,
        ("install", "--frozen", "--target", "copilot", "--no-policy"),
        "hashes-frozen",
    )
    assert (audit.returncode, frozen.returncode) == (0, 0), (
        audit.stdout + audit.stderr + frozen.stdout + frozen.stderr
    )
