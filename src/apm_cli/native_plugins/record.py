"""The ``native_plugin`` lock record: APM's only ownership evidence.

A dependency that APM deploys as an intact native plugin records the deployed
plugin name, its root under the user's ``.agents/plugins`` directory, the
complete native fingerprint of the approved bytes and the harnesses it is
declared to. Settings keys and deployed roots are derived from these records
in the previous and next lockfile; no receipt file exists beside the lock.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

PLUGINS_RELATIVE = ".agents/plugins"
_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]*")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}")
KNOWN_HARNESSES = frozenset({"claude"})


class NativePluginRecordError(ValueError):
    """A ``native_plugin`` lock record is malformed."""


@dataclass(frozen=True)
class NativePluginRecord:
    """One deployed native plugin owned by one locked dependency."""

    name: str
    fingerprint: str
    harnesses: tuple[str, ...]
    marketplace: str
    """Settings marketplace name the plugin was declared under (req-na-007)."""
    marketplace_path: str
    """Directory that marketplace pointed at: the binding retries must match."""

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or _NAME.fullmatch(self.name) is None:
            raise NativePluginRecordError(f"invalid native plugin name: {self.name!r}")
        if not isinstance(self.fingerprint, str) or not _FINGERPRINT.fullmatch(self.fingerprint):
            raise NativePluginRecordError("invalid native plugin fingerprint")
        harnesses = tuple(self.harnesses)
        if not harnesses or tuple(sorted(set(harnesses))) != harnesses:
            raise NativePluginRecordError("native plugin harnesses must be sorted and unique")
        if not set(harnesses) <= KNOWN_HARNESSES:
            raise NativePluginRecordError(f"unknown native plugin harness in {harnesses}")
        if not isinstance(self.marketplace, str) or _NAME.fullmatch(self.marketplace) is None:
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
            "harnesses": list(self.harnesses),
            "marketplace": {"name": self.marketplace, "path": self.marketplace_path},
        }

    @property
    def binding(self) -> tuple[str, str]:
        return (self.marketplace, self.marketplace_path)

    @classmethod
    def from_lock_data(cls, data: Any) -> NativePluginRecord:
        keys = {"name", "root", "fingerprint", "harnesses", "marketplace"}
        if not isinstance(data, dict) or set(data) != keys:
            raise NativePluginRecordError(
                "native_plugin must map name, root, fingerprint, harnesses, marketplace"
            )
        harnesses = data["harnesses"]
        if not isinstance(harnesses, list) or not all(isinstance(h, str) for h in harnesses):
            raise NativePluginRecordError("native_plugin harnesses must be a list of names")
        binding = data["marketplace"]
        if not isinstance(binding, dict) or set(binding) != {"name", "path"}:
            raise NativePluginRecordError("native_plugin marketplace must map name and path")
        record = cls(
            data["name"], data["fingerprint"], tuple(harnesses), binding["name"], binding["path"]
        )
        if data["root"] != record.root:
            raise NativePluginRecordError(f"native_plugin root must be {record.root}")
        return record


def lock_native_plugins(lockfile: Any, harness: str) -> dict[str, NativePluginRecord]:
    """Return ``{dependency_key: record}`` for one harness, or ``{}`` without a lock."""
    if lockfile is None:
        return {}
    return {
        key: dependency.native_plugin
        for key, dependency in lockfile.dependencies.items()
        if dependency.native_plugin is not None and harness in dependency.native_plugin.harnesses
    }
