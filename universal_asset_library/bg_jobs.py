# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Fixed-size background job pool (no bpy).

``submit()`` always returns immediately. At most ``MAX_WORKERS`` jobs run at
once; extra work waits on an unbounded queue. Callers (Blender operators /
timers) must never block waiting for a slot — that froze the whole UI when
LazyTextures / ambientCG held a worker for a slow HTTPS round-trip.
"""

from __future__ import annotations

import queue
import threading
from typing import Callable, Optional

from .diagnostics_redact import format_logged_error

MAX_WORKERS = 8

_JOBS: "queue.Queue[Optional[tuple]]" = queue.Queue()
_STARTED = False
_START_LOCK = threading.Lock()


def pending_job_count() -> int:
    """Approx queued (not yet running) jobs."""
    try:
        return int(_JOBS.qsize())
    except Exception:
        return 0


def start_pool() -> None:
    """Idempotent: spawn ``MAX_WORKERS`` daemon threads."""
    global _STARTED
    with _START_LOCK:
        if _STARTED:
            return
        for index in range(MAX_WORKERS):
            threading.Thread(
                target=_worker_loop,
                name="UAL-BG-{}".format(index),
                daemon=True,
            ).start()
        _STARTED = True


def submit(
    work: Callable[[], None],
    on_complete: Optional[Callable[[], None]] = None,
) -> None:
    """Queue ``work``. Never blocks the caller, even when all workers are busy."""
    start_pool()
    _JOBS.put((work, on_complete))


def _worker_loop() -> None:
    while True:
        item = _JOBS.get()
        if item is None:
            return
        work, on_complete = item
        try:
            work()
        except Exception as exc:  # noqa: BLE001 — never kill a pool thread
            print(format_logged_error(exc, label="bg error"))
        finally:
            if on_complete is not None:
                try:
                    on_complete()
                except Exception as exc:  # noqa: BLE001
                    print(format_logged_error(exc, label="bg complete error"))
