"""Test-only native handles and operation observations for staging failures."""

from __future__ import annotations

import atexit
import ctypes
import json
import os
import subprocess
from pathlib import Path


def hold_path(path: Path) -> tuple[ctypes.CDLL, int]:
    """Open a real Windows file or directory handle without delete sharing."""
    if os.name != "nt":
        raise RuntimeError("Directory sharing-denial proof requires native Windows")
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateFileW(str(path), 0x80000000, 3, None, 3, 0x02000000, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    return kernel, handle


def release_directory(held: tuple[ctypes.CDLL, int]) -> None:
    """Release exactly the handle opened by this fixture."""
    kernel, handle = held
    if not kernel.CloseHandle(handle):
        raise ctypes.WinError(ctypes.get_last_error())


def install_probe(live: str, evidence: str, mode: str) -> None:
    """Observe native operations; inject handles, never simulated OS errors."""
    destination = Path(live)
    report = Path(evidence)
    original_replace = Path.replace
    original_rmdir = os.rmdir
    original_unlink = os.unlink
    handles: list[tuple[ctypes.CDLL, int]] = []
    activated = False
    observed_root: Path | None = None

    def record(operation: str, path: Path, error: OSError | None = None) -> None:
        if observed_root is None or not path.is_relative_to(observed_root):
            return
        event = {"operation": operation, "path": str(path)}
        if error is not None:
            event.update(error=str(error), winerror=error.winerror)
        with report.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=True) + "\n")

    def close_handles() -> None:
        while handles:
            release_directory(handles.pop())

    def replace(path: Path, target: Path) -> Path:
        nonlocal activated, observed_root
        if Path(target) == destination and path.parent.name == "replacements" and not activated:
            activated = True
            observed_root = path.parent.parent
            handles.append(hold_path(path))
            record("held-candidate", path)
            if mode == "restore-denied":
                backups = list((path.parent.parent / "backups").iterdir())
                if len(backups) != 1:
                    raise RuntimeError("Expected exactly one preserved prior package")
                handles.append(hold_path(backups[0]))
                record("held-backup", backups[0])
        try:
            return original_replace(path, target)
        except OSError as error:
            record("replace", path, error)
            if mode == "transient":
                close_handles()
            raise

    def rmdir(path: str, *args: object, **kwargs: object) -> None:
        directory = Path(path)
        try:
            return original_rmdir(path, *args, **kwargs)
        except OSError as error:
            record("rmdir", directory, error)
            raise

    def unlink(path: str, *args: object, **kwargs: object) -> None:
        try:
            return original_unlink(path, *args, **kwargs)
        except OSError as error:
            record("unlink", Path(path), error)
            raise

    if mode == "download-denied":
        from apm_cli.deps import github_downloader

        original_checkout = github_downloader.checkout_git_worktree

        def checkout(worktree: Path, ref: str, **kwargs: object) -> None:
            nonlocal observed_root
            if worktree.parent.name == "replacements" and not handles:
                if not (worktree / "apm.yml").is_file():
                    raise RuntimeError("Expected cloned package bytes before checkout denial")
                index = worktree / ".git" / "index"
                observed_root = worktree.parent.parent
                handles.extend((hold_path(index), hold_path(worktree)))
                record("held-download-index", index)
            try:
                original_checkout(worktree, ref, **kwargs)
            except subprocess.CalledProcessError as error:
                with report.open("a", encoding="utf-8") as stream:
                    stream.write(
                        json.dumps(
                            {
                                "operation": "checkout-failed",
                                "path": str(worktree),
                                "error": error.stderr.strip(),
                                "returncode": error.returncode,
                            }
                        )
                        + "\n"
                    )
                raise

        github_downloader.checkout_git_worktree = checkout

    Path.replace = replace
    os.rmdir = rmdir
    os.unlink = unlink
    atexit.register(close_handles)
