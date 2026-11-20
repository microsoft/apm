"""APMLifecycle: a declared registry mirror may serve locked bytes; the hash decides (#4278).

PROMISE: req-rs-009 -- lockfile replay accepts bytes from any registry declared
in apm.yml when they hash to the locked ``resolved_hash``; ``resolved_url`` is
advisory. A hash mismatch still fails closed, and a URL no declared registry
serves is never fetched (source: docs/src/content/docs/specs/openapm-v0.1.md).
CELL: OP=install SRC=registry SCOPE=project LOCK=replay NET=mirror
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.integration.test_registry_archive_limits_lifecycle import (
    BASE,
    Registry,
    _archive,
    _entries,
)
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_github_https_fixture import openssl_executable
from tests.utils.local_scripted_https_proxy import local_scripted_https_proxy

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_apm_binary,
    pytest.mark.skipif(openssl_executable() is None, reason="openssl CLI required"),
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only fixture"),
    pytest.mark.e2e,
    pytest.mark.promise("P-LAW-auth.credentials_isolated", "P-LAW-source.ref_cache_coherent"),
]

_CORP = "registry.corp.example"
_MIRROR = "mirror.corp.example"
_INSTALL = ("install", "--target", "copilot", "--no-policy")
_DEPLOYED = ".github/instructions/guide.instructions.md"


def _text(result: CommandResult) -> str:
    return f"rc={result.returncode}\n{result.stdout}\n{result.stderr}"


@pytest.mark.req("req-rs-009")
def test_lock_replay_fetches_hash_matching_bytes_from_a_declared_mirror(
    tmp_path: Path, apm_binary_path: Path
) -> None:
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=dict(os.environ))
    runner = ApmLifecycleRunner((str(apm_binary_path),), timeout_seconds=180)
    project = isolated.work_root / "consumer"
    project.mkdir()
    corp, mirror = Registry(), Registry()
    archive = _archive("tar.gz", _entries("widget", "valid"))
    corp.archives["acme/widget"] = archive
    mirror.archives["acme/widget"] = archive
    dump_yaml(
        {
            "name": "consumer",
            "version": "1.0.0",
            "registries": {
                "corp": {"url": f"https://{_CORP}{BASE}"},
                "mirror": {"url": f"https://{_MIRROR}{BASE}"},
                "default": "corp",
            },
            "targets": ["copilot"],
            "dependencies": {"apm": ["acme/widget#1.0.0"]},
        },
        project / "apm.yml",
    )
    handlers = {_CORP: corp.handle, _MIRROR: mirror.handle}
    with local_scripted_https_proxy(isolated.root / "tls", handlers) as proxy:
        env = proxy.child_environment(isolated.subprocess_env())
        env["APM_NON_INTERACTIVE"] = "1"

        def apm(*args: str, step: str) -> CommandResult:
            return runner.run(args, scenario_id=step, cwd=project, env=env)

        enabled = apm("experimental", "enable", "registries", step="enable")
        assert enabled.returncode == 0, _text(enabled)
        first = apm(*_INSTALL, step="install-corp")
        assert first.returncode == 0, _text(first)

        # The lock now points at the mirror and corp no longer serves the bytes.
        lock_path = project / "apm.lock.yaml"
        lock = load_yaml(lock_path)
        (row,) = lock["dependencies"]
        row["resolved_url"] = row["resolved_url"].replace(_CORP, _MIRROR)
        dump_yaml(lock, lock_path)
        corp.archives.clear()
        for path in (project / "apm_modules", project / _DEPLOYED):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)

        replayed = apm(*_INSTALL, step="replay-mirror")
        assert replayed.returncode == 0, _text(replayed)
        assert (project / _DEPLOYED).is_file(), _text(replayed)

        # Bytes that do not hash to resolved_hash fail closed from any registry.
        lock_bytes = lock_path.read_bytes()
        mirror.archives["acme/widget"] = _archive("tar.gz", _entries("other", "valid"))
        shutil.rmtree(project / "apm_modules")
        tampered = apm(*_INSTALL, step="replay-mirror-tampered")
        assert tampered.returncode != 0, _text(tampered)
        assert lock_path.read_bytes() == lock_bytes, _text(tampered)
