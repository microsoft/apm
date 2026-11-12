"""APM-owned Git transport snapshots of the canonical admitted source plan."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from apm_cli.codex_plugins.client import CodexNativeError
from apm_cli.install.deployable_source_plan import DeployableSourcePlan
from apm_cli.utils.atomic_io import atomic_write_text
from apm_cli.utils.content_hash import compute_native_bundle_hash
from apm_cli.utils.git_env import (
    clear_git_auth_env,
    clear_git_platform_token_env,
    get_git_executable,
    git_no_hooks_args,
    git_no_templates_args,
    git_subprocess_env,
)
from apm_cli.utils.path_security import ensure_path_within, has_symlink_component, safe_rmtree
from apm_cli.utils.paths import portable_relpath


@dataclass(frozen=True)
class NativeSnapshot:
    """Transport/deployment identity, separate from the original dependency pin."""

    path: Path
    content_commit: str
    catalog_commit: str


def _git(root: Path, arguments: list[str], *, input_bytes: bytes | None = None) -> str:
    """Run only local Git plumbing without credentials, hooks, filters, or templates."""
    env = git_subprocess_env()
    clear_git_platform_token_env(env, remove=True)
    clear_git_auth_env(env, remove_helpers=True)
    env.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_COUNT": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": "APM",
            "GIT_AUTHOR_EMAIL": "apm@localhost",
            "GIT_COMMITTER_NAME": "APM",
            "GIT_COMMITTER_EMAIL": "apm@localhost",
        }
    )
    try:
        result = subprocess.run(
            [
                get_git_executable(),
                *git_no_hooks_args(),
                "-c",
                "core.autocrlf=false",
                "-c",
                "commit.gpgsign=false",
                *arguments,
            ],
            cwd=root,
            env=env,
            input=input_bytes,
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CodexNativeError("Cannot create the local native transport snapshot.") from error
    if result.returncode:
        raise CodexNativeError(
            "Local Git snapshot creation failed. Check Git and write permissions before retrying."
        )
    return result.stdout.decode("utf-8").strip()


def _index_files(root: Path, paths: list[Path]) -> None:
    """Stage exact bytes in one batch, without applying publisher Git attributes."""
    quoted_paths = "".join(json.dumps(str(path), ensure_ascii=False) + "\n" for path in paths)
    blobs = _git(
        root,
        ["hash-object", "--no-filters", "-w", "--stdin-paths"],
        input_bytes=quoted_paths.encode("utf-8"),
    ).splitlines()
    if len(blobs) != len(paths):
        raise CodexNativeError("Git did not record the complete native snapshot.")
    rows = []
    for path, blob in zip(paths, blobs, strict=True):
        mode = "100755" if path.stat().st_mode & stat.S_IXUSR else "100644"
        rows.append(f"{mode} {blob}\t{portable_relpath(path, root)}\0")
    _git(root, ["update-index", "--add", "-z", "--index-info"], input_bytes="".join(rows).encode())


def prepare_snapshot(
    plan: DeployableSourcePlan,
    destination: Path,
    marketplace: str,
    plugin_name: str,
    *,
    expected_hash: str,
) -> NativeSnapshot:
    """Materialize only admitted bytes; Codex owns every native-cache mutation."""
    if compute_native_bundle_hash(plan.source_root) != expected_hash:
        raise CodexNativeError("Codex plugin source changed after admission; rerun apm install.")
    if destination.exists() or destination.is_symlink():
        raise CodexNativeError(
            "Native snapshot destination already exists; refusing to replace it."
        )
    destination.mkdir(parents=True)
    with ExitStack() as rollback:
        rollback.callback(safe_rmtree, destination, destination.parent)
        snapshot = _populate_snapshot(plan, destination, marketplace, plugin_name, expected_hash)
        rollback.pop_all()
        return snapshot


def _populate_snapshot(
    plan: DeployableSourcePlan,
    destination: Path,
    marketplace: str,
    plugin_name: str,
    expected_hash: str,
) -> NativeSnapshot:
    """Populate a newly owned directory while the caller holds its rollback."""
    payload = destination / "plugins" / plugin_name
    ensure_path_within(payload, destination)
    payload.mkdir(parents=True)
    copied = []
    for relative in sorted(plan.paths):
        source = plan.source_root / relative
        target = payload / relative
        ensure_path_within(source, plan.source_root)
        ensure_path_within(target, payload)
        if has_symlink_component(plan.source_root, source):
            raise CodexNativeError("Codex plugin source changed to a symlink after admission.")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.append(target)
    if compute_native_bundle_hash(payload) != expected_hash:
        raise CodexNativeError(
            "Native source plan does not contain the complete admitted bundle; registration refused."
        )
    _git(destination, ["init", "--initial-branch=main", *git_no_templates_args()])
    _index_files(destination, copied)
    _git(destination, ["commit", "-m", "APM admitted native package snapshot"])
    content_commit = _git(destination, ["rev-parse", "HEAD"])
    catalog = destination / ".agents/plugins/marketplace.json"
    catalog.parent.mkdir(parents=True)
    atomic_write_text(
        catalog,
        json.dumps(
            {
                "name": marketplace,
                "plugins": [
                    {
                        "name": plugin_name,
                        "source": {
                            "source": "url",
                            "url": destination.as_uri(),
                            "path": f"plugins/{plugin_name}",
                            "sha": content_commit,
                        },
                        "policy": {"installation": "AVAILABLE", "authentication": "ON_USE"},
                    }
                ],
            },
            indent=2,
        )
        + "\n",
    )
    _index_files(destination, [catalog])
    _git(destination, ["commit", "-m", "APM pinned native marketplace generation"])
    return NativeSnapshot(destination, content_commit, _git(destination, ["rev-parse", "HEAD"]))


def repair_generation(snapshot: NativeSnapshot) -> NativeSnapshot:
    """Refresh transport identity without changing package bytes or their content pin."""
    _git(
        snapshot.path,
        ["commit", "--allow-empty", "-m", "APM owned native cache repair generation"],
    )
    return NativeSnapshot(
        snapshot.path, snapshot.content_commit, _git(snapshot.path, ["rev-parse", "HEAD"])
    )
