"""Scanner-agnostic options + precedence resolution for external scanners.

A single, vendor-neutral value object (:class:`ScannerOptions`) carries the two
configurable concerns an external SARIF scanner may honour:

* ``llm``        -- opt into a scanner's LLM-powered analysis mode (richer
  findings, but network egress + API-key use). Tri-state ``bool | None`` where
  ``None`` means "adapter default".
* ``extra_args`` -- additional, *allowlist-validated* argv tokens appended to
  the scanner's base command before positional targets.

:func:`resolve_scanner_options` is a pure function (no I/O) that folds the
CLI / config / policy layers into one ``ScannerOptions`` following APM's
precedence ladder.  Crucially, **policy never contributes argv tokens or forces
LLM mode** -- it may only *restrict* (``allow_args=False`` strips all
user/CLI args).  This keeps a malicious project-level ``apm-policy.yml`` from
injecting argv or forcing outbound network egress.

:func:`validate_extra_args` is the security gate for the passthrough surface:
each adapter declares a small allowlist of safe flag prefixes, and any token
that is not allowed, names a secret, or is a path escaping the scan root is
rejected fail-closed (raising :class:`ExternalScanError`).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .base import ExternalScanError


@dataclass(frozen=True)
class ScannerOptions:
    """Resolved, vendor-neutral options handed to an adapter's ``scan``.

    Attributes:
        llm: ``True`` forces LLM-powered analysis on, ``False`` forces it off,
            ``None`` lets the adapter pick its own (offline) default.
        extra_args: Already-validated extra argv tokens (an adapter appends
            these verbatim; validation happens up front via
            :func:`validate_extra_args`).
    """

    llm: bool | None = None
    extra_args: tuple[str, ...] = field(default_factory=tuple)


def resolve_scanner_options(
    *,
    cli_llm: bool | None,
    cli_args: tuple[str, ...] | None,
    config_llm: bool | None,
    config_args: tuple[str, ...] | None,
    policy_allow_args: bool | None,
) -> ScannerOptions:
    """Fold CLI / config / policy layers into one :class:`ScannerOptions`.

    Precedence (high to low):

    * ``llm``  -- CLI (global, this run) > config (per-scanner) > ``None``
      (adapter default). Policy never forces LLM on (restrict-only stance).
    * ``args`` -- if ``policy_allow_args`` is ``False`` the result is ``()``
      (org kill-switch wins); otherwise CLI args (if any) > config args.
      Policy never contributes argv tokens.

    Args:
        cli_llm: ``--external-llm/--no-external-llm`` value, or ``None`` if the
            flag was not passed.
        cli_args: ``--external-args`` tokens, or ``None`` if the flag was not
            passed (``()`` means "passed but empty").
        config_llm: ``external.<name>.llm`` from config, or ``None``.
        config_args: ``external.<name>.args`` from config, or ``None``.
        policy_allow_args: ``security.audit.scanners.<name>.allow_args`` from the
            resolved policy. ``False`` forbids all passthrough; ``None``/``True``
            permit it.

    Returns:
        A :class:`ScannerOptions` with the resolved ``llm`` and (un-validated)
        ``extra_args``. Callers validate ``extra_args`` against the adapter's
        allowlist before use.
    """
    llm = cli_llm if cli_llm is not None else config_llm

    if policy_allow_args is False:
        extra_args: tuple[str, ...] = ()
    else:
        chosen = cli_args if cli_args is not None else config_args
        extra_args = tuple(chosen or ())

    return ScannerOptions(llm=llm, extra_args=extra_args)


def _is_secret_token(token: str) -> bool:
    """Return whether *token* names or carries a credential."""
    from ...core.plugin_manifest import _SECRET_FLAG_NAME_RE, _redact_secret_values

    name = token.split("=", 1)[0]
    if _SECRET_FLAG_NAME_RE.match(name):
        return True
    _, changed = _redact_secret_values(token)
    return changed


def _value_escapes_root(value: str, base_dir: Path) -> bool:
    """Return whether a non-flag *value* is a path escaping *base_dir*.

    Bare values with no path separators (e.g. ``gpt-4o``) are never paths.
    Absolute paths, parent-traversal, or anything resolving outside the scan
    root are rejected.
    """
    if not value:
        return False
    looks_pathy = os.sep in value or (os.altsep is not None and os.altsep in value)
    if not (looks_pathy or value.startswith("..") or os.path.isabs(value)):
        return False
    from ...utils.path_security import ensure_path_within

    try:
        ensure_path_within(base_dir / value, base_dir)
    except (ValueError, OSError):
        return True
    return False


def validate_extra_args(
    name: str,
    args: tuple[str, ...],
    allowed_prefixes: frozenset[str],
    *,
    base_dir: Path,
) -> tuple[str, ...]:
    """Validate passthrough *args* against an adapter allowlist (fail-closed).

    A token is rejected when it (a) names or carries a secret, (b) is a flag
    whose name is not in *allowed_prefixes*, or (c) is a value that resolves to
    a path outside *base_dir*.  ``shell=False`` already prevents shell
    injection; this guards against scanner-*native* dangerous flags
    (``--output``, ``--config <url>``, ``--plugin``) and credential leakage.

    Args:
        name: Scanner name, for error messages.
        args: Candidate argv tokens (from CLI or config).
        allowed_prefixes: Flag names the adapter accepts (e.g. ``{"--model"}``).
        base_dir: Scan root; value tokens may not escape it.

    Returns:
        The validated *args* unchanged.

    Raises:
        ExternalScanError: On the first disallowed token, with an actionable
            message. No partial command is ever built.
    """
    for token in args:
        if _is_secret_token(token):
            raise ExternalScanError(
                f"External scanner '{name}': refusing to pass a credential-bearing "
                f"argument. Set API keys via environment variables, not flags."
            )
        if token.startswith("-"):
            flag_name = token.split("=", 1)[0]
            if flag_name not in allowed_prefixes:
                allowed = ", ".join(sorted(allowed_prefixes)) or "(none)"
                raise ExternalScanError(
                    f"External scanner '{name}': argument '{flag_name}' is not "
                    f"allowed. Permitted flags: {allowed}."
                )
            if "=" in token and _value_escapes_root(token.split("=", 1)[1], base_dir):
                raise ExternalScanError(
                    f"External scanner '{name}': argument '{flag_name}' value "
                    f"must stay within the scan directory."
                )
        elif _value_escapes_root(token, base_dir):
            raise ExternalScanError(
                f"External scanner '{name}': path argument '{token}' must stay "
                f"within the scan directory."
            )
    return args
