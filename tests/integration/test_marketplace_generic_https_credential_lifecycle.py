"""Installed-CLI lifecycle proof for generic marketplace HTTPS helpers."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from apm_cli.core.tls_trust import configure_process_tls_trust
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_http_server import LocalGitHttpServerFactory
from tests.utils.local_git_repository import LocalGitRepositoryFactory

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
    pytest.mark.requires_e2e_mode,
]

_HELPER_PASSWORD = "fixture-marketplace-password"
_DENY_PROXY = "http://127.0.0.1:9"
_SENTINEL_NAMES = (
    "ADO_APM_PAT",
    "GH_TOKEN",
    "GITHUB_APM_PAT",
    "GITHUB_TOKEN",
    "GIT_HTTP_EXTRAHEADER",
    "GIT_TOKEN",
)


def _real_git() -> Path:
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("git executable not available")
    return Path(executable).resolve()


def _git_exec_path(git: Path) -> str:
    """Return the helper directory paired with the selected Git executable."""
    return subprocess.run(
        (str(git), "--exec-path"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _write_credential_helper(git: Path, home: Path, log_path: Path) -> Path:
    """Create a real helper that records only sentinel names, never values."""
    helper = home / "credential-helper.py"
    helper.write_text(
        f"#!{sys.executable}\n"
        "import json\n"
        "import os\n"
        "from pathlib import Path\n"
        f"names = {list(_SENTINEL_NAMES)!r}\n"
        "path = Path(os.environ['APM_TEST_HELPER_LOG'])\n"
        "observations = json.loads(path.read_text()) if path.exists() else []\n"
        "observations.append([name for name in names if name in os.environ])\n"
        "path.write_text(json.dumps(observations), encoding='utf-8')\n"
        "print('username=x-access-token')\n"
        f"print('password={_HELPER_PASSWORD}')\n",
        encoding="ascii",
    )
    helper.chmod(helper.stat().st_mode | stat.S_IXUSR)
    _add_credential_helper(git, home / ".gitconfig", helper)
    return helper


def _add_credential_helper(git: Path, config_path: Path, helper: Path) -> None:
    """Append the fixture helper to one selected Git config file."""
    subprocess.run(
        (
            str(git),
            "config",
            "--file",
            str(config_path),
            "--add",
            "credential.helper",
            f"!{helper}",
        ),
        check=True,
        capture_output=True,
        text=True,
    )


def _write_tls_certificate(root: Path) -> tuple[Path, Path]:
    """Generate a short-lived loopback certificate for the HTTPS Git fixture."""
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl is required for the HTTPS Git fixture")
    certificate = root / "certificate.pem"
    key = root / "key.pem"
    subprocess.run(
        (
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(certificate),
            "-subj",
            "/CN=127.0.0.1",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
            "-sha256",
            "-days",
            "1",
        ),
        check=True,
        capture_output=True,
        text=True,
    )
    return certificate, key


def _verify_git_https_fixture(
    git: Path,
    *,
    remote_url: str,
    environment: dict[str, str],
) -> None:
    """Verify the native Git TLS backend and helper can reach the fixture."""
    verification_env = dict(environment)
    verification_env.pop("GIT_CONFIG_GLOBAL")
    verification_env.pop("GIT_CONFIG_NOSYSTEM")
    for name in _SENTINEL_NAMES:
        verification_env.pop(name, None)
    result = subprocess.run(
        (str(git), "ls-remote", "--heads", remote_url),
        cwd=Path(environment["HOME"]),
        env=verification_env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"native Git HTTPS fixture preflight failed\n"
        f"stdout={result.stdout!r}\n"
        f"stderr={result.stderr!r}"
    )


def _configure_git_https_fixture(
    git: Path,
    *,
    remote_base_url: str,
    config_paths: tuple[Path, ...],
) -> None:
    """Keep the self-signed loopback fixture direct and non-interactive."""
    for config_path in config_paths:
        for key, value in (
            (f"http.{remote_base_url}.sslVerify", "false"),
            (f"http.{remote_base_url}.proxy", ""),
        ):
            subprocess.run(
                (
                    str(git),
                    "config",
                    "--file",
                    str(config_path),
                    key,
                    value,
                ),
                check=True,
                capture_output=True,
                text=True,
            )


def test_generic_https_marketplace_add_uses_native_credential_helper(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """Add and list one generic HTTPS source through a real Git helper."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=os.environ)
    real_git = _real_git()
    helper_log = isolated.root / "credential-helper.json"
    helper = _write_credential_helper(real_git, isolated.home, helper_log)
    configured_helpers = subprocess.run(
        (
            str(real_git),
            "config",
            "--file",
            str(isolated.home / ".gitconfig"),
            "--get-all",
            "credential.helper",
        ),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert configured_helpers == ["", f"!{helper}"]
    environment = isolated.subprocess_env()
    _add_credential_helper(
        real_git,
        Path(environment["GIT_CONFIG_GLOBAL"]),
        helper,
    )
    environment.update(
        {
            "ADO_APM_PAT": "ado-sentinel",
            "GH_TOKEN": "gh-sentinel",
            "GITHUB_APM_PAT": "github-apm-sentinel",
            "GITHUB_TOKEN": "github-sentinel",
            "GIT_HTTP_EXTRAHEADER": "Authorization: sentinel",
            "GIT_TOKEN": "git-sentinel",
            "APM_TEST_HELPER_LOG": str(helper_log),
            "GIT_ALLOW_PROTOCOL": "file:http:https",
            "ALL_PROXY": _DENY_PROXY,
            "HTTP_PROXY": _DENY_PROXY,
            "HTTPS_PROXY": _DENY_PROXY,
            "NO_PROXY": "",
            "all_proxy": _DENY_PROXY,
            "http_proxy": _DENY_PROXY,
            "https_proxy": _DENY_PROXY,
            "no_proxy": "",
        }
    )
    environment["GIT_EXEC_PATH"] = _git_exec_path(real_git)
    repositories = LocalGitRepositoryFactory(isolated.repository_root, env=environment)
    repository = repositories.create("generic-marketplace")
    (repository.worktree / "marketplace.json").write_text(
        json.dumps({"name": "generic-marketplace", "plugins": []}),
        encoding="utf-8",
    )
    repositories.commit(repository, message="seed marketplace")
    server_factory = LocalGitHttpServerFactory(
        isolated.repository_root,
        real_git=real_git,
        env=environment,
    )
    certificate, key = _write_tls_certificate(isolated.root)

    # Reproduce the in-process CLI import order that installs truststore globally.
    configure_process_tls_trust()
    with server_factory.start(
        (repository,),
        password=_HELPER_PASSWORD,
        private_repositories=(repository,),
        certfile=certificate,
        keyfile=key,
    ) as server:
        _configure_git_https_fixture(
            real_git,
            remote_base_url=server.proxy_url,
            config_paths=(
                Path(environment["GIT_CONFIG_GLOBAL"]),
                isolated.home / ".gitconfig",
            ),
        )
        remote_url = server.remote_url(repository)
        _verify_git_https_fixture(
            real_git,
            remote_url=remote_url,
            environment=environment,
        )
        runner = ApmLifecycleRunner((str(apm_binary_path),))
        add_result = runner.run(
            ("marketplace", "add", remote_url, "--name", "generic-marketplace"),
            scenario_id="marketplace-generic-https-native-helper",
            cwd=isolated.work_root,
            env=environment,
        )
        observations = server.observations
        list_result = runner.run(
            ("marketplace", "list"),
            scenario_id="marketplace-generic-https-native-helper",
            cwd=isolated.work_root,
            env=environment,
        )

    assert add_result.returncode == 0, (
        f"stdout={add_result.stdout!r}\n"
        f"stderr={add_result.stderr!r}\n"
        f"http_observations={observations!r}\n"
        f"helper_log_exists={helper_log.exists()}"
    )
    assert list_result.returncode == 0, list_result.stderr
    assert helper_log.exists()
    observations = json.loads(helper_log.read_text(encoding="utf-8"))
    assert observations
    assert all(not names for names in observations)
    saved = json.loads((isolated.config_root / "marketplaces.json").read_text(encoding="utf-8"))
    assert len(saved["marketplaces"]) == 1
    assert saved["marketplaces"][0]["name"] == "generic-marketplace"


def test_generic_https_marketplace_add_rejects_http_rewrite(
    tmp_path: Path,
    apm_binary_path: Path,
) -> None:
    """The installed CLI rejects an HTTPS-to-HTTP Git rewrite safely."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=os.environ)
    real_git = _real_git()
    environment = isolated.subprocess_env()
    subprocess.run(
        (
            str(real_git),
            "config",
            "--file",
            environment["GIT_CONFIG_GLOBAL"],
            "url.http://127.0.0.1:9/.insteadOf",
            "https://gitea.example.test/",
        ),
        check=True,
        capture_output=True,
        text=True,
    )
    environment.update(
        {
            "GITHUB_APM_PAT": "github-apm-sentinel",
            "GIT_EXEC_PATH": _git_exec_path(real_git),
        }
    )

    result = ApmLifecycleRunner((str(apm_binary_path),)).run(
        (
            "marketplace",
            "add",
            "https://gitea.example.test/org/repo.git",
            "--name",
            "downgrade-marketplace",
        ),
        scenario_id="marketplace-generic-https-downgrade",
        cwd=isolated.work_root,
        env=environment,
    )

    output = f"{result.stdout}\n{result.stderr}"
    normalized_output = " ".join(output.split())
    assert result.returncode == 1
    assert "Failed to register marketplace" in output
    assert "rewrite" in output
    assert "insecure HTTP" in output
    assert "apm marketplace update downgrade-marketplace" in normalized_output
    assert "github-apm-sentinel" not in output
