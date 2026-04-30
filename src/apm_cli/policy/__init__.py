"""APM Policy schema, parser, matching, inheritance, and discovery utilities."""

from .discovery import PolicyFetchResult, discover_policy, discover_policy_with_chain
from .inheritance import PolicyInheritanceError, merge_policies, resolve_policy_chain
from .matcher import check_dependency_allowed, check_mcp_allowed, matches_pattern
from .models import CheckResult, CIAuditResult
from .parser import PolicyValidationError, load_policy, validate_policy
from .policy_checks import run_dependency_policy_checks, run_policy_checks
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

__all__ = [
    "ApmPolicy",
    "CIAuditResult",
    "CheckResult",
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
    "discover_policy_with_chain",
    "load_policy",
    "matches_pattern",
    "merge_policies",
    "resolve_policy_chain",
    "run_dependency_policy_checks",
    "run_policy_checks",
    "validate_policy",
]
