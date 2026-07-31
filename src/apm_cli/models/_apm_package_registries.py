"""Registry block parsing helpers for APMPackage.from_apm_yml.

Extracted from apm_package.py to keep that module under the 800-line budget.
Both functions are private implementation details of APMPackage; they are
NOT re-exported and should only be called from apm_package.py.
"""

from __future__ import annotations

from pathlib import Path


def _parse_v01_registries_block(
    data: dict,
    apm_yml_path: Path,
) -> tuple[dict[str, str] | None, str | None]:
    """Parse the normative OpenAPM v0.1 registry map."""
    raw = data.get("registries")
    if raw is None:
        return None, None
    if not isinstance(raw, dict):
        raise ValueError(f"OpenAPM v0.1 'registries:' in {apm_yml_path} must be a mapping")
    registries: dict[str, str] = {}
    default_name = raw.get("default")
    for name, value in raw.items():
        if name == "default":
            continue
        body = {"url": value} if isinstance(value, str) else value
        if not isinstance(name, str) or not isinstance(body, dict):
            raise ValueError("OpenAPM v0.1 registry entries must be named strings or objects")
        url = body.get("url")
        if not isinstance(url, str) or not url.startswith(("https://", "http://")):
            raise ValueError(f"OpenAPM v0.1 registry {name!r} requires an HTTP(S) url")
        registries[name] = url
        aliases = body.get("aliases", [])
        if aliases is not None and not isinstance(aliases, list):
            raise ValueError(f"OpenAPM v0.1 registry {name!r} aliases must be a list")
        for alias in aliases or []:
            if not isinstance(alias, str) or not alias:
                raise ValueError(f"OpenAPM v0.1 registry {name!r} has an invalid alias")
            registries[alias] = url
    if default_name is not None and (
        not isinstance(default_name, str) or default_name not in registries
    ):
        raise ValueError("OpenAPM v0.1 registries.default must name a declared registry")
    return registries or None, default_name


def _parse_registries_block(data: dict, apm_yml_path: Path, manifest_contract):
    """Parse the top-level ``registries:`` block per design SS3.1.

    Schema::

        registries:
          corp-main:
            url: https://registry.corp.example.com/apm
          corp-other:
            url: https://other.example.com/apm
          default: corp-main           # optional; routes unscoped deps here

    Returns ``(registries_map, default_name)`` where *registries_map* is
    ``{name: url}`` and *default_name* is the value of ``default:`` (or
    ``None``). Absent block returns ``(None, None)``.
    """
    from ..models.manifest_contract import ManifestContract

    if manifest_contract is ManifestContract.OPENAPM_V01:
        return _parse_v01_registries_block(data, apm_yml_path)

    raw = data.get("registries")
    if raw is None:
        return None, None
    if raw != {}:
        from ..deps.registry.feature_gate import require_package_registry_enabled

        require_package_registry_enabled("Top-level 'registries:' blocks")
    if not isinstance(raw, dict):
        raise ValueError(
            f"Top-level 'registries:' block in {apm_yml_path} must be a "
            f"mapping (name -> {{url: ...}})"
        )

    default_value = raw.get("default")
    registries_map: dict[str, str] = {}
    for name, body in raw.items():
        if name == "default":
            continue
        if not isinstance(name, str) or not name.strip():
            raise ValueError(
                f"Registry name in 'registries:' block must be a non-empty string (got {name!r})"
            )
        if not isinstance(body, dict):
            raise ValueError(
                f"Registry {name!r} must be a mapping with at least 'url:' "
                f"(got {type(body).__name__})"
            )
        # Token trap: tokens must never appear in repo-tracked YAML files.
        if "token" in body:
            from ..deps.registry.auth import registry_token_env_var

            raise ValueError(
                f"Registry {name!r}: 'token' must not appear in apm.yml. "
                f"Use the {registry_token_env_var(name)} "
                f"environment variable or 'apm config set registry.{name}.token <value>'."
            )
        url = body.get("url")
        if not isinstance(url, str) or not url.strip():
            raise ValueError(f"Registry {name!r} is missing required field 'url:'")
        url = url.strip()
        if not url.startswith(("https://", "http://")):
            raise ValueError(
                f"Registry {name!r} URL must start with https:// or http:// (got {url!r})"
            )
        # Reject any unknown keys to catch typos early.
        unknown = set(body.keys()) - {"url"}
        if unknown:
            raise ValueError(
                f"Registry {name!r} has unknown fields: {sorted(unknown)} (known fields: ['url'])"
            )
        registries_map[name] = url

    default_name: str | None = None
    if default_value is not None:
        if not isinstance(default_value, str) or not default_value.strip():
            raise ValueError(
                f"'registries.default' in {apm_yml_path} must be a non-empty "
                f"string naming one of the configured registries"
            )
        default_name = default_value.strip()
        if default_name not in registries_map:
            raise ValueError(
                f"'registries.default: {default_name}' refers to an "
                f"unconfigured registry. Configured: {sorted(registries_map.keys())}"
            )

    if not registries_map and default_name is None:
        return None, None

    return registries_map, default_name
