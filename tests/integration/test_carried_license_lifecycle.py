"""Installed-CLI contract: the source licence travels with what APM installs (#3556).

A dependency that is a repository subdirectory, or a whole repository whose
skills sit below its root, must leave the repository's licence file,
byte-identical, in ``apm_modules`` and next to each deployed skill; the
lockfile records each copy's source path and hash; ``apm audit`` reports a
missing or modified copy; ``apm uninstall`` removes the copies.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from apm_cli.deps.carried_license import is_license_file_name
from apm_cli.utils.content_hash import compute_file_hash, compute_package_hash
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory

pytestmark = [pytest.mark.e2e, pytest.mark.integration, pytest.mark.requires_apm_binary]

_SUBDIR_REMOTE = "https://github.com/apm-fixture-org/licensed-skills.git"
_WHOLE_REMOTE = "https://github.com/apm-fixture-org/licensed-bundle.git"
_PLAIN_REMOTE = "https://github.com/apm-fixture-org/unlicensed-bundle.git"
_SUBDIR = "skills/productivity/grilling"
_PLUGIN = "plugins/tool"
# A relative link a skill-markdown renderer would rewrite: must deploy byte-identical.
_LICENSE_MD = b"Copyright \xc2\xa9 2026 Fixture. See [terms](./TERMS.md).\n"
_MIT = b"MIT License\n\nCopyright (c) 2026 Fixture Author\n\nPermission is hereby granted.\n"
_AUDIT_ARGS = ("audit", "--ci", "--no-policy", "--format", "json")


def _skill(directory: Path, name: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Licence carry fixture\n---\n# {name}\n",
        encoding="utf-8",
    )


def _ok(result: CommandResult) -> str:
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    return output


def _locked(project: Path) -> dict[str, dict]:
    lock = load_yaml(project / "apm.lock.yaml")
    return {(entry["repo_url"], entry.get("virtual_path")): entry for entry in lock["dependencies"]}


def _audit(runner: ApmLifecycleRunner, project: Path, env: dict[str, str]) -> dict:
    result = runner.run(_AUDIT_ARGS, cwd=project, env=env)
    return json.loads(result.stdout)


def _pre_feature_hash(package: Path) -> str:
    """Hash the package as APM materialized it before #3556: no licence copies."""
    tree = package.parent / f".pre-feature-{package.name}"
    shutil.copytree(package, tree)
    try:
        for path in list(tree.rglob("*")):
            if path.is_file() and (is_license_file_name(path.name) or path.name == ".apm-pin"):
                path.unlink()
        return compute_package_hash(tree)
    finally:
        shutil.rmtree(tree)


def _assert_audit_passes(runner: ApmLifecycleRunner, project: Path, env: dict[str, str]) -> None:
    payload = _audit(runner, project, env)
    assert payload["passed"] is True, _failed_details(payload)


def _failed_details(payload: dict) -> str:
    return json.dumps([check for check in payload["checks"] if not check["passed"]])


def _scenario(
    tmp_path: Path, apm_binary_path: Path
) -> tuple[Path, dict[str, str], ApmLifecycleRunner]:
    isolated = IsolatedApmEnvironment.create(tmp_path / "licence", base_env=dict(os.environ))
    env = isolated.subprocess_env()
    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=env)

    subdir_source = isolated.package_root / "licensed-skills"
    subdir_source.mkdir(parents=True)
    (subdir_source / "LICENSE").write_bytes(_MIT)
    (subdir_source / "README.md").write_text("monorepo\n", encoding="utf-8")
    _skill(subdir_source / _SUBDIR, "grilling")
    (subdir_source / _SUBDIR / "agents").mkdir()
    (subdir_source / _SUBDIR / "agents" / "openai.yaml").write_text("x: 1\n", encoding="utf-8")
    plugin = subdir_source / _PLUGIN
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "tool", "version": "1.0.0", "description": "Fixture"}),
        encoding="utf-8",
    )
    _skill(plugin / "skills" / "helper", "helper")
    subdir_repo = repositories.create("licensed-skills", source_tree=subdir_source)
    subdir_pin = repositories.commit(subdir_repo, message="licensed monorepo")

    whole_source = isolated.package_root / "licensed-bundle"
    whole_source.mkdir(parents=True)
    (whole_source / "LICENSE.md").write_bytes(_LICENSE_MD)
    _skill(whole_source / "skills" / "alpha", "alpha")
    whole_repo = repositories.create("licensed-bundle", source_tree=whole_source)
    whole_pin = repositories.commit(whole_repo, message="licensed bundle")

    plain_source = isolated.package_root / "unlicensed-bundle"
    plain_source.mkdir(parents=True)
    _skill(plain_source / "skills" / "beta", "beta")
    plain_repo = repositories.create("unlicensed-bundle", source_tree=plain_source)
    plain_pin = repositories.commit(plain_repo, message="unlicensed bundle")

    env = repositories.url_rewrite_subprocess_env_many(
        (
            (subdir_repo, _SUBDIR_REMOTE),
            (whole_repo, _WHOLE_REMOTE),
            (plain_repo, _PLAIN_REMOTE),
        )
    )
    project = isolated.work_root / "consumer"
    project.mkdir()
    dump_yaml(
        {
            "name": "consumer",
            "version": "1.0.0",
            "targets": ["copilot"],
            "dependencies": {
                "apm": [
                    {"git": _SUBDIR_REMOTE, "path": _SUBDIR, "ref": subdir_pin.sha},
                    {"git": _SUBDIR_REMOTE, "path": _PLUGIN, "ref": subdir_pin.sha},
                    {"git": _WHOLE_REMOTE, "ref": whole_pin.sha},
                    {"git": _PLAIN_REMOTE, "ref": plain_pin.sha},
                ]
            },
        },
        project / "apm.yml",
    )
    return project, env, ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=180)


