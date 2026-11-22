"""GitPython stays unloaded until a clone needs it (owner: cli-startup-import-budget).

``import git`` loads about 60 modules and runs ``git version`` in a
subprocess. Building the install downloader, as every ``apm install`` does
(including a no-op), must not pay that; GitPython loads only on a clone path.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.architecture_linter.runner import run_selected_rules

pytestmark = [pytest.mark.component, pytest.mark.promise("P-OWN-cli-startup-import-budget")]

ROOT = Path(__file__).resolve().parents[2]

_SETUP_PROBE = """
import json, sys
import apm_cli.commands.install
import apm_cli.deps.bare_cache
import apm_cli.deps.clone_engine
import apm_cli.deps.git_reference_resolver
from apm_cli.core.auth import AuthResolver
from apm_cli.deps.github_downloader import GitHubPackageDownloader
GitHubPackageDownloader(auth_resolver=AuthResolver())
print(json.dumps(sorted(m for m in sys.modules if m == "git" or m.startswith("git."))))
"""

# First failure in a run: the handler is evaluated while GitPython is unloaded,
# then the raise path imports it; the real error must still be caught.
_TRANSITION_PROBE = """
import sys
from apm_cli.deps.git_lazy import git_command_error, new_git_command_error
placeholder = git_command_error()
print("loaded-before", "git" in sys.modules)
try:
    raise new_git_command_error(["git", "ls-remote"], 128, stderr="denied")
except git_command_error() as exc:
    print("caught", type(exc).__name__, exc.status, placeholder is not type(exc))
"""


def _probe(source: str) -> list[str]:
    return [sys.executable, "-c", source]


def test_install_downloader_setup_does_not_import_gitpython() -> None:
    result = subprocess.run(
        _probe(_SETUP_PROBE), capture_output=True, text=True, timeout=120, check=False
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert json.loads(result.stdout.splitlines()[-1]) == []


def test_first_git_failure_is_caught_after_lazy_import() -> None:
    result = subprocess.run(
        _probe(_TRANSITION_PROBE), capture_output=True, text=True, timeout=120, check=False
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.splitlines() == [
        "loaded-before False",
        "caught GitCommandError 128 True",
    ]


@pytest.mark.parametrize(
    "path",
    [
        "src/apm_cli/deps/github_downloader.py",
        "src/apm_cli/deps/clone_engine.py",
        "src/apm_cli/deps/bare_cache.py",
        "src/apm_cli/deps/git_reference_resolver.py",
    ],
)
def test_startup_rule_rejects_an_eager_gitpython_import(path: str) -> None:
    source = (ROOT / path).read_text(encoding="utf-8")
    rule = "registry_delegation.cli_startup_imports"
    report = run_selected_rules(ROOT, (rule,), source_overrides={path: "import git\n" + source})
    assert report.failures == ()
    assert any(item.rule_id == rule and item.path == path for item in report.violations)
