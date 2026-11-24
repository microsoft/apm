"""Swap a deploy path's parent for a symbolic link between the journal and the write.

``tests/integration/test_secure_fast_install_invariants_lifecycle.py`` puts this
directory on ``PYTHONPATH`` for one engine run (``python -m apm_cli.cli``) and
sets ``APM_TEST_SWAP_PATH`` (POSIX, relative to the project) and
``APM_TEST_SWAP_TO`` (an absolute directory outside the project).

The first time the write seam records that path, after the journal has seen
it and before any byte is written, the path's parent directory is renamed to
``<name>.moved`` and replaced by a symbolic link to ``APM_TEST_SWAP_TO``. This
is the race a concurrent process wins against a containment proof made only
before the write. A write-time containment check (an ``openat`` chain with
``O_NOFOLLOW``, or a held directory handle) never follows that link.

Python runs only the first ``sitecustomize`` on its path, so this one runs
the next one (the isolated environment's network guard) before anything else.
"""

import os
import runpy
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
for _entry in sys.path:
    _candidate = os.path.join(_entry or os.curdir, "sitecustomize.py")
    if os.path.abspath(_entry or os.curdir) != _HERE and os.path.isfile(_candidate):
        runpy.run_path(_candidate, run_name="sitecustomize")
        break

_TARGET = os.environ.get("APM_TEST_SWAP_PATH", "")
_OUTSIDE = os.environ.get("APM_TEST_SWAP_TO", "")

if _TARGET and _OUTSIDE:
    from pathlib import Path

    from apm_cli.integration import write_seam

    _record = write_seam.record_before_write
    _swapped = []

    def _record_then_swap(path, *args, **kwargs):
        result = _record(path, *args, **kwargs)
        if not _swapped and Path(path).as_posix().endswith(f"/{_TARGET}"):
            parent = Path(path).parent
            os.rename(parent, parent.with_name(parent.name + ".moved"))
            os.symlink(_OUTSIDE, parent, target_is_directory=True)
            _swapped.append(str(path))
        return result

    write_seam.record_before_write = _record_then_swap
