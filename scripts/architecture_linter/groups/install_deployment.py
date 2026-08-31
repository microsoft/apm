"""Thin rule catalog for install/deployment architecture checks."""

from scripts.architecture_linter.checks.install_deployment_analyzers import COLLECTORS, RULES

__all__ = ["COLLECTORS", "RULES"]
