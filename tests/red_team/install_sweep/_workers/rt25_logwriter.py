"""Round-25 worker: a concurrent writer to the shared scripts.log.

Drives ``_append_to_script_log`` directly (the exact append path
``_execute_command`` uses) from its OWN process, many times, with a LARGE
single-writer-unique stdout payload. Run 8+ of these against the same
``APM_HOME`` to test whether the single ``os.write`` under ``O_APPEND`` keeps
each multi-line entry contiguous (no interleaving / torn line) when the entry
exceeds PIPE_BUF (512) and even a page (4 KiB).

Each writer stamps every captured token with its own index ``K{idx:02d}K`` so
the harness can detect any byte from another writer landing inside this
writer's entry.

Usage:
    python rt25_logwriter.py <apm_home> <idx> <count>
"""

from __future__ import annotations

import os
import sys

apm_home = sys.argv[1]
idx = int(sys.argv[2])
count = int(sys.argv[3])

os.environ["APM_HOME"] = apm_home
# Silence any APM_NO_SCRIPTS short-circuit irrelevant to the log path.
os.environ.pop("APM_NO_SCRIPTS", None)

from apm_cli.core import script_executors as se  # noqa: E402

# A 4-char token repeated to fill the per-field truncation cap (4096 chars).
# Every token carries THIS writer's index; a torn write would splice a token
# bearing a different index into the middle of one of our entries.
TOKEN = f"K{idx:02d}K"
PAYLOAD = TOKEN * 1024  # 4096 chars, exactly _MAX_LOG_FIELD_CHARS

for _ in range(count):
    se._append_to_script_log(
        event_name=f"W{idx:02d}",
        script_type="command",
        target=f"cmd{idx:02d}",
        stdout=PAYLOAD,
        stderr=PAYLOAD,
        exit_code=0,
        status="ok",
    )

os._exit(0)
