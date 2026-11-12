"""Deterministic regressions for the consolidated October performance scans."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from apm_cli.commands.marketplace.plugin import _has_marketplace_block
from apm_cli.commands.uninstall.engine import _validate_uninstall_packages
from apm_cli.deps.download_strategies import DownloadDelegate
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.models.dependency.reference import DependencyReference
from apm_cli.models.dependency.selection import DependencySelection, DependencySelectionStatus

pytestmark = pytest.mark.component


@pytest.mark.parametrize(
    ("module", "dependency"),
    [
        ("apm_cli.commands.update", "git.exc"),
        ("apm_cli.commands.marketplace.plugin", "yaml"),
    ],
)
def test_command_import_defers_its_error_only_dependency(module: str, dependency: str) -> None:
    """Check the reported import edge, not unrelated transitive imports."""
    code = """
import builtins
import importlib
import json
import sys

module, dependency = sys.argv[1:]
original_import = builtins.__import__
attempts = []
def track_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name == dependency and globals and globals.get("__name__") == module:
        attempts.append(name)
    return original_import(name, globals, locals, fromlist, level)
builtins.__import__ = track_import
importlib.import_module(module)
print(json.dumps(attempts))
"""
    result = subprocess.run(
        [sys.executable, "-c", code, module, dependency],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("marketplace:\n  name: example\n", True),
        ("marketplace: {}\n", True),
        ("marketplace: null\n", False),
        ("name: example\n", False),
        ("", False),
        ("- marketplace\n", False),
        ("marketplace: [\n", False),
    ],
)
def test_marketplace_probe_retains_yaml_behavior(
    tmp_path: Path, content: str, expected: bool
) -> None:
    manifest = tmp_path / "apm.yml"
    manifest.write_text(content, encoding="utf-8")
    assert _has_marketplace_block(manifest) is expected


def test_marketplace_probe_retains_missing_and_unreadable_behavior(tmp_path: Path) -> None:
    manifest = tmp_path / "apm.yml"
    assert _has_marketplace_block(manifest) is False
    manifest.write_text("marketplace: {}\n", encoding="utf-8")
    with patch("apm_cli.utils.yaml_io.load_yaml", side_effect=OSError("permission denied")):
        assert _has_marketplace_block(manifest) is False


def _archive(entries: dict[str, bytes]) -> bytes:
    """Build real ZIP bytes without touching disk or a remote registry."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


@pytest.mark.parametrize("unrelated_count", [0, 1000])
@pytest.mark.parametrize(
    ("entries", "expected", "lookups"),
    [
        ({"repo/": b"", "repo/apm.yml": b"prefixed", "apm.yml": b"bare"}, b"prefixed", 1),
        ({"repo/": b"", "apm.yml": b"bare"}, b"bare", 2),
        ({"apm.yml": b"bare"}, b"bare", 2),
        ({}, None, 2),
    ],
)
def test_archive_member_lookup_is_bounded_and_preserves_precedence(
    entries: dict[str, bytes], expected: bytes | None, lookups: int, unrelated_count: int
) -> None:
    """No namelist allocation or linear scan, regardless of archive size."""
    archive_bytes = _archive(
        {**entries, **{f"other/{index}.txt": b"filler" for index in range(unrelated_count)}}
    )
    host = SimpleNamespace(
        registry_config=None,
        artifactory_token=None,
        _resilient_get=MagicMock(
            return_value=SimpleNamespace(status_code=200, content=archive_bytes)
        ),
    )
    original_getinfo = zipfile.ZipFile.getinfo
    queried_names: list[str] = []

    def getinfo(archive: zipfile.ZipFile, name: str) -> zipfile.ZipInfo:
        queried_names.append(name)
        return original_getinfo(archive, name)

    with (
        patch("apm_cli.deps.artifactory_entry.fetch_entry_from_archive", return_value=None),
        patch(
            "apm_cli.deps.download_strategies.build_artifactory_archive_url",
            return_value=["https://registry.example/archive.zip"],
        ),
        patch.object(zipfile.ZipFile, "namelist", side_effect=AssertionError("linear scan")),
        patch.object(zipfile.ZipFile, "getinfo", getinfo),
    ):
        delegate = DownloadDelegate(host)
        if expected is None:
            with pytest.raises(RuntimeError, match="Failed to download file"):
                delegate.download_file_from_artifactory(
                    "registry.example", "apm", "owner", "repo", "apm.yml", "main"
                )
        else:
            assert (
                delegate.download_file_from_artifactory(
                    "registry.example", "apm", "owner", "repo", "apm.yml", "main"
                )
                == expected
            )
    assert len(queried_names) == lookups
    assert queried_names[-1] == ("repo/apm.yml" if lookups == 1 else "apm.yml")


