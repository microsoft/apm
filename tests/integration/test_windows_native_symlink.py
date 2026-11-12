"""Real denied-capability acceptance on the disposable Windows standard user (#3457).

Without symlink privilege, APM must still install a package that contains
links: Git writes link text and APM replaces it with the target content from
the commit's objects, so the hash equals the POSIX hash.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from apm_cli.cache.git_cache import GitCache
from apm_cli.utils import git_sparse
from apm_cli.utils.content_hash import compute_package_hash
from apm_cli.utils.file_ops import robust_copytree
from scripts.windows_native_symlink_probe_entry import MUTATION_FAILURE, probe_native_context
from tests.unit.cache.test_git_symlink_config import _git, _posix_package_hash
from tests.unit.cache.test_git_symlink_config import config_env as config_env
from tests.unit.cache.test_git_symlink_config import symlink_source as symlink_source

pytestmark = [pytest.mark.component, pytest.mark.requires_windows_native_standard_user]


@pytest.fixture
def native_materialization_guard(
    monkeypatch: pytest.MonkeyPatch, record_property: Callable[[str, object], None]
) -> Iterator[None]:
    """Disable only the Git-object link writer for the explicit negative run."""
    mode = os.environ.get("APM_WINDOWS_NATIVE_MUTATION", "")
    if mode not in {"", "drop-link-materialization"}:
        raise ValueError("Unknown native link mutation")
    calls = 0
    if mode:

        def skipped(*args: object, **kwargs: object) -> None:
            nonlocal calls
            calls += 1

        monkeypatch.setattr(git_sparse, "_write_link_plan", skipped)
    yield
    if mode:
        record_property("mutation_skipped_writes", calls)
        assert calls > 0, "The native mutation did not reach the production link writer"


@pytest.mark.usefixtures("native_materialization_guard")
def test_native_standard_user_materializes_links(
    tmp_path: Path,
    config_env: dict[str, str],
    symlink_source: Path,
    record_property: Callable[[str, object], None],
) -> None:
    """Without link privilege, APM installs link content with the POSIX hash."""
    context = probe_native_context(
        tmp_path / "capability", os.environ["APM_WINDOWS_NATIVE_EXPECTED_SID"]
    )
    record_property("native_context", json.dumps(context, sort_keys=True))
    global_path = Path(config_env["GIT_CONFIG_GLOBAL"])
    original_global = global_path.read_bytes()
    sha = _git(config_env, "-C", str(symlink_source), "rev-parse", "HEAD")

    checkout = GitCache(tmp_path / "cache").get_checkout(
        str(symlink_source), sha, locked_sha=sha, env=config_env
    )
    installed = tmp_path / "apm_modules" / "pkg"
    robust_copytree(checkout, installed, ignore=lambda _root, names: {".git"} & set(names))

    agents = (installed / "AGENTS.md").read_bytes()
    record_property("agents_bytes", repr(agents))
    if agents != b"Package instructions\n":
        pytest.fail(f"{MUTATION_FAILURE}; AGENTS.md bytes {agents!r}")
    assert not (installed / "AGENTS.md").is_symlink()
    assert compute_package_hash(installed) == _posix_package_hash(tmp_path)
    assert global_path.read_bytes() == original_global
