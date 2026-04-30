"""APM package integration utilities."""

from .agent_integrator import AgentIntegrator
from .base_integrator import BaseIntegrator, IntegrationResult
from .coverage import check_primitive_coverage
from .dispatch import PrimitiveDispatch, get_dispatch_table
from .hook_integrator import HookIntegrator
from .instruction_integrator import InstructionIntegrator
from .mcp_integrator import MCPIntegrator
from .prompt_integrator import PromptIntegrator
from .skill_integrator import (
    SkillIntegrator,
    copy_skill_to_target,
    get_effective_type,
    normalize_skill_name,
    should_compile_instructions,
    should_install_skill,
    to_hyphen_case,
    validate_skill_name,
)
from .skill_transformer import SkillTransformer
from .targets import (
    KNOWN_TARGETS,
    PrimitiveMapping,
    TargetProfile,
    active_targets,
    get_integration_prefixes,
)

__all__ = [
    "KNOWN_TARGETS",
    "AgentIntegrator",
    "BaseIntegrator",
    "HookIntegrator",
    "InstructionIntegrator",
    "IntegrationResult",
    "MCPIntegrator",
    "PrimitiveDispatch",
    "PrimitiveMapping",
    "PromptIntegrator",
    "SkillIntegrator",
    "SkillTransformer",
    "TargetProfile",
    "active_targets",
    "check_primitive_coverage",
    "copy_skill_to_target",
    "get_dispatch_table",
    "get_effective_type",
    "get_integration_prefixes",
    "normalize_skill_name",
    "should_compile_instructions",
    "should_install_skill",
    "to_hyphen_case",
    "validate_skill_name",
]
