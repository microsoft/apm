"""Build security-checked source plans for hook survivor reintegration."""

from typing import Any

from apm_cli.install.deployable_source_plan import DeployableSourcePlan
from apm_cli.install.exec_gate import check_executable_approval
from apm_cli.security.gate import BLOCK_POLICY
from apm_cli.utils.diagnostics import printable_ascii_text


def build_hook_reintegration_source_plan(
    dep_ref: Any,
    package_info: Any,
    targets: list[Any],
    allow_executables: dict[str, dict[str, bool]] | None,
) -> DeployableSourcePlan:
    """Return a scanned source plan before destructive hook reconciliation."""
    hooks_approved = check_executable_approval(
        dep_ref.get_identity(),
        package_info,
        allow_executables,
    )[0]
    source_plan = DeployableSourcePlan.create(
        package_info,
        targets,
        skill_subset=None,
        hooks_approved=hooks_approved,
        canvas_approved=False,
        skip_bin=True,
    )
    verdict = source_plan.scan_security(policy=BLOCK_POLICY)
    if verdict.should_block:
        raise ValueError(
            f"Refusing to rebuild hooks for {printable_ascii_text(dep_ref.get_identity())}: "
            "authorized source contains critical hidden characters"
        )
    return source_plan
