"""Plugin manifest builder -- generates ``plugin.json`` for each target ecosystem.

Triggered when ``apm.yml`` declares a ``target:`` (or ``targets:``) field containing
``claude``, ``copilot``, or ``codex``.

Supported ecosystems and their output paths
-------------------------------------------
* ``claude``  -> ``.claude-plugin/plugin.json``
* ``copilot`` -> ``.github/plugin/plugin.json``
* ``codex``   -> ``.codex-plugin/plugin.json``

Only canonical targets that survive :func:`apm_cli.core.apm_yml.parse_targets_field`
validation are listed here -- there is exactly one source of truth for valid
ecosystem names (``CANONICAL_TARGETS``), so this module never declares aliases
that the parser would reject before the producer runs.

The builder delegates all heavy lifting to the existing
:func:`apm_cli.deps.plugin_parser.synthesize_plugin_json_from_apm_yml` helper and
the :func:`collect_mcp_servers` local utility -- it adds only the per-ecosystem
routing and the final write step.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from apm_cli.core.scope import manifest_file

from ..utils.atomic_io import write_text_lf
from ..utils.console import _rich_info, _rich_success, _rich_warning
from ..utils.path_security import ensure_path_within
from .remediation import PLUGIN_MCP_OWNERSHIP_UNKNOWN

# Messages already shown inside one ``plugin_manifest_notes_once`` scope.
_EMITTED_NOTES: ContextVar[set[tuple[str, str]] | None] = ContextVar(
    "apm_plugin_manifest_emitted_notes", default=None
)


@contextmanager
def plugin_manifest_notes_once() -> Iterator[None]:
    """Show each plugin-manifest note once while the block runs.

    One ``apm pack`` builds a manifest per ecosystem and for the bundle; each
    build reads the same ``.mcp.json`` and would repeat the same note (#4291).
    """
    token = _EMITTED_NOTES.set(set())
    try:
        yield
    finally:
        _EMITTED_NOTES.reset(token)


def _emit(level: str, message: str, logger: Any, symbol: str) -> None:
    """Dispatch a user-facing message to *logger* if present, else the console.

    Collapses the repeated ``if logger: logger.X(msg) else: _rich_X(msg)``
    branch that every status line in this module would otherwise duplicate.
    *level* is one of ``"info"``, ``"warning"``, or ``"success"`` -- each maps to
    the identically-named :class:`~apm_cli.core.command_logger.CommandLogger`
    method (all three accept a ``symbol`` keyword) and to the matching
    ``_rich_*`` console helper. *symbol* is a key into
    :data:`apm_cli.utils.console.STATUS_SYMBOLS` and is forwarded on both paths
    so a success line renders as ``[+]`` (not ``[i]``) regardless of caller.
    Inside :func:`plugin_manifest_notes_once` a repeated message is dropped.
    """
    emitted = _EMITTED_NOTES.get()
    if emitted is not None:
        if (level, message) in emitted:
            return
        emitted.add((level, message))
    if logger is not None:
        getattr(logger, level)(message, symbol=symbol)
        return
    _console = {"info": _rich_info, "warning": _rich_warning, "success": _rich_success}[level]
    _console(message, symbol=symbol)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PLUGIN_MANIFEST_ECOSYSTEMS: frozenset[str] = frozenset({"claude", "copilot", "codex"})
"""Target names that trigger plugin manifest generation.

Every entry MUST also be a member of
:data:`apm_cli.core.apm_yml.CANONICAL_TARGETS`; otherwise
:func:`apm_cli.core.apm_yml.parse_targets_field` rejects the token with
``UnknownTargetError`` before this module is ever reached, leaving the path
permanently dead.
"""

PLUGIN_ECOSYSTEM_PATHS: dict[str, str] = {
    "claude": ".claude-plugin/plugin.json",
    "copilot": ".github/plugin/plugin.json",
    "codex": ".codex-plugin/plugin.json",
}
"""Output path (relative to project root) for each ecosystem's ``plugin.json``."""

_CODEX_PLUGIN_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def is_safe_codex_plugin_name(name: object) -> bool:
    """Return True when *name* is a plugin name APM's Codex route accepts."""
    return isinstance(name, str) and _CODEX_PLUGIN_NAME_RE.fullmatch(name) is not None


@dataclass(frozen=True)
class PluginManifestWriteResult:
    """Describe the canonical plugin-manifest write decision."""

    path: Path | None
    action: Literal["written", "skipped", "dry_run"]


# Server-object keys that may carry live credentials. Any key whose lowercased
# name matches an entry here (or contains one of the substrings below) is
# stripped before the manifest is serialised -- a committed plugin.json must
# never leak secrets that were resolved at MCP-host startup from .mcp.json.
_SENSITIVE_MCP_KEY_NAMES: frozenset[str] = frozenset(
    {"env", "environment", "headers", "authorization"}
)
# The bare ``key`` substring intentionally over-matches (it strips ``accessKey``,
# ``privateKey``, ``signingKey``, and similar camelCase credential fields). A
# tighter word-boundary rule would MISS those real secrets; the only cost of the
# broad rule is dropping an innocuously-named field, which is safe -- a sanitiser
# must err toward over-stripping, never toward leaking.
_SENSITIVE_MCP_KEY_SUBSTRINGS: tuple[str, ...] = (
    "token",
    "secret",
    "password",
    "credential",
    "apikey",
    "key",
)

# Secret-bearing VALUE patterns, redacted regardless of the key that holds them.
# ``.mcp.json`` secrets hide in shapes the key name cannot reveal: URL userinfo,
# inline ``--token=`` flags, space-separated ``--token VALUE`` pairs, shell
# ``ENV=secret`` prefixes, ``Authorization: Bearer ...`` headers, and bare
# provider tokens passed as positional args. Each surviving string is scrubbed
# against all of these so a positional or split secret cannot slip through.
_URL_USERINFO_RE = re.compile(r"\b([a-zA-Z][\w+.-]*://)([^/?#\s@]+)@")
_INLINE_SECRET_ARG_RE = re.compile(
    r"(--?[\w.-]*(?:token|secret|password|credential|apikey|key)[\w.-]*=)(\S+)",
    re.IGNORECASE,
)
# Space-separated ``--token VALUE`` carried inside ONE string, e.g. a whole
# command line ``npx server --token sk-abc`` or a single args element
# ``"--token sk-abc"``. The list-context lookahead only fires when the flag and
# value are SEPARATE array elements; this catches the single-string shape.
_SPACE_SECRET_ARG_RE = re.compile(
    r"(--?[\w.-]*(?:token|secret|password|credential|apikey|api-key|key)[\w.-]*\s+)(\S+)",
    re.IGNORECASE,
)
# Shell env-assignment of a credential-named variable (no leading dashes), e.g.
# ``API_KEY=sk-abc npx server`` -- keep the variable name, scrub the value. Case
# -insensitive so lowercase ``api_key=...`` assignments are caught too.
_ENV_ASSIGN_SECRET_RE = re.compile(
    r"\b([A-Za-z0-9_]*(?:token|secret|password|credential|apikey|api_key|key)[A-Za-z0-9_]*=)(\S+)",
    re.IGNORECASE,
)
# ``Authorization: Bearer <token>`` / ``Basic <token>`` schemes in any string.
_AUTH_SCHEME_RE = re.compile(r"\b(Bearer|Basic)\s+([A-Za-z0-9._~+/=-]{8,})", re.IGNORECASE)
# Bare provider tokens (no surrounding structure) recognised by their prefix --
# GitHub PAT/OAuth, OpenAI, Slack, AWS access-key, Google API key, GitLab PAT,
# npm automation token, PyPI upload token, HuggingFace, Stripe, SendGrid,
# Supabase, Databricks. This is a best-effort prefix allowlist layered on top of
# the key-name and flag/value redaction; unrecognised provider prefixes still
# fall to those defences when carried under a credential-named key or flag.
_KNOWN_SECRET_TOKEN_RE = re.compile(
    r"\b(?:"
    r"gh[posur]_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|sk-(?:proj-)?[A-Za-z0-9_-]{20,}"
    r"|sk_(?:live|test)_[A-Za-z0-9]{20,}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|A(?:KIA|SIA)[A-Z0-9]{12,}"
    r"|AIza[A-Za-z0-9_-]{30,}"
    r"|glpat-[A-Za-z0-9_-]{20,}"
    r"|npm_[A-Za-z0-9]{20,}"
    r"|pypi-[A-Za-z0-9_-]{20,}"
    r"|hf_[A-Za-z0-9]{20,}"
    r"|SG\.[A-Za-z0-9_.-]{20,}"
    r"|sbp_[A-Za-z0-9]{20,}"
    r"|dapi[a-f0-9]{32}"
    r")\b"
)
# Flag NAME that takes a secret as the NEXT array element (space-separated form),
# e.g. ``["--token", "sk-abc"]``. Anchored so the ``--flag=value`` form (handled
# by _INLINE_SECRET_ARG_RE) does not match here.
_SECRET_FLAG_NAME_RE = re.compile(
    r"^--?[\w.-]*(?:token|secret|password|credential|apikey|api-key|key)[\w.-]*$",
    re.IGNORECASE,
)
_REDACTED = "***REDACTED***"


def _is_sensitive_mcp_key(key: str) -> bool:
    """Return True when *key* names a credential-bearing field to strip."""
    normalized = key.lower().replace("_", "")
    if normalized in _SENSITIVE_MCP_KEY_NAMES:
        return True
    return any(token in normalized for token in _SENSITIVE_MCP_KEY_SUBSTRINGS)


def _redact_secret_values(text: str) -> tuple[str, bool]:
    """Return (*scrubbed text*, *changed?*) with embedded secrets redacted."""
    scrubbed = _URL_USERINFO_RE.sub(lambda m: f"{m.group(1)}{_REDACTED}@", text)
    scrubbed = _INLINE_SECRET_ARG_RE.sub(lambda m: f"{m.group(1)}{_REDACTED}", scrubbed)
    scrubbed = _SPACE_SECRET_ARG_RE.sub(lambda m: f"{m.group(1)}{_REDACTED}", scrubbed)
    scrubbed = _ENV_ASSIGN_SECRET_RE.sub(lambda m: f"{m.group(1)}{_REDACTED}", scrubbed)
    scrubbed = _AUTH_SCHEME_RE.sub(lambda m: f"{m.group(1)} {_REDACTED}", scrubbed)
    scrubbed = _KNOWN_SECRET_TOKEN_RE.sub(_REDACTED, scrubbed)
    return scrubbed, scrubbed != text


def _sanitize_value(value: Any, path: str, dropped: list[str]) -> Any:
    """Recursively strip credential keys and redact secret values under *value*.

    Mutating-credential surfaces hide at any depth -- ``headers.Authorization``,
    a nested ``config.apiKey``, or a ``user:pass@host`` URL buried in an ``args``
    array -- so a single top-level pass is insufficient. Every dropped key or
    redacted value is recorded in *dropped* (dotted/indexed path) for the
    consequence-led warning.
    """
    if isinstance(value, dict):
        cleaned: dict = {}
        for key, val in value.items():
            child = f"{path}.{key}" if path else str(key)
            if _is_sensitive_mcp_key(str(key)):
                dropped.append(child)
                continue
            cleaned[key] = _sanitize_value(val, child, dropped)
        return cleaned
    if isinstance(value, list):
        cleaned_list: list = []
        redact_next = False
        for i, item in enumerate(value):
            child = f"{path}[{i}]"
            is_flag = isinstance(item, str) and bool(_SECRET_FLAG_NAME_RE.match(item))
            if redact_next and isinstance(item, str) and not is_flag:
                # Previous element was a bare secret flag (e.g. "--token"); this
                # element is its space-separated value -- scrub it whole.
                cleaned_list.append(_REDACTED)
                dropped.append(child)
                redact_next = False
                continue
            cleaned_list.append(_sanitize_value(item, child, dropped))
            redact_next = is_flag
        return cleaned_list
    if isinstance(value, str):
        scrubbed, changed = _redact_secret_values(value)
        if changed:
            dropped.append(path)
        return scrubbed
    return value


# ---------------------------------------------------------------------------
# MCP helpers
# ---------------------------------------------------------------------------


def _read_root_mcp_servers(project_root: Path) -> dict | None:
    """Return the raw ``mcpServers`` map of root ``.mcp.json``, or ``None`` if unusable."""
    mcp_file = project_root / ".mcp.json"
    if not mcp_file.is_file() or mcp_file.is_symlink():
        return None
    try:
        data = json.loads(mcp_file.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        # Untrusted .mcp.json: an oversized-int literal raises bare ValueError
        # (int_max_str_digits), a deeply nested doc raises RecursionError --
        # neither is a JSONDecodeError. Fail closed rather than crash the
        # pack/plugin read on a hostile clone.
        return None
    if not isinstance(data, dict):
        return None
    servers = data.get("mcpServers", {})
    return dict(servers) if isinstance(servers, dict) else None


def apm_managed_mcp_names(project_root: Path) -> frozenset[str] | None:
    """Return the MCP server names ``apm install`` manages for *project_root*.

    ``apm install`` renders the project's own MCP dependencies into root
    ``.mcp.json`` (copilot, claude), so that file mixes authored servers with
    APM's consumer render, which can carry the author's credentials. Pack reads
    only authored servers: every name the lockfile records as APM-managed
    (``mcp_servers``, ``mcp_configs``, ``mcp_target_servers`` for any target) is
    excluded, so pack output is the same before and after ``apm install``.
    Returns an empty set when there is no lockfile and ``None`` when it cannot
    be read (ownership unknown; callers fail closed).
    """
    from ..deps.lockfile import LockFile, resolve_lockfile_path_for_read
    from ..factory import ClientFactory

    try:
        lock = LockFile.read(resolve_lockfile_path_for_read(project_root, read_only=True))
    except (OSError, ValueError):
        return None
    if lock is None:
        return frozenset()
    # Only a claim whose destination is root .mcp.json shows APM wrote that
    # entry; a claim held only by another runtime's file (.cursor/mcp.json)
    # does not. Unknown runtimes count as root claims, and an aggregate name
    # with no recorded placement stays managed (conservative).
    placed: set[str] = set()
    names: set[str] = set()
    for runtime, servers in lock.mcp_target_servers.items():
        placed.update(servers)
        if ClientFactory.project_destination_is_root_mcp_json(str(runtime)) is not False:
            names.update(servers)
    names |= (set(lock.mcp_servers) | set(lock.mcp_configs)) - placed
    return frozenset(str(name) for name in names)


def _authored_manifest_mcp_servers(project_root: Path) -> dict[str, dict]:
    """Project the root ``apm.yml``'s self-defined MCP declarations to server entries.

    Values come from ``apm.yml`` itself, never from the copy ``apm install``
    renders into ``.mcp.json``, so pack output is identical before and after
    install. Registry-resolved declarations need network resolution and are
    not projected, nor are servers whose ``targets:`` exclude Claude, the
    only ecosystem that embeds them. Credential-bearing keys are removed later by
    :func:`_sanitize_mcp_servers`.
    """
    from ..models.apm_package import APMPackage

    manifest = manifest_file(project_root)
    if not manifest.is_file():
        return {}
    try:
        dependencies = APMPackage.from_apm_yml(manifest).get_mcp_dependencies()
    except (OSError, ValueError):
        return {}
    from ..integration.mcp_integrator_install import mcp_dependency_runtimes

    servers: dict[str, dict] = {}
    for dep in dependencies:
        # A server whose `targets:` leave out Claude is not Claude's to ship (#2606).
        if not dep.is_self_defined or not mcp_dependency_runtimes(dep, ["claude"]):
            continue
        transport = (dep.transport or "stdio").lower()
        entry: dict[str, Any] = {"type": "http" if transport == "streamable-http" else transport}
        if transport == "stdio":
            if dep.command:
                entry["command"] = dep.command
            if dep.args:
                entry["args"] = list(dep.args) if isinstance(dep.args, list | tuple) else dep.args
            if dep.cwd:
                entry["cwd"] = dep.cwd
            if dep.env:
                entry["env"] = dict(dep.env)
        else:
            if dep.url:
                entry["url"] = dep.url
            if dep.headers:
                entry["headers"] = dict(dep.headers)
        servers[dep.name] = entry
    return servers


def _warn_unknown_mcp_ownership(logger: Any) -> None:
    _emit(
        "warning",
        "Root .mcp.json left out of the plugin: apm.lock.yaml cannot be read, so the "
        "MCP servers APM installed there cannot be told apart from authored ones. "
        f"{PLUGIN_MCP_OWNERSHIP_UNKNOWN.sentence()}",
        logger,
        "warning",
    )


def collect_mcp_servers(
    project_root: Path, *, logger: Any = None, exclude_apm_managed: bool = True
) -> dict:
    """Return ``mcpServers`` dict from ``.mcp.json`` with credentials stripped.

    Returns an empty dict when the file is absent, is a symlink, or cannot be
    parsed.

    With *exclude_apm_managed* (pack reads of the author's own project root),
    servers named by :func:`apm_managed_mcp_names` are dropped first, and the
    whole file is withheld when the lockfile cannot be read.

    Each server object is sanitised before it is returned: any key that may
    carry a live credential (``env``/``environment``/``headers``/``authorization``
    blocks and any key whose name contains ``token``, ``secret``, ``password``,
    ``credential``, ``apikey``, or ``key`` -- case-insensitive) is dropped at any
    nesting depth, and secret-shaped values are redacted wherever they hide --
    ``user:pass@host`` URLs, inline ``--token=`` flags, space-separated
    ``--token VALUE`` pairs, shell ``ENV=secret`` prefixes, ``Bearer``/``Basic``
    auth headers, and bare provider tokens (GitHub, OpenAI, Slack, AWS, Google,
    GitLab, npm, PyPI, HuggingFace, Stripe, SendGrid, Supabase, Databricks, and
    other recognised provider token prefixes) passed as positional args.
    ``.mcp.json`` routinely embeds secrets so
    an MCP host can inject them at startup; copying them verbatim into a
    committed ``plugin.json`` would exfiltrate them into the distributed
    artefact. A loud warning is emitted for every key dropped or value redacted.
    """
    servers = _read_root_mcp_servers(project_root) or {}
    if not exclude_apm_managed:
        return _sanitize_mcp_servers(servers, logger=logger) if servers else {}
    authored = _authored_manifest_mcp_servers(project_root)
    if servers:
        managed = apm_managed_mcp_names(project_root)
        if managed is None:
            _warn_unknown_mcp_ownership(logger)
            servers = {}
        else:
            dropped = sorted(name for name in servers if name in managed and name not in authored)
            if dropped:
                _emit(
                    "warning",
                    "Left out of the plugin: .mcp.json servers that 'apm install' manages and "
                    "apm.yml does not define itself (" + ", ".join(dropped) + "). Pack ships "
                    "only servers you author in apm.yml or .mcp.json.",
                    logger,
                    "warning",
                )
            servers = {name: server for name, server in servers.items() if name not in managed}
    # apm.yml declarations win over a same-named root entry, so a rendered or
    # hand-edited copy never changes what ships.
    servers.update(authored)
    return _sanitize_mcp_servers(servers, logger=logger) if servers else {}


def _sanitize_mcp_servers(servers: dict, *, logger: Any = None) -> dict:
    """Strip credential keys and redact secret values across all server objects.

    Server names (the top-level keys) are NOT credential-tested -- a server
    named ``my-keychain`` must survive -- but every value beneath each server is
    recursed into via :func:`_sanitize_value`.
    """
    cleaned: dict = {}
    dropped: list[str] = []
    for server_name, server in servers.items():
        cleaned[server_name] = _sanitize_value(server, str(server_name), dropped)

    if dropped:
        _warn = (
            "Secrets withheld from plugin.json so they are never committed as "
            "plaintext -- stripped from .mcp.json before writing: "
            + ", ".join(dropped)
            + ". Use $ENV_VAR references in .mcp.json to keep secrets out of the manifest."
        )
        _emit("warning", _warn, logger, "warning")
    return cleaned


# ---------------------------------------------------------------------------
# Plugin JSON source
# ---------------------------------------------------------------------------


def find_or_synthesize_plugin_json(
    project_root: Path,
    apm_yml_path: Path,
    *,
    suppress_missing_warning: bool = False,
    logger: Any = None,
) -> dict:
    """Locate an existing ``plugin.json`` or synthesise one from ``apm.yml``.

    Resolution order:

    1. Call :func:`apm_cli.utils.helpers.find_plugin_json` to locate an
       on-disk ``plugin.json``.
    2. If found, parse and return it.  On a parse error, warn and fall back to
       synthesis.
    3. If not found, synthesise from ``apm.yml`` via
       :func:`apm_cli.deps.plugin_parser.synthesize_plugin_json_from_apm_yml`.

    ``suppress_missing_warning`` silences the "no plugin.json on disk" info
    message when the caller knows synthesis is the expected path -- for example
    a marketplace-publishing run that also has a ``dependencies:`` block for
    local development.  Genuine parse errors on an existing file are always
    surfaced.
    """
    from ..deps.plugin_parser import synthesize_plugin_json_from_apm_yml
    from ..utils.helpers import find_plugin_json

    plugin_json_path = find_plugin_json(project_root)
    if plugin_json_path is not None:
        try:
            return json.loads(plugin_json_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError) as exc:
            _warn_msg = (
                f"Found plugin.json at {plugin_json_path} but could not parse it: {exc}. "
                "Falling back to synthesis from apm.yml."
            )
            _emit("warning", _warn_msg, logger, "warning")

    elif not suppress_missing_warning:
        # Synthesis from apm.yml is the APM-native happy path for plugin
        # authoring, not a defect -- so this is info, not a warning.
        _info_msg = "No plugin.json found; synthesising from apm.yml."
        _emit("info", _info_msg, logger, "info")

    return synthesize_plugin_json_from_apm_yml(apm_yml_path)


# ---------------------------------------------------------------------------
# Manifest builder
# ---------------------------------------------------------------------------


def build_plugin_manifest(
    project_root: Path,
    apm_yml_path: Path,
    ecosystem: str,
    *,
    logger: Any = None,
) -> dict:
    """Build a ``plugin.json`` payload for the given *ecosystem*.

    The base fields are synthesised from ``apm.yml`` via
    :func:`apm_cli.deps.plugin_parser.synthesize_plugin_json_from_apm_yml`.
    Per-ecosystem rules are then applied:

    * **claude**: author kept as ``{"name": ...}``; ``mcpServers`` included when
      a ``.mcp.json`` is present (APM-managed servers excluded, credential-bearing
      keys stripped).
    * **copilot**: author kept as ``{"name": ...}``; ``mcpServers`` omitted
      (not part of the Copilot plugin manifest schema).
    * **codex**: the documented Codex compatibility manifest. Declares
      ``skills: "./skills/"`` and ``mcpServers: "./.mcp.json"`` only when those
      regular paths exist (and ``.mcp.json`` holds no APM-managed server), so
      Codex reads the author's own MCP file natively and no server data is
      copied or translated. No ``interface`` is
      generated, so Codex has no listing presentation metadata for it.

    Convention directories (``agents/``, ``skills/``, ``commands/``) are
    otherwise auto-discovered by the host, so they are not listed explicitly.
    """
    from ..deps.plugin_parser import synthesize_plugin_json_from_apm_yml

    # Always synthesise fresh from apm.yml -- apm.yml is the source of truth for
    # the generated manifest, so we intentionally do NOT consult an on-disk
    # plugin.json here (unlike find_or_synthesize_plugin_json, which is the
    # disk-first reader used by the bundle exporter).
    manifest: dict = synthesize_plugin_json_from_apm_yml(apm_yml_path)

    # Strip any convention-directory keys -- hosts auto-discover these.
    for key in ("agents", "skills", "commands", "instructions"):
        manifest.pop(key, None)

    if ecosystem == "claude":
        mcp_servers = collect_mcp_servers(project_root, logger=logger)
        if mcp_servers:
            manifest["mcpServers"] = mcp_servers
    elif ecosystem == "codex":
        _apply_codex_components(project_root, manifest, logger=logger)
    elif ecosystem == "copilot":
        manifest.pop("mcpServers", None)
    else:
        raise ValueError(f"Unknown plugin manifest ecosystem {ecosystem!r}")

    return manifest


def _is_regular(path: Path, *, directory: bool) -> bool:
    if path.is_symlink():
        return False
    return path.is_dir() if directory else path.is_file()


def _codex_may_reference_root_mcp(project_root: Path, *, logger: Any = None) -> bool:
    """Whether Codex may read root ``.mcp.json`` as the author's own file.

    Codex reads the referenced file verbatim, so it is referenced only when it
    holds no server APM manages (see :func:`apm_managed_mcp_names`).
    """
    if not _is_regular(project_root / ".mcp.json", directory=False):
        return False
    managed = apm_managed_mcp_names(project_root)
    if managed is None:
        _warn_unknown_mcp_ownership(logger)
        return False
    if not managed:
        return True
    servers = _read_root_mcp_servers(project_root)
    if servers is None:
        return False
    held = sorted(name for name in servers if name in managed)
    if not held:
        return True
    authored = sorted(name for name in servers if name not in managed)
    if authored:
        _emit(
            "warning",
            "Codex plugin.json does not reference .mcp.json: it also holds MCP servers "
            "'apm install' manages ("
            + ", ".join(held)
            + "), so authored servers ("
            + ", ".join(authored)
            + ") are not shipped to Codex either. Keep APM-managed servers out of a "
            "Codex package's root .mcp.json.",
            logger,
            "warning",
        )
    return False


def _apply_codex_components(project_root: Path, manifest: dict, *, logger: Any = None) -> None:
    """Shape *manifest* as the documented ``.codex-plugin/plugin.json``."""
    manifest.pop("mcpServers", None)
    if _is_regular(project_root / "skills", directory=True):
        manifest["skills"] = "./skills/"
    if _codex_may_reference_root_mcp(project_root, logger=logger):
        manifest["mcpServers"] = "./.mcp.json"

    author = manifest.get("author")
    missing = [field for field in ("version", "description") if not manifest.get(field)]
    if not (isinstance(author, dict) and author.get("name")):
        missing.append("author")
    if missing:
        _warn = (
            "Codex expects " + ", ".join(missing) + " in .codex-plugin/plugin.json; "
            "add them to apm.yml so Codex lists this plugin with its real identity."
        )
        _emit("warning", _warn, logger, "warning")
    if not is_safe_codex_plugin_name(manifest.get("name")):
        _warn = (
            f"Codex plugin name {manifest.get('name')!r} must start with a letter or "
            "digit and use only letters, digits, '.', '_' or '-'; rename it in apm.yml "
            "so 'apm install -g --target codex' accepts this plugin."
        )
        _emit("warning", _warn, logger, "warning")


# ---------------------------------------------------------------------------
# Manifest writer
# ---------------------------------------------------------------------------


def write_plugin_manifest_with_outcome(
    project_root: Path,
    manifest: dict,
    ecosystem: str,
    *,
    dry_run: bool = False,
    force: bool = False,
    logger: Any = None,
) -> PluginManifestWriteResult:
    """Write *manifest* and return its canonical write decision.

    The output path is resolved from :data:`PLUGIN_ECOSYSTEM_PATHS`.  Parent
    directories are created automatically.

    **Overwrite policy.** If a ``plugin.json`` already exists at the target
    path it is preserved unless *force* is set (threaded from ``apm pack
    --force``). Without ``--force`` the function emits a warning and skips the
    write with a ``"skipped"`` result -- this mirrors the collision contract the rest
    of ``apm pack`` already honours and prevents a compromised ``.mcp.json``
    from silently replacing a hand-audited file. With ``--force`` the existing
    file is overwritten and a warning records the replacement.

    In dry-run mode the function logs what *would* be written and returns a
    ``"dry_run"`` result without touching the filesystem.

    The result distinguishes a real write, a protected existing file, and a
    dry-run preview so callers can report the same decision without inferring it
    from filesystem state.
    """
    rel_path = PLUGIN_ECOSYSTEM_PATHS.get(ecosystem)
    if rel_path is None:
        _warn = f"Unknown plugin ecosystem {ecosystem!r}; skipping plugin.json generation."
        _emit("warning", _warn, logger, "warning")
        return PluginManifestWriteResult(path=None, action="skipped")

    output_path = project_root / rel_path

    # Containment guard: reject symlink-based escapes (e.g. a symlinked
    # .github/ directory pointing outside the project root).
    ensure_path_within(output_path, project_root)

    if output_path.exists():
        if not force:
            _skip_warn = (
                f"{output_path} already exists; skipping plugin.json generation. "
                "Re-run with --force to overwrite it."
            )
            _emit("warning", _skip_warn, logger, "warning")
            return PluginManifestWriteResult(path=None, action="skipped")

    if dry_run:
        if output_path.exists():
            _msg = (
                f"Would overwrite plugin manifest at {output_path} "
                "with generated manifest from apm.yml (--force)."
            )
        else:
            _msg = f"Would write plugin manifest to {output_path}"
        _emit("info", _msg, logger, "info")
        return PluginManifestWriteResult(path=None, action="dry_run")

    if output_path.exists():
        _overwrite_warn = (
            f"Overwriting {output_path} with generated manifest from apm.yml (--force)."
        )
        _emit("warning", _overwrite_warn, logger, "warning")

    # Generated content under .github/ is granted elevated trust by GitHub
    # Actions -- surface the write so operators with branch-protection on
    # .github/ paths are not surprised.
    if rel_path.startswith(".github/"):
        _gh_note = f"Writing generated plugin manifest under .github/: {output_path}"
        _emit("info", _gh_note, logger, "info")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Re-check containment after mkdir to shrink the TOCTOU window -- a parent
    # component could have been swapped for a symlink between the first check
    # and directory creation.
    ensure_path_within(output_path, project_root)
    write_text_lf(output_path, json.dumps(manifest, indent=2, sort_keys=False) + "\n")

    _success = f"Generated plugin manifest: {output_path}"
    _emit("success", _success, logger, "check")

    return PluginManifestWriteResult(path=output_path, action="written")


def write_plugin_manifest(
    project_root: Path,
    manifest: dict,
    ecosystem: str,
    *,
    dry_run: bool = False,
    force: bool = False,
    logger: Any = None,
) -> Path | None:
    """Write *manifest* as ``plugin.json`` and return its path when written."""
    return write_plugin_manifest_with_outcome(
        project_root,
        manifest,
        ecosystem,
        dry_run=dry_run,
        force=force,
        logger=logger,
    ).path
