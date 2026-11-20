"""Closed-world classification of CLI commands against lifecycle serialization.

``install/locking.py`` owns cross-process lifecycle serialization. The
``install-deployment-lifecycle-serialization`` guard proves every route in
:data:`SERIALIZED_LIFECYCLE_ROUTES` carries its serialization decorator. This
module closes that list: every Click command or group callback under
``src/apm_cli`` must be classified exactly once as serialized, delegated to a
serialized route, exempt (writes no lifecycle state, with a reason), or
pending. A new command fails until it is classified, and a stale or
duplicated classification fails too.
"""

from __future__ import annotations

import re

from scripts.architecture_linter.checks.install_deployment_shared import (
    _SRC_PREFIX,
    _facts_for,
    _python_paths,
    _summary,
)
from scripts.architecture_linter.facts import FactsProvider
from scripts.architecture_linter.models import Violation

GUARD_LIFECYCLE_SERIALIZATION_CLOSED = "install-deployment-lifecycle-serialization-closed-world"

# Routes that must carry an install/locking.py serialization decorator: path ->
# function -> decorator. Command callbacks listed here are "serialized"; the
# helper routes are the targets of _DELEGATED commands.
SERIALIZED_LIFECYCLE_ROUTES: dict[str, dict[str, str]] = {
    "src/apm_cli/commands/audit.py": {
        "audit": "serialized_lifecycle_when",
    },
    "src/apm_cli/commands/approve.py": {
        "approve_cmd": "serialized_lifecycle",
        "deny_cmd": "serialized_lifecycle",
    },
    "src/apm_cli/commands/compile/cli.py": {
        "_handle_global_flag": "serialized_lifecycle_unless",
        "_run_compilation": "serialized_lifecycle_unless",
    },
    "src/apm_cli/commands/config.py": {
        "set": "serialized_lifecycle",
        "unset": "serialized_lifecycle",
    },
    "src/apm_cli/commands/deps/cli.py": {
        "clean": "serialized_lifecycle_unless",
        "update": "serialized_lifecycle",
    },
    "src/apm_cli/commands/experimental.py": {
        "enable_flag": "serialized_lifecycle",
        "disable_flag": "serialized_lifecycle",
        "reset_flags": "serialized_lifecycle",
    },
    "src/apm_cli/commands/init.py": {"init": "serialized_lifecycle_unless"},
    "src/apm_cli/commands/install.py": {"install": "serialized_lifecycle_unless"},
    "src/apm_cli/commands/lifecycle.py": {
        "lifecycle_init": "serialized_lifecycle",
        "lifecycle_trust": "serialized_lifecycle",
        "lifecycle_untrust": "serialized_lifecycle",
    },
    "src/apm_cli/commands/lock.py": {"_run_lock": "serialized_lifecycle"},
    "src/apm_cli/commands/marketplace/__init__.py": {
        "add": "serialized_lifecycle",
        "update": "serialized_lifecycle",
        "remove": "serialized_lifecycle",
    },
    "src/apm_cli/commands/marketplace/init.py": {"init": "serialized_lifecycle"},
    "src/apm_cli/commands/marketplace/migrate.py": {"migrate": "serialized_lifecycle_unless"},
    "src/apm_cli/commands/pack.py": {"unpack_cmd": "serialized_lifecycle_unless"},
    "src/apm_cli/commands/marketplace/plugin/add.py": {"add": "serialized_lifecycle"},
    "src/apm_cli/commands/marketplace/plugin/remove.py": {"remove": "serialized_lifecycle"},
    "src/apm_cli/commands/marketplace/plugin/set.py": {"set_cmd": "serialized_lifecycle"},
    "src/apm_cli/commands/plugin/init.py": {"init": "serialized_lifecycle"},
    "src/apm_cli/commands/prune.py": {"prune": "serialized_lifecycle_unless"},
    "src/apm_cli/commands/runtime.py": {
        "setup": "serialized_lifecycle",
        "remove": "serialized_lifecycle",
    },
    "src/apm_cli/commands/self_update.py": {"self_update": "serialized_lifecycle_unless"},
    "src/apm_cli/commands/uninstall/cli.py": {"uninstall": "serialized_lifecycle"},
    "src/apm_cli/commands/update.py": {"update": "serialized_lifecycle"},
}

_Command = tuple[str, str]

_COMPILE = "src/apm_cli/commands/compile/cli.py"
_LOCK = "src/apm_cli/commands/lock.py"

