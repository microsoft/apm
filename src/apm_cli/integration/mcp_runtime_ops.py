"""Runtime-facing MCP operations extracted from :class:`MCPIntegrator`.

``collect_transitive`` and ``install_for_runtime`` reference no module global
that is monkeypatched on ``mcp_integrator``; ``gate_project_scoped_runtimes``
routes its single ``Path.cwd()`` through ``mcp_integrator`` so the patched
``Path`` is honored.  ``MCPIntegrator`` keeps thin delegating staticmethods so
the heavily-patched ``MCPIntegrator.<name>`` call/patch surface is unchanged.
"""

import logging
from pathlib import Path

from apm_cli.core.null_logger import NullCommandLogger
from apm_cli.integration._shared import resolve_locked_apm_yml_paths
from apm_cli.utils.console import _rich_error, _rich_info

_log = logging.getLogger(__name__)


def collect_transitive(
    apm_modules_dir: Path,
    lock_path: Path | None = None,
    trust_private: bool = False,
    logger=None,
    diagnostics=None,
) -> list:
    """Collect MCP dependencies from resolved APM packages listed in apm.lock.

    Only scans apm.yml files for packages present in apm.lock to avoid
    picking up stale/orphaned packages from previous installs.
    Falls back to scanning all apm.yml files if no lock file is available.

    Self-defined servers (registry: false) from direct dependencies
    (depth == 1) are auto-trusted.  Self-defined servers from transitive
    dependencies (depth > 1) are skipped with a warning unless
    *trust_private* is True.
    """
    if logger is None:
        logger = NullCommandLogger()
    if not apm_modules_dir.exists():
        return []

    from apm_cli.models.apm_package import APMPackage

    # Build set of expected apm.yml paths from apm.lock
    resolved, direct_paths = resolve_locked_apm_yml_paths(apm_modules_dir, lock_path)
    apm_yml_paths = resolved if resolved is not None else apm_modules_dir.rglob("apm.yml")

    collected = []
    for apm_yml_path in apm_yml_paths:
        try:
            pkg = APMPackage.from_apm_yml(apm_yml_path)
            mcp = pkg.get_mcp_dependencies()
            if mcp:
                is_direct = apm_yml_path.resolve() in direct_paths
                for dep in mcp:
                    if hasattr(dep, "is_self_defined") and dep.is_self_defined:
                        if is_direct:
                            logger.progress(
                                f"Trusting direct dependency MCP '{dep.name}' from '{pkg.name}'"
                            )
                        elif trust_private:
                            logger.progress(
                                f"Trusting self-defined MCP server '{dep.name}' "
                                f"from transitive package '{pkg.name}' (--trust-transitive-mcp)"
                            )
                        else:
                            _trust_msg = (
                                f"Transitive package '{pkg.name}' declares self-defined "
                                f"MCP server '{dep.name}' (registry: false). "
                                f"Re-declare it in your apm.yml or use --trust-transitive-mcp."
                            )
                            if diagnostics:
                                diagnostics.warn(_trust_msg)
                            else:
                                logger.warning(_trust_msg)
                            continue
                    collected.append(dep)
        except Exception:
            _log.debug(
                "Skipping package at %s: failed to parse apm.yml",
                apm_yml_path,
                exc_info=True,
            )
            continue
    return collected


