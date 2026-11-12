"""Shared, side-effect-free helpers for the Agent Plugin projection analyzer.

Used by two or more of :mod:`agent_plugin_scan_primitives`,
:mod:`agent_plugin_boundary_checks_a`, :mod:`agent_plugin_boundary_checks_b`,
:mod:`agent_plugin_boundary_checks_c`, and :mod:`agent_plugin_projection`.
"""

from __future__ import annotations

PROJECTION = "src/apm_cli/agent_plugins/projection.py"


PACKAGE = "src/apm_cli/models/apm_package.py"


VALIDATION = "src/apm_cli/models/validation.py"


RESOLVER = "src/apm_cli/deps/apm_resolver.py"


ERRORS = "src/apm_cli/agent_plugins/errors.py"


TEMPLATE = "src/apm_cli/install/template.py"


INTEGRATE_PHASE = "src/apm_cli/install/phases/integrate.py"


LOCAL_BUNDLE_HANDLER = "src/apm_cli/install/local_bundle_handler.py"


CI_CHECKS = "src/apm_cli/policy/ci_checks.py"


UNINSTALL_CLI = "src/apm_cli/commands/uninstall/cli.py"


UNINSTALL_ENGINE = "src/apm_cli/commands/uninstall/engine.py"


INSTALL_COMMAND = "src/apm_cli/commands/install.py"


PRUNE_COMMAND = "src/apm_cli/commands/prune.py"


HOOK_INTEGRATOR = "src/apm_cli/integration/hook_integrator.py"


SKILL_INTEGRATOR = "src/apm_cli/integration/skill_integrator.py"


SKILL_ROUTING = "src/apm_cli/integration/skill_package_routing.py"


_BOUNDARY_OWNER = "enforce_agent_plugin_deployment_boundary"


_SURVIVOR_PREFLIGHT = "preflight_reintegration_survivors"
