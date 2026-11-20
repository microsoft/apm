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


def _git_entries_without_valid_hash(lock_path: Path) -> dict[str, list[str]]:
    lock = load_yaml(lock_path) or {}
    missing: dict[str, list[str]] = {}
    for entry in lock.get("dependencies") or []:
        if entry.get("source") == "local" or entry.get("local_path"):
            continue
        if not entry.get("resolved_commit"):
            continue
        if not _ENVELOPE.fullmatch(str(entry.get("content_hash") or "")):
            kind = "transitive" if entry.get("resolved_by") else "direct"
            if entry.get("marketplace_plugin") or entry.get("marketplace"):
                kind = "marketplace"
            missing.setdefault(kind, []).append(str(entry.get("repo_url")))
    return missing


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

    project_lock = load_yaml(consumer.root / "apm.lock.yaml")
    kinds = {
        "direct": [e for e in project_lock["dependencies"] if not e.get("resolved_by")],
        "transitive": [e for e in project_lock["dependencies"] if e.get("resolved_by")],
    }
    assert kinds["direct"] and kinds["transitive"], project_lock

    user = _run(
        scenario,
        consumer.root,
        ("install", "-g", user_pkg, "--target", "copilot", "--no-policy"),
        "hashes-user-install",
    )
    assert user.returncode == 0, user.stdout + user.stderr

    missing = {
        "project": _git_entries_without_valid_hash(consumer.root / "apm.lock.yaml"),
        "user": _git_entries_without_valid_hash(scenario.isolated.config_root / "apm.lock.yaml"),
    }
    assert missing == {"project": {}, "user": {}}, missing

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
