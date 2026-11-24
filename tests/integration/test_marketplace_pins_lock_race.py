"""Both marketplace pin writers serialize on the pins lock (#5169).

A registry-side write (legacy pin adoption) holds the pins lock across its
load-modify-save; an install commit in another process waits for it and keeps
the adopted pin instead of overwriting it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from apm_cli.cache.locking import shard_lock
from apm_cli.marketplace.version_pins import _pins_path, load_ref_pins, save_ref_pins

pytestmark = [
    pytest.mark.component,
    pytest.mark.promise("P-OWN-marketplace-ref-pin-commit"),
]

_CHILD_COMMIT = """
import sys
from apm_cli.marketplace.version_pins import open_ref_pin_stage
stage = open_ref_pin_stage()
stage.add("mkt", "child", "v-child", "1.0.0", catalog="cat")
stage.commit(pins_dir=sys.argv[1])
"""


def test_a_pin_commit_waits_for_a_concurrent_writer_holding_the_pins_lock(tmp_path):
    """Two processes: the install commit never overwrites a registry-side write (#5169)."""
    pins_dir = str(tmp_path)
    save_ref_pins({}, pins_dir)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    with shard_lock(Path(_pins_path(pins_dir))):
        pins = load_ref_pins(pins_dir)
        child = subprocess.Popen([sys.executable, "-c", _CHILD_COMMIT, pins_dir], env=env)
        time.sleep(3)
        pins["cat#adopted/1.0.0"] = "v-adopted"
        save_ref_pins(pins, pins_dir)
    assert child.wait(timeout=60) == 0

    assert json.loads(Path(_pins_path(pins_dir)).read_text(encoding="utf-8")) == {
        "cat#adopted/1.0.0": "v-adopted",
        "cat#child/1.0.0": "v-child",
    }
