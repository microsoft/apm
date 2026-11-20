# Root conftest.py -- shared pytest configuration
#
# Test directory structure:
#   tests/unit/          -- Fast isolated unit tests (default CI scope)
#   tests/integration/   -- E2E tests requiring network / external services
#   tests/acceptance/    -- Acceptance criteria tests
#   tests/benchmarks/    -- Performance benchmarks (excluded by default)
#   tests/test_*.py      -- Root-level tests (mixed unit/integration)
#
# Quick reference:
#   uv run pytest tests/unit tests/test_console.py -x   # CI-equivalent fast run
#   uv run pytest                                         # Full suite
#   uv run pytest -m benchmark                            # Benchmarks only
#
# Path.home() override (top of file, runs at conftest import time on every
# pytest-xdist worker): the windows-2025-vs2026 GitHub-hosted runner does not
# seed USERPROFILE / HOMEDRIVE / HOMEPATH for pytest-xdist worker subprocesses,
# so Path.home() raises RuntimeError. Earlier attempts patched only the env
# vars in tests/unit/conftest.py, but at least one xdist worker (gw2) still
# evaluated Path.home() before that conftest's import-time mutation took
# effect. Override Path.home() directly here -- the root conftest is loaded
# by every worker before any test in any test directory runs, so this is
# the earliest hook we have without writing a pytest plugin.

import contextlib
import functools
import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def valid_hook_package(tmp_path: Path) -> Path:
    """Materialize the minimum valid manifestless hook package."""
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "lint.json").write_text(
        '{"hooks":{"preToolUse":[{"bash":"echo fixture"}]}}', encoding="utf-8"
    )
    return tmp_path


_TMP_HOME = Path(tempfile.mkdtemp(prefix="apm-test-home-"))


def _ensure_home_env(home: Path) -> None:
    home_str = str(home)
    os.environ["HOME"] = home_str
    if os.name == "nt":
        os.environ["USERPROFILE"] = home_str
        drive, _, tail = home_str.partition(":")
        if tail:
            os.environ["HOMEDRIVE"] = f"{drive}:"
            os.environ["HOMEPATH"] = tail


_ensure_home_env(_TMP_HOME)
# User-scope harness roots honor these variables; keep a developer's value
# from redirecting tests that expect the default roots.
for _user_root_variable in ("CODEX_HOME", "COPILOT_HOME", "CLAUDE_CONFIG_DIR", "HERMES_HOME"):
    os.environ.pop(_user_root_variable, None)
# A developer's APM_HOME must not redirect user-scope metadata during tests.
os.environ.pop("APM_HOME", None)


def _hermetic_home(_cls=Path) -> Path:
    """Resolve a home dir without ever raising.

    Honors HOME / USERPROFILE / HOMEDRIVE+HOMEPATH so per-test
    `monkeypatch.setenv("HOME", ...)` (or its Windows-trio equivalent)
    keeps working. Falls back to a hermetic tmp dir only when the env
    is empty -- which is the windows-2025-vs2026 xdist worker case.
    """
    home = os.environ.get("HOME")
    if not home and os.name == "nt":
        home = os.environ.get("USERPROFILE")
        if not home:
            drive = os.environ.get("HOMEDRIVE", "")
            tail = os.environ.get("HOMEPATH", "")
            if tail:
                home = drive + tail
    return Path(home) if home else _TMP_HOME


# Override Path.home() so any code path -- production or test -- that calls
# it during the test run gets the hermetic tmp dir, regardless of whether
# the runner subprocess inherited a usable HOME / USERPROFILE.
Path.home = classmethod(_hermetic_home)  # type: ignore[method-assign]


# Same problem, different code path: Path("~/pkg").expanduser() goes through
# ntpath.expanduser, which raises RuntimeError("Could not determine home
# directory.") on the windows-2025-vs2026 runner when USERPROFILE and
# HOMEPATH are both absent. Production code (e.g. install.package_resolution
# user_scope_rejection_reason) relies on expanduser to detect that ~/pkg is
# absolute. Wrap Path.expanduser so the RuntimeError can never surface.
_ORIGINAL_EXPANDUSER = Path.expanduser


