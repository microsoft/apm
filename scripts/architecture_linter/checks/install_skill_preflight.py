"""Guard the shared skill selection and whole-plan pre-deployment boundary."""

from __future__ import annotations

import ast
import re

from scripts.architecture_linter.checks.install_deployment_shared import (
    _duplicate_definition_lines,
    _facts_for,
    _present,
    _summary,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.models import Violation

GUARD_SKILL_PREFLIGHT = "install-deployment-skill-preflight"
_OWNER = "src/apm_cli/integration/skill_preflight.py"
_PHASE = "src/apm_cli/install/phases/integrate.py"


def check_skill_preflight(provider: FactsProvider) -> tuple[Violation, ...]:
    """Require one source selector and batch preflight before the deployment loop."""
    required = {
        _OWNER: (
            "def selected_skill_sources(",
            "def find_unmanaged_conflicts(",
            "integrator.selected_skill_sources(package_info)",
            "source_plan.paths",
            "integrator._target_skill_dir(",
            "BaseIntegrator.check_collision(",
        ),
        "src/apm_cli/integration/skill_integrator.py": (
            "selected_skill_sources = staticmethod(selected_skill_sources)",
            "selection = self.selected_skill_sources(package_info)",
        ),
        "src/apm_cli/install/template.py": (
            "def preflight_skill_materializations(",
            "plan = prepare_package_deployment(",
            "skill.find_unmanaged_conflicts(",
            "deployment_plan=deployment_plan",
        ),
        "src/apm_cli/install/services.py": ("source_plan = deployment_plan.source_plan",),
        "src/apm_cli/install/deployment_plan.py": (
            "source_plan = DeployableSourcePlan.create(",
            "copilot_native = admits_native_plugin(package_info)",
            "codex_native is not None or copilot_native",
            "native_plugin_root=native_root",
        ),
    }
    failures: list[Violation] = []
    for path, fragments in required.items():
        facts, missing = _facts_for(provider, path, GUARD_SKILL_PREFLIGHT)
        failures.extend(missing)
        if not missing and any(not _present(facts, fragment) for fragment in fragments):
            failures.append(
                _summary(
                    GUARD_SKILL_PREFLIGHT,
                    path,
                    "Deployment preflight must reuse canonical selection, native admission, "
                    "and collision owners",
                )
            )
    index = provider.tree_index(_PHASE)
    run = index.function("run") if index is not None else None
    calls = (
        [node for node in index.own_scope(run) if isinstance(node, ast.Call)]
        if run is not None
        else []
    )
    preflight = [
        call
        for call in calls
        if isinstance(call.func, ast.Name) and call.func.id == "preflight_skill_materializations"
    ]
    deployments = [
        call
        for call in calls
        if isinstance(call.func, ast.Name) and call.func.id == "run_integration_template"
    ]
    if (
        len(preflight) != 1
        or not deployments
        or any(call.lineno < preflight[0].lineno for call in deployments)
    ):
        failures.append(
            _summary(
                GUARD_SKILL_PREFLIGHT,
                _PHASE,
                "Preflight the complete materialized skill plan before any dependency deployment",
            )
        )
    failures.extend(
        _duplicate_definition_lines(
            provider,
            rule_id=GUARD_SKILL_PREFLIGHT,
            prefix="src/apm_cli/",
            pattern=re.compile(r"^def (selected_skill_sources|find_unmanaged_conflicts)\("),
            owner=_OWNER,
            message="Skill source selection and unmanaged collision planning have one owner",
            respect_exempt=False,
        )
    )
    return tuple(failures)