@pytest.mark.parametrize(
    "first_content",
    [b"not a zip", _archive({}), _archive({"other.yml": b"other"})],
    ids=["invalid-zip", "empty-zip", "missing-member"],
)
def test_archive_missing_member_continues_to_next_candidate(first_content: bytes) -> None:
    host = SimpleNamespace(
        registry_config=None,
        artifactory_token=None,
        _resilient_get=MagicMock(
            side_effect=[
                SimpleNamespace(status_code=200, content=first_content),
                SimpleNamespace(status_code=200, content=_archive({"apm.yml": b"found"})),
            ]
        ),
    )
    with (
        patch("apm_cli.deps.artifactory_entry.fetch_entry_from_archive", return_value=None),
        patch(
            "apm_cli.deps.download_strategies.build_artifactory_archive_url",
            return_value=[
                "https://registry.example/first.zip",
                "https://registry.example/second.zip",
            ],
        ),
    ):
        result = DownloadDelegate(host).download_file_from_artifactory(
            "registry.example", "apm", "owner", "repo", "apm.yml", "main"
        )
    assert result == b"found"
    assert host._resilient_get.call_count == 2


@pytest.mark.parametrize("entry_kind", ["string", "mapping", "model"])
@pytest.mark.parametrize("log_matches", [False, True])
def test_uninstall_deduplication_retains_manifest_objects_and_argument_order(
    entry_kind: str, log_matches: bool
) -> None:
    declarations = ["org/first", "org/second"]
    if entry_kind == "mapping":
        entries = [{"git": name, "ref": "main"} for name in declarations]
    elif entry_kind == "model":
        entries = [DependencyReference.parse(name) for name in declarations]
    else:
        entries = declarations
    logger = MagicMock()
    removed, missing = _validate_uninstall_packages(
        ["org/second", "org/first", "org/second", "org/missing"],
        entries,
        logger,
        log_matches=log_matches,
    )
    assert len(removed) == 2
    assert removed[0] is entries[1]
    assert removed[1] is entries[0]
    assert missing == ["org/missing"]
    assert logger.progress.call_count == (3 if log_matches else 0)


def test_uninstall_alias_and_exact_path_remove_one_unhashable_entry() -> None:
    entry = {"path": "../packages/example"}
    locked = LockedDependency(
        repo_url="_local/example", source="local", local_path="../packages/example"
    )
    lockfile = LockFile(dependencies={locked.get_unique_key(): locked})
    removed, missing = _validate_uninstall_packages(
        ["_local/example", "../packages/example", "_local/example"],
        [entry],
        MagicMock(),
        lockfile=lockfile,
    )
    assert len(removed) == 1
    assert removed[0] is entry
    assert missing == []


@pytest.mark.parametrize("count", [10, 1000])
def test_uninstall_deduplication_does_not_compare_previously_selected_entries(count: int) -> None:
    """Isolate deduplication cost from the unchanged canonical selection cost."""
    comparisons = 0

    class CountedEntry(dict):
        def __eq__(self, other: object) -> bool:
            nonlocal comparisons
            comparisons += 1
            return super().__eq__(other)

    entries = [CountedEntry(git=f"org/package-{index}") for index in range(count)]
    selections = [
        DependencySelection(DependencySelectionStatus.MATCHED, entry, 1) for entry in entries
    ]
    with patch(
        "apm_cli.commands.uninstall.engine.select_manifest_dependency",
        side_effect=selections + selections,
    ):
        removed, missing = _validate_uninstall_packages(
            [entry["git"] for entry in entries] * 2,
            entries,
            MagicMock(),
            log_matches=False,
        )
    assert len(removed) == count
    assert all(actual is expected for actual, expected in zip(removed, entries, strict=True))
    assert missing == []
    assert comparisons == 0