def _hermetic_expanduser(self):
    try:
        return _ORIGINAL_EXPANDUSER(self)
    except RuntimeError:
        parts = self.parts
        if not parts or not parts[0].startswith("~"):
            return self
        remainder = parts[1:]
        return _TMP_HOME.joinpath(*remainder) if remainder else _TMP_HOME


Path.expanduser = _hermetic_expanduser  # type: ignore[method-assign]


@pytest.fixture(autouse=True, scope="session")
def _validate_primitive_coverage():
    """Fail fast if KNOWN_TARGETS has primitives without dispatch handlers."""
    from apm_cli.integration.coverage import check_primitive_coverage
    from apm_cli.integration.dispatch import get_dispatch_table

    dispatch = get_dispatch_table()
    check_primitive_coverage(dispatch)


@pytest.fixture(autouse=True)
def _apm_state_leak_detector(request: pytest.FixtureRequest):
    """Fail the test that leaves process state changed, then restore it.

    Snapshots the environment (including PATH), the working directory and the
    process-global state in ``tests/utils/state_leaks.py`` (console routing,
    config cache, git/gh executable memos). A leak there reaches every later
    test on the same xdist worker, so a victim fails far from its cause.
    Known leakers live in ``tests/fixtures/test_state_leak_baseline.json``,
    which may only shrink.

    The name sorts first among this file's autouse fixtures, so it wraps the
    resets below and judges the state they leave; a unit guard pins that.
    """
    from tests.utils import state_leaks

    nodeid = request.node.nodeid
    if not state_leaks.applies_to(nodeid):
        yield
        return
    before = state_leaks.take_snapshot()
    yield
    after = state_leaks.take_snapshot()
    leaks = state_leaks.leaked_keys(before, after)
    if not leaks and nodeid not in _state_leak_baseline():
        return
    state_leaks.restore(before)
    message = state_leaks.verdict(nodeid, leaks, _state_leak_baseline())
    if message:
        pytest.fail(message, pytrace=False)


@functools.cache
def _state_leak_baseline() -> dict[str, frozenset[str]]:
    from tests.utils import state_leaks

    return state_leaks.load_baseline()


@pytest.fixture(autouse=True)
def _reset_console_state():
    """Reset APM and Rich console singletons before and after each test.

    Machine-readable output (``--json``, ``--format json``; see
    ``configure_output_mode``) calls ``set_console_stderr(True)``, which
    routes rich/click output to stderr for the rest of the process. A test
    that invokes such a command in-process would otherwise leave every later
    test on the worker reading empty stdout.

    Tests that initialize Rich's process-global console while its
    ``Console`` class is patched can also leave a ``MagicMock`` clock
    behind. Rich progress then compares mock timestamps in later tests.
    """
    import rich

    from apm_cli.utils.console import _reset_console

    # ``rich._console`` is Rich's process-global default console (the one
    # ``rich.get_console()``/``rich.print`` lazily create). Nulling it forces
    # a fresh real console next call. The attribute has lived at this name
    # since Rich 10.0; the project pins ``rich>=13.0.0`` (verified on 15.x).
    _reset_console()
    rich._console = None
    yield
    _reset_console()
    rich._console = None


@pytest.fixture(autouse=True)
def _reset_config_cache():
    """Drop the process-wide ``~/.apm/config.json`` cache around each test.

    ``get_config`` caches the file for the process lifetime. A test that reads
    config under a patched HOME would otherwise serve that config to later
    tests on the worker.
    """
    from apm_cli import config

    config._invalidate_config_cache()
    yield
    config._invalidate_config_cache()


