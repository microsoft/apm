"""Regression guard for #3524: the dead workflow/aggregator code stays removed.

``apm_cli.workflow`` and ``apm_cli.deps.aggregator`` had no production
callers; ``apm run`` goes through ``core/script_runner.py`` instead.
"""

import importlib.util

import pytest

from apm_cli import deps

REMOVED_MODULES = (
    "apm_cli.workflow",
    "apm_cli.workflow.discovery",
    "apm_cli.workflow.parser",
    "apm_cli.workflow.runner",
    "apm_cli.deps.aggregator",
)

REMOVED_DEPS_EXPORTS = (
    "scan_workflows_for_dependencies",
    "sync_workflow_dependencies",
)


@pytest.mark.parametrize("module_name", REMOVED_MODULES)
def test_removed_module_is_not_importable(module_name: str) -> None:
    try:
        spec = importlib.util.find_spec(module_name)
    except ModuleNotFoundError:
        spec = None
    # A leftover __pycache__ dir resolves as an empty namespace package
    # (origin None); any restored source file has a concrete origin.
    assert spec is None or spec.origin is None, (
        f"{module_name} was removed in #3524 and must not return"
    )


@pytest.mark.parametrize("name", REMOVED_DEPS_EXPORTS)
def test_deps_package_does_not_reexport_aggregator(name: str) -> None:
    assert not hasattr(deps, name)
    assert name not in deps.__all__


def test_script_runner_parameter_substitution_is_still_live() -> None:
    from apm_cli.core.script_runner import PromptCompiler

    assert callable(PromptCompiler._substitute_parameters)