# Commands whose writes run inside serialized helper routes listed above.
_DELEGATED: dict[_Command, tuple[_Command, ...]] = {
    (_COMPILE, "compile"): ((_COMPILE, "_handle_global_flag"), (_COMPILE, "_run_compilation")),
    (_LOCK, "lock"): ((_LOCK, "_run_lock"),),
    ("src/apm_cli/commands/mcp.py", "mcp_install"): (
        ("src/apm_cli/commands/install.py", "install"),
    ),
}

_GROUP = "group dispatcher; subcommands are classified individually"
_READ_ONLY = "read-only"
_CACHE = "cache state is owned by cache/cleanup.py and cache/locking.py shard locks"
_USER_SCRIPTS = "runs user scripts that may invoke serialized apm commands"

# Commands that write no APM lifecycle state.
_EXEMPT: dict[_Command, str] = {
    ("src/apm_cli/cli.py", "cli"): _GROUP,
    ("src/apm_cli/commands/auth.py", "auth"): _READ_ONLY,
    ("src/apm_cli/commands/cache.py", "cache"): _GROUP,
    ("src/apm_cli/commands/cache.py", "clean"): _CACHE,
    ("src/apm_cli/commands/cache.py", "info"): _READ_ONLY,
    ("src/apm_cli/commands/cache.py", "prune"): _CACHE,
    ("src/apm_cli/commands/config.py", "config"): _GROUP,
    ("src/apm_cli/commands/config.py", "get"): _READ_ONLY,
    ("src/apm_cli/commands/config.py", "list_config"): _READ_ONLY,
    ("src/apm_cli/commands/deps/cli.py", "deps"): _GROUP,
    ("src/apm_cli/commands/deps/cli.py", "info"): _READ_ONLY,
    ("src/apm_cli/commands/deps/cli.py", "list_packages"): _READ_ONLY,
    ("src/apm_cli/commands/deps/cli.py", "tree"): _READ_ONLY,
    ("src/apm_cli/commands/deps/why.py", "why"): _READ_ONLY,
    ("src/apm_cli/commands/discover.py", "discover"): _READ_ONLY,
    ("src/apm_cli/commands/doctor.py", "doctor"): _READ_ONLY,
    ("src/apm_cli/commands/experimental.py", "experimental"): _GROUP,
    ("src/apm_cli/commands/experimental.py", "list_flags"): _READ_ONLY,
    ("src/apm_cli/commands/export_patch.py", "export_patch"): (
        "writes patch files to a caller-chosen output only"
    ),
    ("src/apm_cli/commands/find.py", "find"): _READ_ONLY,
    ("src/apm_cli/commands/lifecycle.py", "lifecycle"): _GROUP,
    ("src/apm_cli/commands/lifecycle.py", "lifecycle_test"): _USER_SCRIPTS,
    ("src/apm_cli/commands/lifecycle.py", "lifecycle_validate"): _READ_ONLY,
    ("src/apm_cli/commands/list_cmd.py", "list"): _READ_ONLY,
    (_LOCK, "lock_export"): "reads the lockfile and writes an SBOM to a caller-chosen output",
    ("src/apm_cli/commands/marketplace/__init__.py", "browse"): _READ_ONLY,
    ("src/apm_cli/commands/marketplace/__init__.py", "list_cmd"): _READ_ONLY,
    ("src/apm_cli/commands/marketplace/__init__.py", "marketplace"): _GROUP,
    ("src/apm_cli/commands/marketplace/__init__.py", "search"): _READ_ONLY,
    ("src/apm_cli/commands/marketplace/audit.py", "audit"): _READ_ONLY,
    ("src/apm_cli/commands/marketplace/check.py", "check"): _READ_ONLY,
    ("src/apm_cli/commands/marketplace/outdated.py", "outdated"): _READ_ONLY,
    ("src/apm_cli/commands/marketplace/plugin/__init__.py", "package"): _GROUP,
    ("src/apm_cli/commands/marketplace/validate.py", "validate"): _READ_ONLY,
    ("src/apm_cli/commands/mcp.py", "list"): _READ_ONLY,
    ("src/apm_cli/commands/mcp.py", "mcp"): _GROUP,
    ("src/apm_cli/commands/mcp.py", "search"): _READ_ONLY,
    ("src/apm_cli/commands/mcp.py", "show"): _READ_ONLY,
    ("src/apm_cli/commands/outdated.py", "outdated"): _READ_ONLY,
    ("src/apm_cli/commands/pack.py", "pack_cmd"): "writes build artifacts to its output only",
    ("src/apm_cli/commands/plugin/__init__.py", "plugin"): _GROUP,
    ("src/apm_cli/commands/policy.py", "explain"): _READ_ONLY,
    ("src/apm_cli/commands/policy.py", "policy"): _GROUP,
    ("src/apm_cli/commands/policy.py", "status"): _READ_ONLY,
    ("src/apm_cli/commands/publish.py", "publish_cmd"): (
        "writes the publish archive and uploads it; no lifecycle state"
    ),
    ("src/apm_cli/commands/run.py", "preview"): _USER_SCRIPTS,
    ("src/apm_cli/commands/run.py", "run"): _USER_SCRIPTS,
    ("src/apm_cli/commands/runtime.py", "list"): _READ_ONLY,
    ("src/apm_cli/commands/runtime.py", "runtime"): _GROUP,
    ("src/apm_cli/commands/runtime.py", "status"): _READ_ONLY,
    ("src/apm_cli/commands/targets.py", "targets"): _READ_ONLY,
    ("src/apm_cli/commands/view.py", "view"): _READ_ONLY,
}

