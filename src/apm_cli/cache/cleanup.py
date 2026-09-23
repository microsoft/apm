"""Verified, best-effort removal outcomes for explicit cache cleaning."""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

from ..utils import file_ops


def clean_cache_buckets(buckets: Iterable[Path]) -> list[str]:
    """Remove bucket children without following links; return all failure details.

    Successful removals are not rolled back. The residual check is essential:
    the read-only recovery callback in robust_rmtree can swallow OS errors.
    """
    failures: list[str] = []
    for bucket in buckets:
        try:
            if bucket.is_symlink():
                failures.append(f"{bucket}: refusing to traverse a symlinked cache bucket")
                continue
            with os.scandir(bucket) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            file_ops.robust_rmtree(path)
                        else:
                            path.unlink()
                    except FileNotFoundError:
                        pass
                    except OSError as exc:
                        failures.append(f"{path}: {exc}")
                        continue
                    try:
                        # lstat distinguishes a removed entry from an unreadable
                        # one and also detects dangling symlinks.
                        path.lstat()
                    except FileNotFoundError:
                        continue
                    except OSError as exc:
                        failures.append(f"{path}: {exc}")
                    else:
                        failures.append(f"{path}: entry remains after deletion")
        except FileNotFoundError:
            continue
        except OSError as exc:
            failures.append(f"{bucket}: {exc}")
    return failures
