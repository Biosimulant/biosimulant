"""Machine-readable run progress lines for hosts that watch a lab run.

When ``BIOSIM_PROGRESS_EVENTS=1`` is set, a lab run writes one
``BSIM_PROGRESS: {json}`` line to stderr when the simulation starts, whenever
it crosses another whole percent (or at least every few seconds), and when it
finishes. Hosts such as Biosimulant managed compute turn these lines into live
run status; other readers can ignore them.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Callable, TextIO

from .world import BioWorld, WorldEvent

BIOSIM_PROGRESS_EVENTS_ENV = "BIOSIM_PROGRESS_EVENTS"
PROGRESS_PREFIX = "BSIM_PROGRESS:"
_MIN_INTERVAL_SECONDS = 5.0

_STATUS_KEYS = {
    "t": "sim_time",
    "start": "sim_start",
    "end": "sim_end",
    "duration": "duration",
    "remaining": "sim_remaining",
    "progress": "progress",
    "progress_pct": "progress_pct",
}


def progress_events_enabled() -> bool:
    return os.environ.get(BIOSIM_PROGRESS_EVENTS_ENV, "").strip().lower() in {"1", "true", "yes"}


def attach_progress_stream(
    world: BioWorld,
    *,
    stream: TextIO | None = None,
    min_interval_seconds: float = _MIN_INTERVAL_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> Callable[[WorldEvent, dict[str, Any]], None]:
    """Subscribe a listener that writes ``BSIM_PROGRESS`` lines; return it."""
    last_emit_at = 0.0
    last_bucket = -1
    step_count = 0

    def _write(message: str, data: dict[str, Any]) -> None:
        payload: dict[str, Any] = {"phase": "running", "message": message}
        for raw_key, key in _STATUS_KEYS.items():
            value = data.get(raw_key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                payload[key] = value
        if step_count:
            payload["step"] = step_count
            payload["step_count"] = step_count
        target = stream if stream is not None else sys.stderr
        target.write(PROGRESS_PREFIX + " " + json.dumps(payload, sort_keys=True) + "\n")
        target.flush()

    def _listener(event: WorldEvent, data: dict[str, Any]) -> None:
        nonlocal last_emit_at, last_bucket, step_count
        data = data if isinstance(data, dict) else {}
        now = clock()
        if event is WorldEvent.STARTED:
            last_emit_at, last_bucket, step_count = now, -1, 0
            _write("Running simulation...", data)
            return
        if event is WorldEvent.FINISHED:
            _write("Finalizing simulation...", data)
            return
        if event is not WorldEvent.STEP:
            return
        step_count += 1
        pct = data.get("progress_pct")
        bucket = int(max(0.0, min(100.0, float(pct)))) if isinstance(pct, (int, float)) else last_bucket
        if bucket <= last_bucket and now - last_emit_at < min_interval_seconds:
            return
        last_emit_at = now
        last_bucket = max(last_bucket, bucket)
        sim_time = data.get("t")
        if isinstance(sim_time, (int, float)) and isinstance(pct, (int, float)):
            message = f"Simulated {float(sim_time):.6g} ({float(pct):.1f}%)"
        else:
            message = "Simulation running..."
        _write(message, data)

    world.on(_listener)
    return _listener
