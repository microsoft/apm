"""Where one scope's native plugins live and which marketplace declares them.

User scope deploys to ``~/.agents/plugins`` under an absolute marketplace path
named per APM home. Project scope deploys to the committed
``<project>/.agents/plugins`` under the relative path ``./.agents/plugins``,
named from the project identity in ``apm.yml`` so every clone agrees
(OpenAPM req-na-003 and req-na-007).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apm_cli.utils.content_hash import compute_native_plugin_hash, native_plugin_files
from apm_cli.utils.path_security import PathTraversalError

from .errors import NativePluginError
from .layout import plugins_root
from .record import PLUGINS_RELATIVE

PROJECT_MARKETPLACE_PATH = f"./{PLUGINS_RELATIVE}"
"""Marketplace path written at project scope, relative to the repository root."""


# TODO(#3989): replace with canonical pin on rebase (compute_native_plugin_pin).
def compute_native_plugin_portable_hash(plugin_root: Path) -> str:
    """Portable digest of the native plugin file set: path and bytes, no exec bit.

    A committed project-scope plugin root is checked out on every machine, and
    the exec bit is per machine (Windows and ``core.fileMode=false`` drop it),
    so the project lock records this digest. For each file in
    :func:`native_plugin_files` order it hashes the POSIX relative path, one
    NUL byte and the raw SHA-256 of the file's bytes.
    """
    if plugin_root.is_symlink():
        raise PathTraversalError(f"Native plugin integrity refuses symlink: {plugin_root}")
    hasher = hashlib.sha256()
    for relative in native_plugin_files(plugin_root):
        hasher.update(relative.as_posix().encode("utf-8") + b"\0")
        hasher.update(hashlib.sha256((plugin_root / relative).read_bytes()).digest())
    return f"sha256:{hasher.hexdigest()}"


@dataclass(frozen=True)
class NativeBinding:
    """One scope's profile root and the marketplace binding that declares it."""

    root: Path
    """Absolute profile root (``<deploy root>/.agents/plugins``)."""
    marketplace: str
    path: str
    """Marketplace ``path`` written to settings and recorded in the lock."""
    project: bool

    @property
    def pair(self) -> tuple[str, str]:
        return (self.marketplace, self.path)

    @property
    def label(self) -> str:
        """How diagnostics name the profile root."""
        return PLUGINS_RELATIVE if self.project else f"~/{PLUGINS_RELATIVE}"

    def fingerprint(self, plugin_root: Path) -> str:
        """Root digest recorded in this scope's lock (see :func:`native_fingerprint`)."""
        return native_fingerprint(plugin_root, project=self.project)


def native_fingerprint(plugin_root: Path, *, project: bool) -> str:
    """Root digest a scope's lock records for one plugin root.

    Project roots are committed and checked out on every machine, so their
    digest covers paths and bytes only; user roots also bind the exec bit.
    """
    digest = compute_native_plugin_portable_hash if project else compute_native_plugin_hash
    try:
        return digest(plugin_root)
    except (OSError, PathTraversalError) as exc:
        raise NativePluginError(
            f"Cannot fingerprint native plugin {plugin_root}: unreadable data, unsafe path "
            "or symlink. Restore the intact package before retrying."
        ) from exc


def user_marketplace_name(apm_home: Path) -> str:
    """Deterministic user-scope marketplace name, unique per APM home (req-na-007)."""
    digest = hashlib.sha256(str(Path(apm_home).resolve()).encode("utf-8")).hexdigest()
    return f"apm-u-{digest[:8]}"


def project_marketplace_name(project_name: str) -> str:
    """Deterministic project-scope name from the ``apm.yml`` name (req-na-007).

    It depends on no user, host or checkout path, so every clone and worktree
    of the project declares the same marketplace.
    """
    digest = hashlib.sha256(f"project:{project_name}".encode()).hexdigest()
    return f"apm-p-{digest[:8]}"


def project_identity(project_root: Path) -> str:
    """Return the project identity: the ``name`` declared in ``apm.yml``."""
    from apm_cli.utils.yaml_io import load_yaml

    manifest = Path(project_root) / "apm.yml"
    try:
        data = load_yaml(manifest) if manifest.is_file() else None
    except Exception as exc:  # yaml errors are not ValueError
        raise NativePluginError(f"Cannot read {manifest} to name the native marketplace.") from exc
    name = data.get("name") if isinstance(data, dict) else None
    if not isinstance(name, str) or not name.strip():
        raise NativePluginError(
            f"Native plugins at project scope need a 'name' in {manifest}: it names the "
            "project's plugin marketplace. Add one, then run the install again."
        )
    return name.strip()


def user_binding(apm_home: Path, deploy_root: Path) -> NativeBinding:
    root = plugins_root(deploy_root)
    return NativeBinding(root, user_marketplace_name(apm_home), str(root), project=False)


def project_binding(project_root: Path, manifest_root: Path | None = None) -> NativeBinding:
    """Project binding under *project_root*, named from ``apm.yml`` in *manifest_root*.

    ``apm install --root DIR`` deploys under DIR while ``apm.yml`` stays in the
    working directory, so the two can differ.
    """
    return NativeBinding(
        plugins_root(project_root),
        project_marketplace_name(project_identity(manifest_root or project_root)),
        PROJECT_MARKETPLACE_PATH,
        project=True,
    )


def binding_for(
    *, scope: Any, modules_dir: Path, deploy_root: Path, manifest_root: Path | None = None
) -> NativeBinding:
    """The binding for *scope*; *modules_dir* is that scope's ``apm_modules``."""
    from apm_cli.core.scope import is_user_scope

    if is_user_scope(scope):
        return user_binding(Path(modules_dir).absolute().parent, deploy_root)
    return project_binding(deploy_root, manifest_root)
