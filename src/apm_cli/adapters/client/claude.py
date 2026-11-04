"""Claude Code MCP client adapter.

Project scope: ``.mcp.json`` at the project root with top-level ``mcpServers``
(``--scope project`` in Claude Code). Writes are opt-in when ``.claude/`` exists,
mirroring the Cursor/OpenCode directory-presence convention.

User scope: top-level ``mcpServers`` in ``$CLAUDE_CONFIG_DIR/.claude.json``
when configured, otherwise ``~/.claude.json`` (``--scope user``). Writes are
atomic and create the file with ``0o600`` permissions on first write so the
shared Claude Code config is not silently truncated by a concurrent writer
and cannot leak any embedded OAuth state to other users on a multi-user host.

Local scope (Claude Code's third scope -- the default for ``claude mcp add``,
storing per-project private config under ``~/.claude.json -> projects.<abs_path>
.mcpServers``) is intentionally NOT supported: APM packages are designed for
reproducible team installs, which aligns with PROJECT (VCS-shared) and USER
(cross-project), not LOCAL (per-project private to one user).

See https://code.claude.com/docs/en/mcp
"""

import json
import logging
import os
import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import ClassVar

from ...utils.atomic_io import atomic_write_text
from ...utils.console import _rich_error, _rich_success, _rich_warning
from ...utils.path_security import PathTraversalError, validate_path_segments
from .base import _ENV_VAR_RE, _translate_env_placeholder
from .copilot import CopilotClientAdapter

_log = logging.getLogger(__name__)

# Claude Code's reference syntax: ${VAR} or ${VAR:-default}.
_CLAUDE_REFERENCE_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-[^}]*)?\}")


def _string_leaves(value: object) -> Iterator[str]:
    """Yield every string inside a rendered server entry."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _string_leaves(item)
    elif isinstance(value, list):
        for item in value:
            yield from _string_leaves(item)


def _paired_leaves(old: object, new: object, path: str = "") -> Iterator[tuple[str, str, str]]:
    """Yield ``(path, old, new)`` for string leaves present in both entries."""
    if isinstance(old, str) and isinstance(new, str):
        yield path, old, new
    elif isinstance(old, Mapping) and isinstance(new, Mapping):
        for key in new.keys() & old.keys():
            yield from _paired_leaves(old[key], new[key], f"{path}.{key}" if path else str(key))
    elif isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
        for index, (old_item, new_item) in enumerate(zip(old, new, strict=True)):
            yield from _paired_leaves(old_item, new_item, f"{path}[{index}]")


def _expanded_leaves(old: object, new: object) -> Iterator[tuple[str, str]]:
    """Yield ``(path, new)`` where *new* holds a reference and *old* a value."""
    for path, old_text, new_text in _paired_leaves(old, new):
        if old_text != new_text and "${" not in old_text and _CLAUDE_REFERENCE_RE.search(new_text):
            yield path, new_text


def _expand_like_install_time(text: str, environ: Mapping[str, str]) -> str:
    """Expand bare ``${VAR}`` references the way pre-#3252 installs did."""
    return _ENV_VAR_RE.sub(lambda m: environ.get(m.group(1)) or m.group(0), text)


def _legacy_equivalent(native: object, expected: object, environ: Mapping[str, str]) -> bool:
    """Whether *native* is *expected* with its references expanded from *environ*.

    Pre-#3252 releases expanded each recognized reference at install time and
    left unresolved ones in their authored spelling (``${env:VAR}``, ``<VAR>``)
    or, for registry variables, as an empty string.
    """
    if isinstance(expected, Mapping):
        return (
            isinstance(native, Mapping)
            and native.keys() == expected.keys()
            and all(_legacy_equivalent(native[key], expected[key], environ) for key in expected)
        )
    if isinstance(expected, list):
        return (
            isinstance(native, list)
            and len(native) == len(expected)
            and all(
                _legacy_equivalent(item, ref, environ)
                for item, ref in zip(native, expected, strict=True)
            )
        )
    if isinstance(expected, str) and isinstance(native, str):
        if native == expected:
            return True
        expanded = _expand_like_install_time(expected, environ)
        if _translate_env_placeholder(native) == expanded:
            return True
        unset_reference = _ENV_VAR_RE.fullmatch(expected)
        return native == "" and bool(unset_reference) and not environ.get(unset_reference.group(1))
    return native == expected