# Commands whose serialization is decided by another owner still in flight.
_PENDING: dict[_Command, str] = {}

_CLICK_COMMAND_DECORATOR = re.compile(r"(?:[A-Za-z_]\w*\.)*(?:command|group)(?:\(|$)")
_SERIALIZED_DECORATOR = re.compile(r"serialized_lifecycle(?:_unless|_when)?(?:\(|$)")


def _discover_commands(
    provider: FactsProvider, rule_id: str
) -> tuple[dict[_Command, tuple[int, bool]], list[Violation]]:
    """Return every Click callback as (path, name) -> (line, serialized)."""
    commands: dict[_Command, tuple[int, bool]] = {}
    failures: list[Violation] = []
    for path in _python_paths(provider, _SRC_PREFIX):
        facts, problems = _facts_for(provider, path, rule_id)
        if problems:
            failures.extend(problems)
            continue
        for definition in facts.definitions:
            if definition.kind == "class":
                continue
            if not any(_CLICK_COMMAND_DECORATOR.match(item) for item in definition.decorators):
                continue
            serialized = any(_SERIALIZED_DECORATOR.match(item) for item in definition.decorators)
            commands[(path, definition.name)] = (definition.line, serialized)
    return commands, failures


def _is_serialized_route(command: _Command) -> bool:
    path, name = command
    return name in SERIALIZED_LIFECYCLE_ROUTES.get(path, {})


def check_lifecycle_serialization_closed_world(provider: FactsProvider) -> tuple[Violation, ...]:
    """Every CLI command is classified once against lifecycle serialization."""
    rule_id = GUARD_LIFECYCLE_SERIALIZATION_CLOSED
    commands, findings = _discover_commands(provider, rule_id)

    for command, (line, serialized) in sorted(commands.items()):
        path, name = command
        classes = [
            label
            for label, member in (
                ("serialized", _is_serialized_route(command)),
                ("delegated", command in _DELEGATED),
                ("exempt", command in _EXEMPT),
                ("pending", command in _PENDING),
            )
            if member
        ]
        if not classes:
            findings.append(
                _summary(
                    rule_id,
                    path,
                    f"CLI command {name} (line {line}) is not classified for lifecycle "
                    "serialization; route it through install/locking.py and list it, or "
                    "record why it writes no lifecycle state",
                )
            )
        elif len(classes) > 1:
            findings.append(
                _summary(
                    rule_id,
                    path,
                    f"CLI command {name} is classified more than once: {', '.join(classes)}",
                )
            )
        if serialized and not _is_serialized_route(command):
            findings.append(
                _summary(
                    rule_id,
                    path,
                    f"serialized CLI command {name} must be listed in SERIALIZED_LIFECYCLE_ROUTES",
                )
            )

    for label, table in (("delegated", _DELEGATED), ("exempt", _EXEMPT), ("pending", _PENDING)):
        for command in sorted(table):
            if command not in commands:
                path, name = command
                findings.append(
                    _summary(
                        rule_id,
                        path,
                        f"stale {label} lifecycle classification: no CLI command {name}",
                    )
                )

    for command, routes in sorted(_DELEGATED.items()):
        for route in routes:
            if not _is_serialized_route(route):
                findings.append(
                    _summary(
                        rule_id,
                        command[0],
                        f"{command[1]} delegates to {route[1]} in {route[0]}, which is not "
                        "a serialized lifecycle route",
                    )
                )
    return tuple(findings)
