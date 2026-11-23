"""Guard the receipt-less skill adoption baseline owner (#3391, #3625, #3883)."""

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

GUARD_SKILL_ADOPTION_BASELINE = "install-deployment-skill-adoption-baseline"
_OWNER = "src/apm_cli/integration/skill_adoption.py"


def check_skill_adoption_baseline(provider: FactsProvider) -> tuple[Violation, ...]:
    """Adoption compares with the locked render whenever the installed commit changes."""
    required = {
        _OWNER: (
            "def installing_commit(",
            "return LockedSkillSources(fetch, locked_commit)",
            "locked = locked_sources.plan(owner, plan, targets, commit)",
            "owner, commit = integrator._skill_current_owner, integrator._skill_current_commit",
        ),
        "src/apm_cli/integration/skill_preflight.py": ("commit=installing_commit(package_info),",),
        "src/apm_cli/integration/skill_integrator.py": (
            "self._skill_current_commit = installing_commit(package_info)",
            "adopted = skill_tree_baselines(",
        ),
        "src/apm_cli/install/pipeline.py": ("locked = locked_skill_sources(ctx)",),
    }
    failures: list[Violation] = []
    for path, fragments in required.items():
        facts, missing = _facts_for(provider, path, GUARD_SKILL_ADOPTION_BASELINE)
        failures.extend(missing)
        if not missing and any(not _present(facts, fragment) for fragment in fragments):
            failures.append(
                _summary(
                    GUARD_SKILL_ADOPTION_BASELINE,
                    path,
                    "Skill adoption baselines must route through skill_adoption with the installing commit",
                )
            )
    index = provider.tree_index(_OWNER)
    gate = index.function("locked_skill_sources") if index is not None else None
    if gate is None or any(
        isinstance(node, ast.Attribute) and node.attr == "update_refs" for node in index.walk(gate)
    ):
        failures.append(
            _summary(
                GUARD_SKILL_ADOPTION_BASELINE,
                _OWNER,
                "The locked-source baseline follows the lock and installing commit, not apm update mode",
            )
        )
    failures.extend(
        _duplicate_definition_lines(
            provider,
            rule_id=GUARD_SKILL_ADOPTION_BASELINE,
            prefix="src/apm_cli/",
            pattern=re.compile(
                r"^def (skill_tree_baselines|locked_skill_sources|installing_commit)\("
            ),
            owner=_OWNER,
            message="Receipt-less skill adoption baselines have one owner",
            respect_exempt=False,
        )
    )
    return tuple(failures)