def install_for_runtime(
    runtime: str,
    mcp_deps: list[str],
    shared_env_vars: dict = None,  # noqa: RUF013
    server_info_cache: dict = None,  # noqa: RUF013
    shared_runtime_vars: dict = None,  # noqa: RUF013
    project_root=None,
    user_scope: bool = False,
    logger=None,
) -> bool:
    """Install MCP dependencies for a specific runtime.

    Returns True if all deps were configured successfully, False otherwise.
    """
    if logger is None:
        logger = NullCommandLogger()
    try:
        from apm_cli.core.operations import install_package

        all_ok = True
        for dep in mcp_deps:
            logger.verbose_detail(f"  Installing {dep}...")
            try:
                result = install_package(
                    runtime,
                    dep,
                    shared_env_vars=shared_env_vars,
                    server_info_cache=server_info_cache,
                    shared_runtime_vars=shared_runtime_vars,
                    project_root=project_root,
                    user_scope=user_scope,
                )
                if result["failed"]:
                    logger.error(f"  Failed to install {dep}")
                    all_ok = False
                elif logger and runtime == "codex":
                    from apm_cli.factory import ClientFactory

                    config_path = ClientFactory.create_client(
                        runtime,
                        project_root=project_root,
                        user_scope=user_scope,
                    ).get_config_path()
                    _log.debug("Codex config written to %s", config_path)
                    logger.verbose_detail(f"  Codex config: {config_path}")
            except Exception as install_error:
                _log.debug(
                    "Failed to install MCP dep %s for runtime %s",
                    dep,
                    runtime,
                    exc_info=True,
                )
                logger.error(f"  Failed to install {dep}: {install_error}")
                all_ok = False

        # Emit aggregated post-install diagnostics for runtimes that
        # support runtime env-var substitution (currently Copilot CLI).
        # Safe no-op for runtimes whose adapter doesn't aggregate state.
        try:
            if runtime == "copilot":
                from apm_cli.adapters.client.copilot import CopilotClientAdapter

                CopilotClientAdapter.emit_install_run_summary()
        except Exception:
            _log.debug("Failed to emit install-run summary", exc_info=True)

        return all_ok

    except ImportError as e:
        logger.warning(f"Core operations not available for runtime {runtime}: {e}")
        logger.progress(f"Dependencies for {runtime}: {', '.join(mcp_deps)}")
        return False
    except ValueError as e:
        from apm_cli.factory import ClientFactory

        supported_runtimes = ", ".join(sorted(ClientFactory.supported_clients()))
        logger.warning(f"Runtime {runtime} not supported: {e}")
        logger.progress(f"Supported runtimes: {supported_runtimes}")
        return False
    except Exception as e:
        _log.debug("Unexpected error installing for runtime %s", runtime, exc_info=True)
        logger.error(f"Error installing for runtime {runtime}: {e}")
        return False


