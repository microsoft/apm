"""apm pack refuses credential files in every bundle format (#4323, req-sc-007)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

from apm_cli.deps.lockfile import LockedDependency, LockFile

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.promise("req-sc-007", "P-OWN-pack-credential-files"),
]

_CREDENTIAL_NAMES = (".env", ".env.local", "server.pem", "signing.key", "id_rsa", "id_ed25519")
_PKG = "creds-pack"


def _run_pack(apm: Path, project: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    for name in ("GITHUB_TOKEN", "GITHUB_APM_PAT", "ADO_APM_PAT"):
        env.pop(name, None)
    home = project / ".home"
    home.mkdir(exist_ok=True)
    env.update({"HOME": str(home), "PYTHONUTF8": "1"})
    return subprocess.run(
        [str(apm), "pack", *args],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _plugin_project(root: Path, with_credentials: bool) -> tuple[Path, str]:
    root.mkdir()
    (root / "apm.yml").write_text(
        yaml.safe_dump(
            {
                "name": _PKG,
                "version": "1.0.0",
                "description": "Credential file pack fixture",
                "target": "copilot",
                "dependencies": {"apm": [], "mcp": []},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    skill = root / ".apm" / "skills" / "creds"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: creds\ndescription: Credential fixture\n---\n# Creds\n", encoding="utf-8"
    )
    if with_credentials:
        for name in _CREDENTIAL_NAMES:
            (skill / name).write_text("SECRET-FIXTURE\n", encoding="utf-8")
    (root / "apm.lock.yaml").write_text("dependencies: []\n", encoding="utf-8")
    return root, "skills/creds"


def _apm_format_project(root: Path, with_credentials: bool) -> tuple[Path, str]:
    root.mkdir()
    (root / "apm.yml").write_text(
        yaml.safe_dump(
            {
                "name": _PKG,
                "version": "1.0.0",
                "description": "Credential file pack fixture",
                "target": "copilot",
                "dependencies": {"apm": [{"git": "acme/creds", "ref": "abc123"}], "mcp": []},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (root / ".github").mkdir()
    (root / ".github" / "copilot-instructions.md").write_text("# fixture\n", encoding="utf-8")
    skill = root / ".github" / "skills" / "creds"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Creds\n", encoding="utf-8")
    if with_credentials:
        for name in _CREDENTIAL_NAMES:
            (skill / name).write_text("SECRET-FIXTURE\n", encoding="utf-8")
    lockfile = LockFile()
    lockfile.add_dependency(
        LockedDependency(
            repo_url="acme/creds",
            resolved_commit="abc123",
            depth=1,
            package_type="skill_bundle",
            deployed_files=[".github/skills/creds/"],
        )
    )
    lockfile.write(root / "apm.lock.yaml")
    return root, ".github/skills/creds"


_FORMATS = [
    pytest.param(("--format", "plugin"), _plugin_project, id="plugin"),
    pytest.param(("--format", "agent-plugin"), _plugin_project, id="agent-plugin"),
    pytest.param(("--format", "apm"), _apm_format_project, id="apm"),
]


@pytest.mark.parametrize(("fmt_args", "make_project"), _FORMATS)
@pytest.mark.parametrize("dry_run", [False, True], ids=["write", "dry-run"])
def test_pack_refuses_credential_files_naming_each_path(
    tmp_path: Path, apm_binary_path: Path, fmt_args, make_project, dry_run: bool
) -> None:
    project, prefix = make_project(tmp_path / "project", with_credentials=True)
    args = [*fmt_args, *(["--dry-run"] if dry_run else [])]

    result = _run_pack(apm_binary_path, project, *args)

    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert f"Refusing to pack {len(_CREDENTIAL_NAMES)} credential file(s)" in output
    for name in _CREDENTIAL_NAMES:
        assert f"  - {prefix}/{name}" in output, output
    assert "SECRET-FIXTURE" not in output
    build = project / "build"
    assert not build.exists() or not any(build.rglob("*")), sorted(build.rglob("*"))


@pytest.mark.parametrize(("fmt_args", "make_project"), _FORMATS)
def test_pack_without_credential_files_is_unchanged(
    tmp_path: Path, apm_binary_path: Path, fmt_args, make_project
) -> None:
    project, _prefix = make_project(tmp_path / "project", with_credentials=False)

    result = _run_pack(apm_binary_path, project, *fmt_args)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (project / "build" / f"{_PKG}-1.0.0").is_dir()
    assert "credential file" not in result.stdout + result.stderr


def test_publish_auto_pack_refuses_credential_files(tmp_path: Path, apm_binary_path: Path) -> None:
    project = tmp_path / "project"
    skill = project / ".apm" / "skills" / "creds"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Creds\n", encoding="utf-8")
    (skill / "id_rsa").write_text("SECRET-FIXTURE\n", encoding="utf-8")
    (project / "apm.yml").write_text(
        yaml.safe_dump(
            {
                "name": _PKG,
                "version": "1.0.0",
                "description": "Credential file publish fixture",
                "registries": {"fixture": {"url": "https://registry.invalid/v1"}},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    enable = subprocess.run(
        [str(apm_binary_path), "experimental", "enable", "registries"],
        cwd=project,
        env={**os.environ, "HOME": str(tmp_path / "home")},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert enable.returncode == 0, enable.stdout + enable.stderr

    result = subprocess.run(
        [str(apm_binary_path), "publish", "--package", "acme/creds", "--dry-run"],
        cwd=project,
        env={**os.environ, "HOME": str(tmp_path / "home")},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "  - .apm/skills/creds/id_rsa" in output, output
    assert "SECRET-FIXTURE" not in output
    assert not list(project.glob("*.zip"))
