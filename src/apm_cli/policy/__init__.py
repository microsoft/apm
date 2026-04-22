"""APM Policy schema, parser, matching, inheritance, and discovery utilities."""

from .schema import (
    ApmPolicy,
    CompilationPolicy,
    CompilationStrategyPolicy,
    CompilationTargetPolicy,
    DependencyPolicy,
    ManifestPolicy,
    McpPolicy,
    McpTransportPolicy,
    PolicyCache,
    UnmanagedFilesPolicy,
)
from .parser import PolicyValidationError, load_policy, validate_policy
from .matcher import check_dependency_allowed, check_mcp_allowed, matches_pattern
from .inheritance import merge_policies, resolve_policy_chain, PolicyInheritanceError
from .discovery import PolicyFetchResult, discover_policy
from .models import CIAuditResult, CheckResult
from .policy_checks import run_dependency_policy_checks, run_policy_checks

__all__ = [
    "ApmPolicy",
    "CompilationPolicy",
    "CompilationStrategyPolicy",
    "CompilationTargetPolicy",
    "DependencyPolicy",
    "ManifestPolicy",
    "McpPolicy",
    "McpTransportPolicy",
    "PolicyCache",
    "PolicyFetchResult",
    "PolicyInheritanceError",
    "PolicyValidationError",
    "UnmanagedFilesPolicy",
    "check_dependency_allowed",
    "check_mcp_allowed",
    "discover_policy",
    "load_policy",
    "matches_pattern",
    "merge_policies",
    "resolve_policy_chain",
    "run_dependency_policy_checks",
    "run_policy_checks",
    "validate_policy",
    "CIAuditResult",
    "CheckResult",
]