def test_licence_is_carried_recorded_audited_and_uninstalled(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    project, env, runner = _scenario(tmp_path, apm_binary_path)
    install = ("install", "--no-policy")
    _ok(runner.run(install, cwd=project, env=env))

    grilling_cache = next(
        path for path in (project / "apm_modules").rglob("grilling") if path.is_dir()
    )
    deployed_grilling = project / ".agents" / "skills" / "grilling"
    deployed_alpha = project / ".agents" / "skills" / "alpha"
    deployed_beta = project / ".agents" / "skills" / "beta"
    assert (grilling_cache / "LICENSE").read_bytes() == _MIT
    assert (deployed_grilling / "LICENSE").read_bytes() == _MIT
    assert (deployed_grilling / "SKILL.md").is_file()
    assert (deployed_alpha / "LICENSE.md").read_bytes() == _LICENSE_MD
    assert (project / ".agents" / "skills" / "helper" / "LICENSE").read_bytes() == _MIT
    assert (deployed_beta / "SKILL.md").is_file()
    assert not (deployed_beta / "LICENSE").exists()

    licence_hash = compute_file_hash(grilling_cache / "LICENSE")
    entries = _locked(project)
    subdir_entry = next(entry for key, entry in entries.items() if key[1] == _SUBDIR)
    assert subdir_entry["carried_licenses"] == [
        {"path": "LICENSE", "source": "LICENSE", "hash": licence_hash}
    ]
    assert ".agents/skills/grilling/LICENSE" in subdir_entry["deployed_files"]
    # content_hash keeps its pre-#3556 value, so older APM clients still verify.
    assert subdir_entry["content_hash"] == _pre_feature_hash(grilling_cache)
    whole_entry = next(
        entry for key, entry in entries.items() if "licensed-bundle" in key[0] and not key[1]
    )
    whole_cache = next(p for p in (project / "apm_modules").rglob("licensed-bundle") if p.is_dir())
    assert whole_entry["carried_licenses"] == [
        {
            "path": "skills/alpha/LICENSE.md",
            "source": "LICENSE.md",
            "hash": compute_file_hash(whole_cache / "LICENSE.md"),
        }
    ]
    plugin_entry = next(entry for key, entry in entries.items() if key[1] == _PLUGIN)
    assert {record["source"] for record in plugin_entry["carried_licenses"]} == {"LICENSE"}
    # Plugin normalization must not duplicate an unrecorded licence copy.
    plugin_cache = next(p for p in (project / "apm_modules").rglob("tool") if p.is_dir())
    assert plugin_entry["content_hash"] == _pre_feature_hash(plugin_cache)
    plain_entry = next(entry for key, entry in entries.items() if "unlicensed" in key[0])
    assert "carried_licenses" not in plain_entry

    # A warm reinstall keeps the same lockfile and carried copies.
    lock_bytes = (project / "apm.lock.yaml").read_bytes()
    _ok(runner.run(install, cwd=project, env=env))
    assert (project / "apm.lock.yaml").read_bytes() == lock_bytes
    assert (deployed_grilling / "LICENSE").read_bytes() == _MIT

    # A cold reinstall against the lockfile verifies and re-carries.
    shutil.rmtree(project / "apm_modules")
    _ok(runner.run(install, cwd=project, env=env))
    assert (project / "apm.lock.yaml").read_bytes() == lock_bytes
    assert (grilling_cache / "LICENSE").read_bytes() == _MIT

    _assert_audit_passes(runner, project, env)

    # A pre-#3556 lockfile (no carried_licenses) keeps verifying: the carried
    # copy is excluded from content_hash, so a cold install re-records it.
    legacy = load_yaml(project / "apm.lock.yaml")
    for entry in legacy["dependencies"]:
        entry.pop("carried_licenses", None)
    dump_yaml(legacy, project / "apm.lock.yaml")
    shutil.rmtree(project / "apm_modules")
    _ok(runner.run(install, cwd=project, env=env))
    assert (project / "apm.lock.yaml").read_bytes() == lock_bytes

    # A lock whose carried record disagrees with the fetched licence fails closed.
    tampered = load_yaml(project / "apm.lock.yaml")
    for entry in tampered["dependencies"]:
        for record in entry.get("carried_licenses", ()):
            record["hash"] = "sha256:" + "0" * 64
    dump_yaml(tampered, project / "apm.lock.yaml")
    shutil.rmtree(project / "apm_modules")
    rejected = runner.run(install, cwd=project, env=env)
    assert rejected.returncode != 0
    assert "Licence file mismatch" in " ".join((rejected.stdout + rejected.stderr).split())
    (project / "apm.lock.yaml").write_bytes(lock_bytes)
    _ok(runner.run(install, cwd=project, env=env))
    _assert_audit_passes(runner, project, env)

    (deployed_grilling / "LICENSE").write_bytes(_MIT + b"edited\n")
    modified = _audit(runner, project, env)
    assert modified["passed"] is False
    assert ".agents/skills/grilling/LICENSE" in _failed_details(modified)

    (deployed_grilling / "LICENSE").unlink()
    missing = _audit(runner, project, env)
    assert missing["passed"] is False
    assert ".agents/skills/grilling/LICENSE" in _failed_details(missing)

    _ok(runner.run(install, cwd=project, env=env))
    assert (deployed_grilling / "LICENSE").read_bytes() == _MIT

    _ok(
        runner.run(
            ("uninstall", f"apm-fixture-org/licensed-skills/{_SUBDIR}"), cwd=project, env=env
        )
    )
    assert not deployed_grilling.exists()
    assert not grilling_cache.exists()
    assert (deployed_alpha / "LICENSE.md").read_bytes() == _LICENSE_MD
