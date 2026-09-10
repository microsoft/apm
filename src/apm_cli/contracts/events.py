"""One ordered event handoff for a local invocation."""

import time
from typing import Literal

from .models import EventSink, RunEvent


class EventEmitter:
    """Assign observation order in the conductor/supervisor thread."""

    def __init__(self, run_id: str, sink: EventSink) -> None:
        self.run_id = run_id
        self.sink = sink
        self.started = time.monotonic()
        self.sequence = 0

    def emit(
        self,
        kind: str,
        *,
        source: Literal["engine", "harness", "checker"] = "engine",
        **data: object,
    ) -> None:
        """Deliver one observation without deriving assessment outcomes."""
        self.sequence += 1
        self.sink(
            RunEvent(
                run_id=self.run_id,
                sequence=self.sequence,
                elapsed_seconds=time.monotonic() - self.started,
                kind=kind,
                source=source,
                data=data,
            )
        )