@pytest.fixture(autouse=True)
def _restore_executable_caches():
    """Restore the process-wide git/gh executable caches after each test.

    A test that patches ``shutil.which`` while git resolves would otherwise
    cache a wrong path for every later test on the same worker. Restoring
    the pre-test value (rather than clearing it) undoes that leak without
    changing what any test observes at its start.
    """
    from apm_cli.utils import git_env

    if git_env._git_executable is None:
        # Start every test from the real, resolved git, so a test that clears
        # PATH sees the same warm cache whatever ran before it on the worker.
        with contextlib.suppress(FileNotFoundError):
            git_env.get_git_executable()
    saved = (git_env._git_executable, git_env._gh_executable)
    yield
    git_env._git_executable, git_env._gh_executable = saved


@pytest.fixture(autouse=True)
def _isolate_discovery_state():
    """Clear process-scoped discovery cache + perf counters between tests.

    These globals are reset at the top of ``run_install_pipeline`` in
    production, but unit tests exercise ``discover_primitives`` and
    ``find_primitive_files`` directly. Without this fixture, cache hits
    from test A would silently shadow walk-time measurements (and even
    primitive results) in test B -- making the suite order-dependent.
    See review notes for #1533.
    """
    from apm_cli.primitives.discovery import clear_discovery_cache
    from apm_cli.utils import perf_stats

    clear_discovery_cache()
    perf_stats.reset()
    yield
    clear_discovery_cache()
    perf_stats.reset()


@pytest.fixture(autouse=True)
def _hermetic_copilot_plugin_capability():
    """Reset native Agent Plugin registration state between tests.

    Admission is a pure function of resolved target names -- it never reads
    the host (no binary probe, no version, no environment override), so
    there is nothing to pin here. This fixture only resets the published
    capability contextvar so one test's ``activate_native_registration``
    call can never leak into the next.
    """
    from apm_cli.copilot_plugins.capability import _ACTIVE as _capability_var
    from apm_cli.copilot_plugins.capability import reset_native_registration

    token = _capability_var.set(None)
    try:
        yield
    finally:
        with contextlib.suppress(ValueError):
            reset_native_registration(token)


class _OfflineMcpRegistryClient:
    registry_url = "https://mcp-registry.invalid"

    def find_server_by_reference(self, name):
        raise ConnectionError("MCP registry lookups are disabled in tests")


@pytest.fixture(autouse=True)
def _hermetic_mcp_transport_lookup():
    """Keep policy transport resolution off the public MCP registry.

    Policy ``mcp.transport.allow`` resolves registry servers through
    ``mcp_transport_resolution._registry_client``; tests that need a
    registry patch that seam with their own fake. Restored by hand (not
    ``monkeypatch``) so this autouse fixture does not reorder other
    fixtures' monkeypatch teardown.
    """
    from apm_cli.integration import mcp_transport_resolution

    original = mcp_transport_resolution._registry_client
    mcp_transport_resolution.clear_registry_info_memo()
    mcp_transport_resolution._registry_client = lambda registry_url: _OfflineMcpRegistryClient()
    try:
        yield
    finally:
        mcp_transport_resolution._registry_client = original
        mcp_transport_resolution.clear_registry_info_memo()


_LIFECYCLE_MERGE_GROUP_MARKER = "lifecycle_merge_group"
_LIFECYCLE_SMOKE_MARKER = "lifecycle_smoke"


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Imply ``lifecycle_smoke`` and bind OpenAPM req markers to the conformance map.

    A ``lifecycle_merge_group`` node is also ``lifecycle_smoke``, so the
    merge-group selection is a subset of the full lifecycle smoke selection by
    construction; ``tryfirst`` runs this before ``-m`` deselection. Running
    first also means environment-dependent skip markers that other conftests
    add at collection time never change a requirement's recorded status.
    """
    for item in items:
        if item.get_closest_marker(_LIFECYCLE_MERGE_GROUP_MARKER) and not item.get_closest_marker(
            _LIFECYCLE_SMOKE_MARKER
        ):
            item.add_marker(_LIFECYCLE_SMOKE_MARKER)

    from tests.spec_conformance.coverage_map import record

    record(config, items)
