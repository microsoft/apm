"""Thin rule catalog for marketplace/integration architecture checks."""

from scripts.architecture_linter.checks.marketplace_integration_analyzers import (
    COLLECTORS,
    RULES,
)

__all__ = ["COLLECTORS", "RULES"]
