"""The ``native_plugin`` lock record: APM's only ownership evidence.

A dependency that APM deploys as an intact native plugin records the deployed
plugin name, its root under the user's ``.agents/plugins`` directory, the
complete native fingerprint of the deployed bytes and the marketplace binding
it was declared under. Which harness loads it follows from the entry's
targets, so the record does not repeat them. Settings keys and deployed roots are derived from these records
in the previous and next lockfile; no receipt file exists beside the lock.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

PLUGINS_RELATIVE = ".agents/plugins"
_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]*")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}")


def is_safe_plugin_name(value: object) -> bool:
    """Whether *value* is a plugin name safe as a directory and settings key."""
    return isinstance(value, str) and _NAME.fullmatch(value) is not None


class NativePluginRecordError(ValueError):
    """A ``native_plugin`` lock record is malformed."""


@dataclass(frozen=True)
class NativePluginRecord:
    """One deployed native plugin owned by one locked dependency."""

    name: str
    fingerprint: str
    marketplace: str
    """Settings marketplace name the plugin was declared under (req-na-007)."""
    marketplace_path: str
    """Directory that marketplace pointed at: the binding retries must match."""

    def __post_init__(self) -> None:
        if not is_safe_plugin_name(self.name):
            raise NativePluginRecordError(f"invalid native plugin name: {self.name!r}")
        if not isinstance(self.fingerprint, str) or not _FINGERPRINT.fullmatch(self.fingerprint):
            raise NativePluginRecordError("invalid native plugin fingerprint")
        if not is_safe_plugin_name(self.marketplace):
            raise NativePluginRecordError("invalid native plugin marketplace name")
        if not isinstance(self.marketplace_path, str) or not self.marketplace_path:
            raise NativePluginRecordError("native plugin marketplace path is required")

    @property
    def root(self) -> str:
        """Deploy-root-relative POSIX path of the deployed plugin root."""
        return f"{PLUGINS_RELATIVE}/{self.name}"

    def to_lock_data(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "root": self.root,
            "fingerprint": self.fingerprint,
            "marketplace": {"name": self.marketplace, "path": self.marketplace_path},
        }

    @property
    def binding(self) -> tuple[str, str]:
        return (self.marketplace, self.marketplace_path)

    @classmethod
    def from_lock_data(cls, data: Any) -> NativePluginRecord:
        keys = {"name", "root", "fingerprint", "marketplace"}
        if not isinstance(data, dict) or set(data) != keys:
            raise NativePluginRecordError(
                "native_plugin must map name, root, fingerprint, marketplace"
            )
        binding = data["marketplace"]
        if not isinstance(binding, dict) or set(binding) != {"name", "path"}:
            raise NativePluginRecordError("native_plugin marketplace must map name and path")
        record = cls(data["name"], data["fingerprint"], binding["name"], binding["path"])
        if data["root"] != record.root:
            raise NativePluginRecordError(f"native_plugin root must be {record.root}")
        return record


def lock_native_plugins(lockfile: Any) -> dict[str, NativePluginRecord]:
    """Return ``{dependency_key: record}`` from *lockfile*, or ``{}`` without a lock."""
    if lockfile is None:
        return {}
    return {
        key: dependency.native_plugin
        for key, dependency in lockfile.dependencies.items()
        if dependency.native_plugin is not None
    }
