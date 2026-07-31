"""I/O helpers and trust-context builder for the executable-gate subsystem.

Extracted from ``executables.py`` to keep that facade under the 800-line
guardrail. All public names are re-exported from ``executables.py`` so
callers see no change.

Monkeypatch seam preservation: test suites patch ``executables._user_config_file``
and ``executables._legacy_approvals_path`` via ``monkeypatch.setattr(ex, ...)``.
The private proxy functions ``_user_config_file()`` and ``_legacy_approvals_path()``
below route every call through the ``executables`` module so those patches
are still intercepted.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any

from ._trust_helpers import normalize_bin_deploy_deny_key
from ._types import ALL_EXEC_TYPES, ExecTrustContext

# ---------------------------------------------------------------------------
# Test-seam proxies: route through executables module so monkeypatching works
# ---------------------------------------------------------------------------


def _user_config_file() -> Path:
    """Proxy for executables._user_config_file -- preserves monkeypatch seam."""
    from . import executables as _e

    return _e._user_config_file()


def _legacy_approvals_path() -> Path:
    """Proxy for executables._legacy_approvals_path -- preserves monkeypatch seam."""
    from . import executables as _e

    return _e._legacy_approvals_path()


# ---------------------------------------------------------------------------
# Grant-block parser (private helper)
# ---------------------------------------------------------------------------


def _parse_grant_block(
    raw: Any,
    *,
    where: str,
) -> dict[str, dict[str, bool]]:
    """Validate and normalise a ``{package_key: {exec_type: bool}}`` map."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(
            f"{where} must be a mapping of package keys to "
            "{hooks: bool, mcp: bool, bin: bool, canvas: bool}"
        )
    result: dict[str, dict[str, bool]] = {}
    for pkg_key, entry in raw.items():
        if not isinstance(pkg_key, str):
            raise ValueError(f"{where} key must be a string, got {type(pkg_key).__name__}")
        if not isinstance(entry, dict):
            raise ValueError(
                f"{where}[{pkg_key!r}] must be a mapping of exec types to "
                f"booleans, got {type(entry).__name__}"
            )
        parsed: dict[str, bool] = {}
        for exec_type, value in entry.items():
            exec_type_str = str(exec_type)
            if exec_type_str not in ALL_EXEC_TYPES:
                raise ValueError(
                    f"{where}[{pkg_key!r}]: unknown exec type {exec_type_str!r} "
                    f"(valid: {', '.join(ALL_EXEC_TYPES)})"
                )
            if not isinstance(value, bool):
                raise ValueError(
                    f"{where}[{pkg_key!r}][{exec_type_str!r}] must be a boolean, "
                    f"got {type(value).__name__}"
                )
            parsed[exec_type_str] = value
        result[pkg_key] = parsed
    return result


# ---------------------------------------------------------------------------
# Project executables block (parse / gate check / load / write)
# ---------------------------------------------------------------------------


def parse_project_executables(
    data: dict[str, Any],
) -> tuple[dict[str, dict[str, bool]], dict[str, dict[str, bool]], bool]:
    """Parse the project ``executables`` block from raw apm.yml data.

    Returns ``(allow, deny, used_deprecated_alias)``. The deprecated
    ``allowExecutables`` block is folded into ``allow`` (the new
    ``executables.allow`` wins on a per-key conflict); when it is present the
    boolean flag is ``True`` so callers can emit one deprecation warning.
    """
    used_alias = False
    allow: dict[str, dict[str, bool]] = {}

    alias_raw = data.get("allowExecutables")
    if alias_raw is not None:
        used_alias = True
        allow.update(_parse_grant_block(alias_raw, where="allowExecutables"))

    deny: dict[str, dict[str, bool]] = {}
    block = data.get("executables")
    if block is not None:
        if not isinstance(block, dict):
            raise ValueError("executables must be a mapping with 'allow' and/or 'deny' keys")
        allow.update(_parse_grant_block(block.get("allow"), where="executables.allow"))
        deny = _parse_grant_block(block.get("deny"), where="executables.deny")

    return allow, deny, used_alias


def project_executables_gate_enabled(data: dict[str, Any]) -> bool:
    """Return True when the project opts into the gate (any block present)."""
    return data.get("executables") is not None or data.get("allowExecutables") is not None


def load_project_executables(
    manifest_path: Path,
) -> tuple[dict[str, dict[str, bool]], dict[str, dict[str, bool]], bool]:
    """Read the project ``executables`` block (and alias) from ``apm.yml``."""
    from ..utils.yaml_io import load_yaml

    if not manifest_path.is_file():
        return {}, {}, False
    data = load_yaml(manifest_path)
    if not isinstance(data, dict):
        return {}, {}, False
    return parse_project_executables(data)


def write_project_executables(
    manifest_path: Path,
    allow: dict[str, dict[str, bool]],
    deny: dict[str, dict[str, bool]],
) -> None:
    """Persist project ``executables: {allow, deny}`` back to ``apm.yml``.

    Migrates a legacy ``allowExecutables`` block into ``executables.allow`` on
    write so a project converges on the unified noun. Empty ``allow``/``deny``
    sub-blocks are omitted; an empty ``executables: {}`` is still written when
    the gate was already opted-in so the signal is not lost.
    """
    from ..utils.yaml_io import dump_yaml, load_yaml

    data = load_yaml(manifest_path)
    if not isinstance(data, dict):
        return

    had_alias = data.pop("allowExecutables", None) is not None
    block: dict[str, Any] = {}
    if allow:
        block["allow"] = allow
    if deny:
        block["deny"] = deny

    if block or had_alias or "executables" in data:
        data["executables"] = block
    dump_yaml(data, manifest_path)