def gate_project_scoped_runtimes(
    target_runtimes: list[str],
    *,
    user_scope: bool,
    project_root,
    apm_config: dict | None,
    explicit_target: str | list[str] | None,
) -> list[str]:
    """Filter *target_runtimes* against the project's active targets.

    UX parity with ``apm install`` for apm dependencies: the active
    target set (explicit ``--target`` > ``targets:`` field >
    directory-signal detection) is the whitelist for MCP writes. Any
    runtime outside that set is skipped with an info line naming both
    what was dropped and the active set, so users can audit the
    decision input without re-reading apm.yml (#1335).

    Strict resolution model -- mirrors :func:`resolve_targets`,
    the same call ``apm install`` uses
    (``install/phases/targets.py:233``):

      - flag > yaml-targets > directory signals (no permissive
        "fallback to copilot" greenfield default);
      - no flag, no ``targets:``, and no harness-signal directory ->
        :class:`NoHarnessError` (red ``[x]``, write nothing);
      - multiple ambiguous signals with no disambiguation ->
        :class:`AmbiguousHarnessError` (same fail-closed shape).

    ``explicit_target`` accepts ``str``, ``list[str]``, or a CSV
    string (``"claude,copilot"``) -- the latter is produced by
    legacy callers; it is normalized to a list before the resolver
    is invoked so the canonical-name validator does not reject it as
    one unknown token.

    A malformed ``targets:`` field (conflicting ``target:`` +
    ``targets:``, ``targets: []``, or unknown canonical name) likewise
    fails closed: nothing is written.

    Exit semantics differ deliberately from ``install/phases/targets.py``:
    the canonical install phase calls ``raise SystemExit(2)`` when
    resolution fails; this gate may be invoked mid-bundle (see
    ``install/local_bundle_handler``) where a hard exit would corrupt
    partial state, so we render the same red ``[x]`` voice and return
    an empty list (fail-closed-continue).

    ``user_scope=True`` is a deliberate carve-out: user-scope writes
    target ``~/.config`` paths the user owns globally, so the
    project-level whitelist is irrelevant. Documented in the
    consumer install-mcp-servers guide.
    """
    if user_scope:
        return target_runtimes

    from apm_cli.core.apm_yml import (
        ConflictingTargetsError,
        EmptyTargetsListError,
        UnknownTargetError,
        parse_targets_field,
    )
    from apm_cli.core.errors import (
        AmbiguousHarnessError,
        NoHarnessError,
    )
    from apm_cli.core.target_detection import resolve_targets
    from apm_cli.integration.targets import RUNTIME_TO_CANONICAL_TARGET

    # --- step 1: parse declared targets (fail-closed on any invalid form)
    yaml_targets: list[str] | None = None
    if apm_config:
        try:
            parsed = parse_targets_field(apm_config)
            yaml_targets = parsed if parsed else None
        except (
            ConflictingTargetsError,
            EmptyTargetsListError,
            UnknownTargetError,
        ) as exc:
            # Voice mirrors the canonical `apm install` skills phase
            # (install/phases/targets.py:213): red [x] lead-with-outcome,
            # then the structured error body. symbol="" suppresses the
            # auto-prefix on the body because the exception text already
            # begins with "[x] ..." (see core/errors.py).
            _rich_error(
                "Skipping all MCP config writes -- apm.yml 'targets' field is invalid.",
                symbol="error",
            )
            _rich_error(str(exc), symbol="")
            _log.debug(
                "parse_targets_field failed; failing closed (no MCP writes)",
                exc_info=True,
            )
            return []

    # --- step 2: normalize CSV explicit_target sugar to a list -----
    # `_wire_bundle_mcp_servers` historically passes a CSV string; the
    # canonical-name validator inside _resolve_targets_v2 would reject
    # the whole CSV as one unknown token. Normalize first.
    flag: str | list[str] | None
    if isinstance(explicit_target, str) and "," in explicit_target:
        flag = [t.strip() for t in explicit_target.split(",") if t.strip()]
    else:
        flag = explicit_target

    # Apply the runtime->canonical-target alias BEFORE passing the flag
    # to resolve_targets. The canonical-name validator inside the
    # resolver only knows about CANONICAL_TARGETS (claude/copilot/...);
    # it rejects runtime aliases (vscode/agents) as unknown tokens.
    # The MCP gate, however, must accept those aliases because users
    # naturally type `--target vscode` for the VS Code Copilot runtime.
    if flag is not None:
        tokens = [flag] if isinstance(flag, str) else list(flag)
        flag = [RUNTIME_TO_CANONICAL_TARGET.get(t, t) for t in tokens]

    # --- step 3: delegate to the canonical v2 resolver -------------
    # This is the same call the `apm install` skills phase makes at
    # install/phases/targets.py:233. It enforces the strict
    # flag > yaml > signals chain and raises NoHarnessError /
    # AmbiguousHarnessError on greenfield / under-disambiguated
    # projects -- the ASYMMETRY closed by this PR is that the gate
    # used to silently fall back to [copilot] in those cases.
    from apm_cli.integration import mcp_integrator as _mi

    root = project_root or _mi.Path.cwd()
    try:
        resolved = resolve_targets(root, flag=flag, yaml_targets=yaml_targets)
    except (NoHarnessError, AmbiguousHarnessError) as exc:
        _rich_error(
            "Skipping all MCP config writes -- could not resolve active targets.",
            symbol="error",
        )
        _rich_error(str(exc), symbol="")
        _log.debug(
            "resolve_targets failed; failing closed (no MCP writes)",
            exc_info=True,
        )
        return []

    active = set(resolved.targets)

    # Runtime name "vscode" maps to canonical target "copilot" (same
    # alias active_targets honors); shared table prevents drift with
    # the alias resolution in integration/targets.py.
    out = [rt for rt in target_runtimes if RUNTIME_TO_CANONICAL_TARGET.get(rt, rt) in active]
    dropped = sorted(set(target_runtimes) - set(out))
    if dropped:
        # Mirror the canonical `Targets: X  (source: Y)` provenance shape
        # (install/phases/targets.py:265, core/target_detection.py:777):
        # double-space before the parenthetical. The "or '<none>'" guard is
        # defensive -- an empty active set is unreachable when
        # _resolve_targets_v2 succeeded, but if a future contract change
        # widens that contract we surface "<none>" rather than render
        # "(active targets: )" which reads as a renderer bug.
        active_csv = ", ".join(sorted(active)) or "<none>"
        _rich_info(
            f"Skipped MCP config for {', '.join(dropped)}  (active targets: {active_csv})",
            symbol="info",
        )
        _log.debug(
            "Active-targets gate dropped: %s (active=%s)",
            dropped,
            sorted(active),
        )
    return out