class ClaudeClientAdapter(CopilotClientAdapter):
    """MCP configuration for Claude Code (``mcpServers`` schema).

    Registry formatting reuses :class:`CopilotClientAdapter`, then entries are
    normalized for Claude Code's on-disk shape (stdio servers omit Copilot-only
    keys like ``type: "local"``, default ``tools``, and empty ``id``).

    Scope routing is governed by the ``user_scope`` constructor flag inherited
    from :class:`MCPClientAdapter` -- not by post-construction monkey-patching.
    """

    supports_user_scope: bool = True
    _client_label: str = "Claude Code"
    target_name: str = "claude"
    mcp_servers_key: str = "mcpServers"

    _runtime_config_label: ClassVar[str] = ".mcp.json"
    _runtime_launch_command: ClassVar[str] = "claude"

    # Own post-install aggregation buckets: the parent's mutable ClassVars
    # are shared by reference, so without these Claude's diagnostics would
    # be reported (and worded) as Copilot CLI's.
    _legacy_angle_offenders_by_server: ClassVar[dict] = {}
    _security_upgraded_keys: ClassVar[set] = set()
    _unset_env_keys_by_server: ClassVar[dict] = {}
    _install_run_summary_emitted: ClassVar[bool] = False

    # Extra guidance appended to the security-upgrade notice: replacing a
    # committed value does not revoke it or remove it from Git history.
    _runtime_upgrade_guidance: ClassVar[str] = (
        "If .mcp.json was committed, rotate these credentials: replacing the "
        "values does not remove them from Git history."
    )

    @property
    def _supports_runtime_env_substitution(self) -> bool:
        """Keep ``${VAR}`` references in the project-scoped ``.mcp.json``.

        Claude Code expands ``${VAR}`` and ``${VAR:-default}`` in a project
        ``.mcp.json`` in ``command``, ``args``, ``env``, ``url`` and
        ``headers`` when it starts a server (checked with Claude Code
        2.1.310), so secrets stay in each developer's environment instead of
        being copied into a file teams commit. User scope (``~/.claude.json``,
        outside the repo, written ``0o600``) keeps install-time resolution.
        See #1152 and #3252.
        """
        return not self._is_user_scope()

    def _format_runtime_env_placeholder(self, name: str) -> str:
        """Return Claude Code's native reference syntax, ``${NAME}``."""
        return "${" + name + "}"

    # Skill packages declare self-defined MCP launchers using the cross-tool
    # converged path ``.agents/skills/<name>/...`` (see
    # https://microsoft.github.io/apm/reference/targets-matrix/#skills-convergence).
    # Claude is the only target that does NOT converge to ``.agents/skills/``
    # -- it keeps ``.claude/skills/`` (see install/skill_path_migration.py:55).
    # Rewrite the command prefix so ``.mcp.json`` points at where the launcher
    # actually lives after deploy.
    _CONVERGED_SKILL_PREFIX = ".agents/skills/"
    _CLAUDE_SKILL_PREFIX = ".claude/skills/"

    # Entry ``type`` values Claude Code uses for URL-addressed servers.
    _REMOTE_TYPES = ("http", "sse", "streamable-http")

    # Transport fields are replaced across families; a partial update can
    # retain an omitted type only when it still belongs to the selected family.
    _REMOTE_TRANSPORT_KEYS = frozenset({"type", "url", "headers"})
    _STDIO_TRANSPORT_KEYS = frozenset({"type", "command", "args", "env", "cwd"})

    @classmethod
    def _rewrite_self_defined_skill_command(cls, command: str) -> str:
        if isinstance(command, str) and command.startswith(cls._CONVERGED_SKILL_PREFIX):
            return cls._CLAUDE_SKILL_PREFIX + command[len(cls._CONVERGED_SKILL_PREFIX) :]
        return command

    def _format_server_config(self, server_info, env_overrides=None, runtime_vars=None):
        config = super()._format_server_config(server_info, env_overrides, runtime_vars)
        if isinstance(config, dict) and "command" in config:
            config["command"] = self._rewrite_self_defined_skill_command(config["command"])
        if isinstance(config, dict) and self._supports_runtime_env_substitution:
            # The shared pipeline copies command and url verbatim. Claude Code
            # only reads ${VAR}, so normalize ${env:VAR} and <VAR> there too.
            for field in ("command", "url"):
                if isinstance(config.get(field), str):
                    config[field] = self._resolve_env_variable(field, config[field])
        return config

    def render_server_config(
        self,
        server_info: dict,
        *,
        env_overrides: dict | None = None,
        runtime_vars: dict | None = None,
    ) -> dict:
        """Compare against the normalized shape actually written to Claude."""
        return self._normalize_mcp_entry_for_claude_code(
            super().render_server_config(
                server_info, env_overrides=env_overrides, runtime_vars=runtime_vars
            )
        )

    def matches_managed_server_config(self, native: object, expected: dict) -> bool:
        """OAuth metadata is runtime-owned, not part of an APM connection declaration."""
        if not isinstance(native, dict):
            return False
        return self.managed_server_config(native) == expected

    def managed_server_config(self, native: dict) -> dict:
        """Exclude only the runtime-owned OAuth account from write fingerprints."""
        return {key: value for key, value in native.items() if key != "oauthAccount"}

    def project_managed_server_config(
        self, native: object, *, baseline: dict, requested: dict
    ) -> object:
        """Exclude a valid native-only cwd from managed reconciliation, never cleanup."""
        if (
            not isinstance(native, dict)
            or native.get("type") != "stdio"
            or baseline.get("type") != "stdio"
            or "cwd" in baseline
            or "cwd" in requested
        ):
            return native
        cwd = native.get("cwd")
        if not isinstance(cwd, str) or not cwd.strip() or "\x00" in cwd:
            return native
        try:
            validate_path_segments(cwd, context="MCP cwd", allow_current_dir=True)
        except PathTraversalError:
            return native
        return {key: value for key, value in native.items() if key != "cwd"}

    def _resolve_environment_variables(self, env_vars, env_overrides=None):
        """Keep static ``env`` values authored in apm.yml as written.

        Copilot's translate mode also rewrites static values to ``${NAME}``.
        Claude Code passes an unset variable without a default through as
        the literal ``${NAME}`` text, so a server configured with a plain
        setting such as ``LOG_LEVEL: debug`` would silently lose it.
        """
        if isinstance(env_vars, dict) and self._supports_runtime_env_substitution:
            return self._resolve_explicit_environment_values(env_vars, env_overrides)
        return super()._resolve_environment_variables(env_vars, env_overrides)

    # ------------------------------------------------------------------ #
    # Post-install diagnostics (names only, never values)
    # ------------------------------------------------------------------ #

    def _begin_env_tracking(self, server_url, server_name):
        """Snapshot the current native entry for the security-upgrade notice."""
        self._last_env_placeholder_keys = set()
        self._last_legacy_angle_vars = set()
        if not self._supports_runtime_env_substitution:
            return None
        key = self._determine_config_key(server_url, server_name)
        previous = (self.get_current_config().get(self.mcp_servers_key) or {}).get(key)
        return previous if isinstance(previous, dict) else None

    def _record_env_diagnostics(self, config_key, server_config, previously_baked):
        """Record replaced values and missing variables for the run summary."""
        if self._supports_runtime_env_substitution:
            if self._last_legacy_angle_vars:
                self._legacy_angle_offenders_by_server[config_key] = set(
                    self._last_legacy_angle_vars
                )
            if isinstance(previously_baked, dict):
                for _path, text in _expanded_leaves(previously_baked, server_config):
                    self._security_upgraded_keys.update(
                        m.group(1) for m in _CLAUDE_REFERENCE_RE.finditer(text)
                    )
        self._emit_install_summary(config_key, server_config)

    def _emit_install_summary(self, config_key, server_config):
        """Track unset references without a default in every expanded field."""
        if not self._supports_runtime_env_substitution:
            return
        names = {
            m.group(1) for text in _string_leaves(server_config) for m in _ENV_VAR_RE.finditer(text)
        }
        unset = sorted(name for name in names if not os.environ.get(name))
        if unset:
            bucket = self.__class__._unset_env_keys_by_server.setdefault(config_key, [])
            bucket.extend(name for name in unset if name not in bucket)

    # ------------------------------------------------------------------ #
    # Entries written before references were kept (#3252)
    # ------------------------------------------------------------------ #

    def matches_legacy_managed_config(
        self, native: object, expected: dict, *, native_hash: str | None = None
    ) -> bool:
        """Whether *native* is the expanded form APM wrote for *expected*.

        Earlier releases expanded references at install time. Such an entry is
        treated as APM-generated only when it is unchanged since APM recorded
        its write hash, or when expanding the references in *expected* (the
        current rendering of the same declaration) from this environment
        reproduces it exactly. Anything else may hold user edits.
        """
        if self._is_user_scope() or not isinstance(native, dict):
            return False
        if not any(_CLAUDE_REFERENCE_RE.search(text) for text in _string_leaves(expected)):
            return False
        comparable = self.managed_server_config(native)
        if native_hash is not None:
            from apm_cli.install.mcp.ownership import mcp_native_hash

            if mcp_native_hash(comparable) == native_hash:
                return True
        return _legacy_equivalent(comparable, expected, os.environ)

    def native_conflict_guidance(self, name: str, expected: dict) -> str | None:
        """Explain a preserved entry that holds values where apm.yml has references."""
        if self._is_user_scope():
            return None
        key = self._determine_config_key(name, None)
        native = (self.get_current_config().get(self.mcp_servers_key) or {}).get(key)
        fields = sorted(path for path, _text in _expanded_leaves(native, expected))
        if not fields:
            return None
        where = (
            f"Field {fields[0]} holds a value"
            if len(fields) == 1
            else f"Fields {', '.join(fields)} hold values"
        )
        return (
            f"{where} where apm.yml declares environment references, and APM cannot "
            "confirm it wrote them. Put the ${VAR} references declared in apm.yml "
            "back in those places yourself, then retry. If .mcp.json was committed, "
            "rotate those credentials: editing the file does not erase them in Git "
            "history."
        )

    @classmethod
    def _is_remote_mcp_entry(cls, entry: dict) -> bool:
        """Return whether *entry* describes a remote (URL-addressed) server."""
        return bool(entry.get("url")) or entry.get("type") in cls._REMOTE_TYPES

    @classmethod
    def _normalize_mcp_entry_for_claude_code(cls, entry: dict) -> dict:
        """Normalize a server entry to Claude Code's on-disk shape.

        For remote servers, keep ``type``/``url``/``headers`` per Claude
        Code docs.  For stdio servers, drop Copilot-CLI-only fields
        (``type: "local"``, default ``tools``, empty ``id``) and emit
        an explicit ``type: "stdio"`` so ``claude mcp list`` renders
        the entry the same way it would if installed via
        ``claude mcp add --transport stdio``.

        See https://code.claude.com/docs/en/mcp
        """
        if not isinstance(entry, dict):
            return entry
        out = dict(entry)

        if cls._is_remote_mcp_entry(out):
            if out.get("id") in ("", None):
                out.pop("id", None)
            if out.get("tools") == ["*"]:
                out.pop("tools", None)
            return out

        if out.get("type") in (None, "local", "stdio"):
            out["type"] = "stdio"
        if out.get("tools") == ["*"]:
            out.pop("tools", None)
        if out.get("id") in ("", None):
            out.pop("id", None)
        return out

    @classmethod
    def _retained_previous_entry(cls, prev: dict, new_cfg: dict) -> dict:
        """Return *prev* without the keys of the transport *new_cfg* is not.

        A redeclaration that switches transport must not leave the previous
        transport's keys behind.  A surviving ``url`` keeps the entry
        classified as remote, so the stdio shape never wins and a stale
        ``Authorization`` header stays in the Claude Code config.

        The keys are selected from the update rather than from a comparison
        of the two entries, so an entry already carrying both transports --
        written by a release that merged them unconditionally -- is repaired
        by the next install that writes it instead of matching its own mixed shape and
        surviving.  Keys APM does not manage (hand-authored OAuth blocks and
        the like) describe no transport and are retained either way.
        """
        declares_transport = any(new_cfg.get(key) for key in ("type", "url", "command"))
        is_remote = cls._is_remote_mcp_entry(new_cfg if declares_transport else prev)
        stale = cls._STDIO_TRANSPORT_KEYS if is_remote else cls._REMOTE_TRANSPORT_KEYS
        compatible_types = cls._REMOTE_TYPES if is_remote else ("local", "stdio")
        # Keep a compatible type in place so repeated writes retain field order.
        return {
            key: value
            for key, value in prev.items()
            if key not in stale or (key == "type" and value in compatible_types)
        }

    @classmethod
    def _merge_mcp_server_dicts(
        cls, existing_servers: dict, config_updates: dict, managed_fields: set[str] | None = None
    ) -> None:
        """Merge *config_updates* into *existing_servers* in place.

        Per-server entries are shallow-merged: ``{**old, **new}`` so keys present
        only on plugin- or hand-authored configs (e.g. OAuth blocks)
        survive when an update omits them.  Keys in *new* overwrite *old* on
        conflict so APM/registry installs still refresh ``command``/``args``/etc.
        The previous entry first drops the keys of the transport the update
        does not declare, so the merged entry describes exactly one.
        """
        for name, new_cfg in config_updates.items():
            if not isinstance(new_cfg, dict):
                existing_servers[name] = new_cfg
                continue
            prev = existing_servers.get(name)
            if isinstance(prev, dict):
                if managed_fields:
                    prev = {
                        key: value
                        for key, value in prev.items()
                        if key not in managed_fields or key in new_cfg
                    }
                merged = {**cls._retained_previous_entry(prev, new_cfg), **new_cfg}
                existing_servers[name] = merged
            else:
                existing_servers[name] = dict(new_cfg)

    def _merge_and_normalize_updates(
        self, data: dict, config_updates: dict, managed_fields: set[str] | None = None
    ) -> None:
        if "mcpServers" not in data:
            data["mcpServers"] = {}
        self._merge_mcp_server_dicts(data["mcpServers"], config_updates, managed_fields)
        for name in config_updates:
            ent = data["mcpServers"].get(name)
            if isinstance(ent, dict):
                data["mcpServers"][name] = self._normalize_mcp_entry_for_claude_code(ent)

    def _is_user_scope(self) -> bool:
        return bool(self.user_scope)

    def _project_mcp_path(self) -> Path:
        return self.project_root / ".mcp.json"

    @classmethod
    def resolve_user_claude_unresolved_path(cls) -> Path:
        """Return the un-normalized path for user-scope Claude Code config.

        Used for symlink pre-checks before canonical resolution.
        """
        config_dir = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
        if config_dir:
            config_path = Path(config_dir).expanduser()
            if not config_path.is_absolute():
                raise PathTraversalError(
                    "CLAUDE_CONFIG_DIR must be an absolute path for user-scope MCP "
                    "config; set an absolute path or unset it to use ~/.claude.json."
                )
            return config_path / ".claude.json"
        return Path.home() / ".claude.json"

    @classmethod
    def resolve_user_claude_json_path(cls, unresolved_path: Path | None = None) -> Path:
        """Resolve the selected destination, optionally reusing a checked path."""
        path = (
            unresolved_path
            if unresolved_path is not None
            else cls.resolve_user_claude_unresolved_path()
        )
        return path.parent.resolve(strict=False) / path.name

    def _user_claude_json_path(self) -> Path:
        return self.resolve_user_claude_json_path()

    def _should_write_project(self) -> bool:
        return (self.project_root / ".claude").is_dir()

    def get_config_path(self):
        if self._is_user_scope():
            return str(self._user_claude_json_path())
        return str(self._project_mcp_path())

    def get_current_config(self):
        path = self._user_claude_json_path() if self._is_user_scope() else self._project_mcp_path()
        if not path.is_file():
            return {"mcpServers": {}}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {"mcpServers": {}}
            return {"mcpServers": dict(data.get("mcpServers") or {})}
        except (json.JSONDecodeError, OSError):
            return {"mcpServers": {}}

    def update_config(
        self, config_updates, enabled=True, *, managed_fields: set[str] | None = None
    ):
        if self._is_user_scope():
            return self._merge_user_mcp(config_updates, managed_fields=managed_fields)
        if not self._should_write_project():
            _rich_warning(
                f"Skipped Claude Code project MCP -- .claude/ not found in {self.project_root}"
            )
            return False
        path = self._project_mcp_path()
        try:
            if path.is_file():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    _rich_warning(f"Existing {path} is not valid JSON; rewriting from scratch")
                    data = {}
                if not isinstance(data, dict):
                    data = {}
            else:
                data = {}
            self._merge_and_normalize_updates(data, config_updates, managed_fields)
            path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            return True
        except OSError:
            return False

    def _merge_user_mcp(self, config_updates, *, managed_fields: set[str] | None = None) -> bool:
        path = self._user_claude_json_path()
        try:
            if path.is_file():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    _rich_warning(f"Existing {path} is not valid JSON; rewriting from scratch")
                    data = {}
                if not isinstance(data, dict):
                    data = {}
            else:
                data = {}
            self._merge_and_normalize_updates(data, config_updates, managed_fields)
            payload = json.dumps(data, indent=2) + "\n"
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(path, payload, new_file_mode=0o600)
            _log.debug("Claude user-scope MCP config written to %s", path)
            return True
        except OSError:
            return False

    def configure_mcp_server(
        self,
        server_url,
        server_name=None,
        enabled=True,
        env_overrides=None,
        server_info_cache=None,
        runtime_vars=None,
        managed_fields: set[str] | None = None,
    ):
        if not server_url:
            _rich_error("server_url cannot be empty")
            return False

        if not self._is_user_scope() and not self._should_write_project():
            _rich_warning(
                f"Skipped Claude Code project MCP -- .claude/ not found in {self.project_root}"
            )
            return False

        try:
            if server_info_cache and server_url in server_info_cache:
                server_info = server_info_cache[server_url]
            else:
                server_info = self.registry_client.find_server_by_reference(server_url)

            if not server_info:
                _rich_error(f"MCP server '{server_url}' not found in registry")
                return False

            config_key = self._determine_config_key(server_url, server_name)

            previously_baked = self._begin_env_tracking(server_url, server_name)
            server_config = self._format_server_config(server_info, env_overrides, runtime_vars)
            from apm_cli.install.mcp.ownership import mcp_native_hash

            fingerprint = mcp_native_hash(
                self.managed_server_config(self._normalize_mcp_entry_for_claude_code(server_config))
            )
            ok = self.update_config({config_key: server_config}, managed_fields=managed_fields)
            if not ok:
                _rich_error(f"Failed to write MCP config for '{config_key}' to Claude Code")
                return False
            self.written_mcp_hashes[server_url] = fingerprint
            self._record_env_diagnostics(config_key, server_config, previously_baked)

            _rich_success(f"Successfully configured MCP server '{config_key}' for Claude Code")
            return True

        except Exception:
            # Do not interpolate the exception message: registry URLs and
            # other inputs may carry embedded credentials.
            _rich_error("Error configuring MCP server")
            return False