# ---------------------------------------------------------------------------
# User consent store (load / save / migrate legacy)
# ---------------------------------------------------------------------------


def _migrate_legacy_approvals(allow: dict[str, dict[str, bool]]) -> dict[str, dict[str, bool]]:
    """Fold a legacy ``approvals.yml`` into *allow* and delete the file.

    The legacy file stored a bare ``{package_key: {exec_type: bool}}`` map of
    grants. Existing config entries win over legacy entries on conflict.
    """
    legacy = _legacy_approvals_path()
    if not legacy.is_file():
        return allow
    from ..utils.yaml_io import load_yaml

    legacy_data = load_yaml(legacy)
    if isinstance(legacy_data, dict):
        for pkg_key, entry in legacy_data.items():
            if isinstance(entry, dict):
                merged = {**{k: bool(v) for k, v in entry.items()}, **allow.get(pkg_key, {})}
                allow[pkg_key] = merged
    with contextlib.suppress(OSError):
        legacy.unlink()
    return allow


def load_user_executables() -> tuple[dict[str, dict[str, bool]], dict[str, dict[str, bool]]]:
    """Load personal executable consent from ``~/.apm/config.json``.

    Returns ``(allow, deny)``. On first read, any legacy
    ``~/.apm/approvals.yml`` is folded into ``allow`` and deleted, and the
    migrated state is persisted back to the config so the fold happens once.
    """
    cfg_path = _user_config_file()
    cfg: dict[str, Any] = {}
    if cfg_path.is_file():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError):
            cfg = {}
    section = cfg.get("executables") if isinstance(cfg.get("executables"), dict) else {}
    allow = dict(section.get("allow") or {})
    deny = dict(section.get("deny") or {})

    migrated = _migrate_legacy_approvals(allow)
    if migrated != allow or (allow and "executables" not in cfg):
        allow = migrated
        save_user_executables(allow, deny)
    else:
        allow = migrated
    return allow, deny


def save_user_executables(
    allow: dict[str, dict[str, bool]],
    deny: dict[str, dict[str, bool]],
) -> None:
    """Persist personal executable consent into ``~/.apm/config.json``.

    The config file is written owner-only (``0o600``) to keep the consent
    list private on shared systems. The write is atomic (``atomic_write_text``
    with ``new_file_mode=0o600``) so a crash mid-write cannot corrupt the
    shared config and a freshly-created file is never world-readable.
    """
    from ..utils.atomic_io import atomic_write_text

    cfg_path = _user_config_file()
    cfg: dict[str, Any] = {}
    if cfg_path.is_file():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError):
            cfg = {}
    cfg["executables"] = {"allow": allow, "deny": deny}
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(cfg_path, json.dumps(cfg, indent=2), new_file_mode=0o600)


# ---------------------------------------------------------------------------
# Trust-context builder
# ---------------------------------------------------------------------------


def build_exec_trust_context(
    *,
    policy: Any | None,
    project_data: dict[str, Any] | None,
) -> ExecTrustContext:
    """Assemble an :class:`ExecTrustContext` from org / project / user inputs.

    Args:
        policy: The merged org :class:`~apm_cli.policy.schema.ApmPolicy`
            (or ``None`` when no policy applies).
        project_data: Raw project ``apm.yml`` data (or ``None``).

    The gate is enabled when ANY layer opts in: the project declares an
    ``executables``/``allowExecutables`` block (even empty), or the org policy
    carries a non-empty ``executables`` block, or a legacy ``bin_deploy`` deny.
    """
    data = project_data or {}
    project_allow, project_deny, _alias = parse_project_executables(data)
    user_allow, user_deny = load_user_executables()

    org_deny_all = False
    org_deny: frozenset[str] = frozenset()
    org_require: frozenset[str] = frozenset()
    org_recommend: frozenset[str] = frozenset()
    org_enforce: frozenset[str] = frozenset()
    org_bin_deny_all = False
    org_bin_deny: frozenset[str] = frozenset()
    org_signal = False

    if policy is not None:
        execs = getattr(policy, "executables", None)
        if execs is not None:
            org_deny_all = bool(getattr(execs, "deny_all", False))
            org_deny = frozenset(getattr(execs, "deny", ()) or ())
            org_require = frozenset(getattr(execs, "require", ()) or ())
            org_recommend = frozenset(getattr(execs, "recommend", ()) or ())
            org_enforce = frozenset(getattr(execs, "enforce", ()) or ())
            org_signal = bool(
                org_deny_all or org_deny or org_require or org_recommend or org_enforce
            )
        bin_deploy = getattr(policy, "bin_deploy", None)
        if bin_deploy is not None:
            org_bin_deny_all = bool(getattr(bin_deploy, "deny_all", False))
            org_bin_deny = frozenset(
                normalize_bin_deploy_deny_key(entry)
                for entry in (getattr(bin_deploy, "deny", ()) or ())
            )
            org_signal = org_signal or org_bin_deny_all or bool(org_bin_deny)

    gate_enabled = project_executables_gate_enabled(data) or org_signal

    return ExecTrustContext(
        gate_enabled=gate_enabled,
        org_deny_all=org_deny_all,
        org_deny=org_deny,
        org_require=org_require,
        org_recommend=org_recommend,
        org_enforce=org_enforce,
        org_bin_deny_all=org_bin_deny_all,
        org_bin_deny=org_bin_deny,
        project_allow=project_allow,
        project_deny=project_deny,
        user_allow=user_allow,
        user_deny=user_deny,
    )
