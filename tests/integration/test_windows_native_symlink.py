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
def native_precedence_guard(
    monkeypatch: pytest.MonkeyPatch, record_property: Callable[[str, object], None]
) -> Iterator[None]:
    """Remove only the local-over-inherited guard for the explicit negative run."""
    mode = os.environ.get("APM_WINDOWS_NATIVE_MUTATION", "")
    if mode not in {"", "drop-local-precedence"}:
        raise ValueError("Unknown native symlink mutation")
    calls = 0
    changes: list[tuple[str, str, str, str, bool, bool]] = []
    original_selection = git_env._symlink_entry_wins
    if mode:

        def without_local_precedence(
            current: git_env.GitConfigEntry | None, candidate: git_env.GitConfigEntry
        ) -> bool:
            nonlocal calls
            calls += 1
            mutant = current is None or candidate.scope == "command"
            correct = original_selection(current, candidate)
            if current is not None and mutant != correct:
                changes.append(
                    (
                        current.scope,
                        current.value,
                        candidate.scope,
                        candidate.value,
                        correct,
                        mutant,
                    )
                )
            return mutant

        monkeypatch.setattr(git_env, "_symlink_entry_wins", without_local_precedence)
    yield
    if mode:
        assert calls > 0, "The native mutation did not reach the production selection helper"
        record_property("mutation_decisions", json.dumps(changes))
        assert ("global", "true", "local", "false", True, False) in changes, (
            "The mutation must actually reject native local false over inherited global true"
        )


@pytest.mark.usefixtures("native_precedence_guard")
def test_native_standard_user_symlink_fallback(
    tmp_path: Path,
    config_env: dict[str, str],
    symlink_source: Path,
    record_property: Callable[[str, object], None],
) -> None:
    """Match native Git with global true, actual local false and no create privilege."""
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
    except subprocess.CalledProcessError as error:
        if "unable to create symlink AGENTS.md" not in (error.stderr or ""):
            raise
        pytest.fail(f"{MUTATION_FAILURE}; Git exit {error.returncode}")

    assert not (target / "AGENTS.md").is_symlink()
    assert (target / "AGENTS.md").read_bytes() == (native / "AGENTS.md").read_bytes()
    assert (target / "CLAUDE.md").read_bytes() == (native / "CLAUDE.md").read_bytes()
    assert (target / "CLAUDE.md").read_text(encoding="utf-8") == "Package instructions\n"
    assert _git(config_env, "-C", str(target), "ls-files", "--stage", "AGENTS.md").startswith(
        "120000 "
    )
    assert global_path.read_bytes() == original_global
    assert _git(config_env, "config", "--global", "--bool", "core.symlinks") == "true"
