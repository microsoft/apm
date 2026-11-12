"""Real denied-capability acceptance on the disposable Windows standard user."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from apm_cli.utils import git_env
from scripts.windows_native_symlink_probe_entry import MUTATION_FAILURE, probe_native_context
from tests.unit.cache.test_git_symlink_config import _git
from tests.unit.cache.test_git_symlink_config import config_env as config_env
from tests.unit.cache.test_git_symlink_config import symlink_source as symlink_source

pytestmark = [pytest.mark.component, pytest.mark.requires_windows_native_standard_user]


@pytest.fixture
def native_pin_guard(
    monkeypatch: pytest.MonkeyPatch, record_property: Callable[[str, object], None]
) -> Iterator[None]:
    """Invert only the canonical symlink pin for the explicit negative run."""
    mode = os.environ.get("APM_WINDOWS_NATIVE_MUTATION", "")
    if mode not in {"", "drop-symlink-pin"}:
        raise ValueError("Unknown native symlink mutation")
    calls = 0
    if mode:
        original = git_env._materialize_git_config_snapshot

        def counted(*args: object, **kwargs: object) -> None:
            nonlocal calls
            calls += 1
            original(*args, **kwargs)

        monkeypatch.setattr(git_env, "_SYMLINK_PIN_VALUE", "false")
        monkeypatch.setattr(git_env, "_materialize_git_config_snapshot", counted)
    yield
    if mode:
        record_property("mutation_materializations", calls)
        assert calls > 0, "The native mutation did not reach the production config materializer"


@pytest.mark.usefixtures("native_pin_guard")
def test_native_standard_user_symlink_diagnostic(
    tmp_path: Path,
    config_env: dict[str, str],
    symlink_source: Path,
    record_property: Callable[[str, object], None],
) -> None:
    """Without link privilege, APM fails with the diagnostic instead of link text."""
    context = probe_native_context(
        tmp_path / "capability", os.environ["APM_WINDOWS_NATIVE_EXPECTED_SID"]
    )
    record_property("native_context", json.dumps(context, sort_keys=True))
    global_path = Path(config_env["GIT_CONFIG_GLOBAL"])
    original_global = global_path.read_bytes()
    assert _git(config_env, "config", "--global", "--bool", "core.symlinks") == "true"
    native = tmp_path / "native"
    target = tmp_path / "apm"
    _git(config_env, "clone", "--quiet", "--template=", str(symlink_source), str(native))
    assert _git(config_env, "-C", str(native), "config", "--local", "--bool", "core.symlinks") == (
        "false"
    )
    assert not (native / "AGENTS.md").is_symlink()
    assert (native / "AGENTS.md").read_bytes() == b"CLAUDE.md"

    try:
        git_env.clone_git_worktree(str(symlink_source), target, env=config_env)
    except git_env.GitSymlinkCapabilityError as error:
        record_property("diagnostic", str(error))
        assert "symbolic links" in str(error)
        assert "Developer Mode" in str(error)
        assert isinstance(error.__cause__, subprocess.CalledProcessError)
        assert "unable to create symlink AGENTS.md" in (error.__cause__.stderr or "")
    else:
        pytest.fail(f"{MUTATION_FAILURE}; AGENTS.md bytes {(target / 'AGENTS.md').read_bytes()!r}")

    assert global_path.read_bytes() == original_global
    assert _git(config_env, "config", "--global", "--bool", "core.symlinks") == "true"
