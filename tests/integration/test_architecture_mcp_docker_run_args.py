"""Static regression traps for the docker run argument owner (#3404).

``MCPClientAdapter._ensure_docker_run_args`` owns the ``docker run -i --rm``
default and options-only normalization. Container consumers must delegate to
it rather than re-growing an inline ``or ["run", "-i", "--rm"]`` default, which
is how options-only registry metadata lost its ``run`` subcommand.
"""

from pathlib import Path

import pytest

from scripts.architecture_linter.runner import run_selected_rules

pytestmark = pytest.mark.component
_ROOT = Path(__file__).resolve().parents[2]
_RULE = "mutation_writes.mcp_package_launcher"
_BASE = "src/apm_cli/adapters/client/base.py"


def test_docker_run_args_owner_routes_are_intact() -> None:
    report = run_selected_rules(_ROOT, {_RULE})
    assert report.failures == ()
    assert report.violations == ()


@pytest.mark.parametrize(
    "path,old,new",
    [
        (
            "src/apm_cli/adapters/client/codex.py",
            "runtime_args = self._ensure_docker_run_args(processed_runtime_args)",
            'runtime_args = processed_runtime_args or ["run", "-i", "--rm"]',
        ),
        (
            "src/apm_cli/adapters/client/copilot.py",
            "runtime_args = self._ensure_docker_run_args(processed_runtime_args)",
            'runtime_args = processed_runtime_args or ["run", "-i", "--rm"]',
        ),
        (
            "src/apm_cli/adapters/client/gemini.py",
            "runtime_args = self._ensure_docker_run_args(processed_rt)",
            'runtime_args = processed_rt or ["run", "-i", "--rm"]',
        ),
        (
            "src/apm_cli/adapters/client/vscode.py",
            "run_options = cls._ensure_docker_run_args(run_options)",
            "run_options = list(run_options)",
        ),
    ],
)
def test_docker_run_args_owner_bypass_is_rejected(path: str, old: str, new: str) -> None:
    source = (_ROOT / path).read_text(encoding="utf-8")
    assert old in source
    report = run_selected_rules(
        _ROOT, {_RULE}, source_overrides={path: source.replace(old, new, 1)}
    )
    assert report.failures == ()
    assert any(item.rule_id == _RULE and item.path == path for item in report.violations)


def test_duplicate_docker_run_args_owner_is_rejected() -> None:
    path = "src/apm_cli/adapters/client/codex.py"
    source = (_ROOT / path).read_text(encoding="utf-8")
    report = run_selected_rules(
        _ROOT,
        {_RULE},
        source_overrides={
            path: source + "\n\ndef _ensure_docker_run_args(args):\n    return args\n"
        },
    )
    assert report.failures == ()
    assert any(item.rule_id == _RULE and item.path == _BASE for item in report.violations)
