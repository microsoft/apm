"""Keep scoped inline hook admission on the shared selection and consent owners."""

from __future__ import annotations

import re

from scripts.architecture_linter.checks.install_deployment_shared import (
    _duplicate_definition_lines,
    _facts_for,
    _present,
    _present_re,
    _summary,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.models import Violation

GUARD_INLINE_HOOKS = "install-deployment-inline-hooks"
_OWNER = "src/apm_cli/integration/inline_hooks.py"


def check_inline_hooks(provider: FactsProvider) -> tuple[Violation, ...]:
    """Require selected native documents, shared resources, and read-only consent replay."""
    required = {
        "src/apm_cli/install/deployable_source_plan.py": (
            "candidates & self.paths",
            "HookIntegrator.select_deployable_hook_sources(",
            "inline_documents=documents",
            "paths=self.paths - self.claude_withheld_paths",
        ),
        "src/apm_cli/integration/hook_source_selection.py": (
            "def inline_for(",
            "self.inline_documents.keys()",
            "strict=hook_file in inline_documents",
        ),
        _OWNER: (
            'selection.inline_for("claude")',
            'selected_bundle_files=selection.bundle_for("claude")',
            "copy_deployed_hook_bundle(",
            "require_complete=True",
        ),
        "src/apm_cli/install/services.py": (
            "materialize_inline_hook_resources(",
            '"source_plan": source_plan.for_target(_target.name)',
        ),
        "src/apm_cli/security/executables.py": (
            "DeployableSourcePlan.for_executable_scan(install_path)",
            "source_plan.inline_hook_documents",
        ),
        "src/apm_cli/install/drift.py": (
            "effective_exec_map_for_project(",
            "migrate_user_legacy=False",
            "allow_executables=allow_executables",
        ),
        "src/apm_cli/install/phases/cleanup.py": (
            "require_recorded_hash=dep_key in ctx.inline_hook_withheld_packages",
        ),
    }
    failures: list[Violation] = []
    for path, fragments in required.items():
        facts, missing = _facts_for(provider, path, GUARD_INLINE_HOOKS)
        failures.extend(missing)
        if not missing and any(not _present(facts, fragment) for fragment in fragments):
            failures.append(
                _summary(
                    GUARD_INLINE_HOOKS,
                    path,
                    "Inline hooks must reuse selected sources, native scope, consent and ownership",
                )
            )
    facts, missing = _facts_for(provider, _OWNER, GUARD_INLINE_HOOKS)
    if not missing and _present_re(facts, re.compile(r"\.(?:rglob|glob|walk)\(")):
        failures.append(
            _summary(
                GUARD_INLINE_HOOKS,
                _OWNER,
                "Inline hooks may not rediscover candidates or traverse a second resource bundle",
            )
        )
    failures.extend(
        _duplicate_definition_lines(
            provider,
            rule_id=GUARD_INLINE_HOOKS,
            prefix="src/apm_cli/",
            pattern=re.compile(
                r"^def (read_inline_hook_document|materialize_inline_hook_resources)\("
            ),
            owner=_OWNER,
            message="Standalone inline declaration and materialization have one scoped owner",
            respect_exempt=False,
        )
    )
    return tuple(failures)
