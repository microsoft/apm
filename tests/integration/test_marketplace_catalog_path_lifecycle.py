"""Real-CLI per-audience catalogs from one repository via ``marketplace add --path`` (#3319)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.lifecycle_smoke,
    pytest.mark.requires_apm_binary,
    pytest.mark.requires_e2e_mode,
]

_HOST = "gitlab.example.invalid"
_REPO = "team/agent-skills"
_REMOTE = f"https://{_HOST}/{_REPO}"
_FRONTEND_PATH = "catalogs/frontend/marketplace.json"
_BACKEND_PATH = "catalogs/backend/marketplace.json"


def _skill_document(name: str) -> str:
    return f"---\nname: {name}\ndescription: {name} audience fixture\n---\n# {name}\n"


def _write_catalog(path: Path, name: str, plugin: dict[str, object]) -> None:
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"name": name, "owner": {"name": "APM Test"}, "plugins": [plugin]}),
        encoding="utf-8",
    )


def _registrations(config_root: Path) -> dict[str, dict[str, object]]:
    data = json.loads((config_root / "marketplaces.json").read_text(encoding="utf-8"))
    return {entry["name"]: entry for entry in data["marketplaces"]}


def test_two_catalogs_from_one_repository_register_install_and_refresh(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Each --path registration keeps its own catalog through refresh and install."""
    isolated = IsolatedApmEnvironment.create(
        tmp_path.resolve() / "isolated", base_env=dict(os.environ)
    )
    environment = isolated.subprocess_env()
    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=environment)
    repository = repositories.create("agent-skills")
    packages = LocalPackageFactory(repository.worktree / "plugins")
    for skill in ("frontend-skill", "backend-skill"):
        package = packages.create(skill, targets=("codex",))
        packages.add_skill(package, skill, _skill_document(skill))
    # Relative sources resolve from the repository root, not the catalog folder.
    _write_catalog(
        repository.worktree / _FRONTEND_PATH,
        "acme-frontend",
        {"name": "frontend-skill", "source": "./plugins/frontend-skill"},
    )
    _write_catalog(
        repository.worktree / _BACKEND_PATH,
        "acme-backend",
        {
            "name": "backend-skill",
            "source": {
                "source": "git-subdir",
                "url": _REMOTE,
                "path": "plugins/backend-skill",
                "ref": "main",
            },
        },
    )
    repositories.commit(repository, message="publish per-audience catalogs")
    repositories.install_url_rewrite(repository, _REMOTE)
    (isolated.home / ".gitconfig").write_bytes(Path(environment["GIT_CONFIG_GLOBAL"]).read_bytes())
    environment = repositories.url_rewrite_subprocess_env(repository, _REMOTE)
    consumer = LocalPackageFactory(isolated.work_root).create("consumer", targets=("codex",))
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=120)

    # Escaping or missing paths fail before anything is registered; without
    # --path the default discovery order is unchanged and finds nothing here.
    failures = runner.run_sequence(
        (
            ("marketplace", "add", _REMOTE, "--ref", "main", "--path", "../outside.json"),
            (
                "marketplace",
                "add",
                _REMOTE,
                "--ref",
                "main",
                "--path",
                "catalogs/x/marketplace.json",
            ),
            ("marketplace", "add", _REMOTE, "--ref", "main", "--name", "acme"),
        ),
        expected_returncodes=(1, 1, 1),
        scenario_id="catalog-path-rejections",
        cwd=consumer.root,
        env=environment,
    )
    outputs = [" ".join((result.stdout + result.stderr).split()) for result in failures]
    assert "Invalid --path '../outside.json'" in outputs[0]
    assert "No marketplace catalog found at 'catalogs/x/marketplace.json'" in outputs[1]
    assert "Checked: marketplace.json, .github/plugin/marketplace.json" in outputs[2]
    marketplaces_file = isolated.config_root / "marketplaces.json"
    assert not marketplaces_file.exists() or _registrations(isolated.config_root) == {}

    install_args = ("--target", "codex", "--no-policy", "--parallel-downloads", "0")
    runner.run_sequence(
        (
            ("marketplace", "add", _REMOTE, "--ref", "main", "--path", _FRONTEND_PATH),
            ("marketplace", "add", _REMOTE, "--ref", "main", "--path", _BACKEND_PATH),
            ("marketplace", "update"),
            ("marketplace", "update", "acme-backend"),
            ("install", "frontend-skill@acme-frontend", *install_args),
            ("install", "backend-skill@acme-backend", *install_args),
        ),
        expected_returncodes=(0, 0, 0, 0, 0, 0),
        scenario_id="catalog-path-register-refresh-install",
        cwd=consumer.root,
        env=environment,
    )

    registered = _registrations(isolated.config_root)
    assert registered["acme-frontend"]["path"] == _FRONTEND_PATH
    assert registered["acme-backend"]["path"] == _BACKEND_PATH
    skills_root = consumer.root / ".agents" / "skills"
    for skill in ("frontend-skill", "backend-skill"):
        deployed = skills_root / skill / "SKILL.md"
        assert deployed.read_text(encoding="utf-8") == _skill_document(skill)

    # Each catalog only lists its own audience's package.
    browse = runner.run_sequence(
        (
            ("marketplace", "browse", "acme-frontend"),
            ("install", "backend-skill@acme-frontend", *install_args),
        ),
        expected_returncodes=(0, 1),
        scenario_id="catalog-path-isolation",
        cwd=consumer.root,
        env=environment,
    )
    assert "frontend-skill" in browse[0].stdout
    assert "backend-skill" not in browse[0].stdout
