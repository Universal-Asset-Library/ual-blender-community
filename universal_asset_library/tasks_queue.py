# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Main-thread deferred task queue via bpy.app.timers (no bpy on workers).

Caps (session stress guards):
- At most ``MAX_BG_THREADS`` concurrent ``run_in_background`` workers
  (fixed pool in ``bg_jobs`` — submit never blocks the caller)
- At most ``MAX_DRAIN_PER_TICK`` main-thread callbacks per timer slice
"""

from __future__ import annotations

import queue
from typing import Callable, Optional

import bpy

from . import bg_jobs
from .diagnostics_redact import format_logged_error

_TASKS: "queue.Queue[Callable[[], None]]" = queue.Queue()
_REGISTERED = False
_TIMER_INTERVAL = 0.05
MAX_BG_THREADS = 8
MAX_DRAIN_PER_TICK = 32


def add_task(fn: Callable[[], None]) -> None:
    """Schedule ``fn`` to run on the Blender main thread."""
    _TASKS.put(fn)


def pending_task_count() -> int:
    """Approx queued main-thread callbacks (for stress / diagnostics)."""
    try:
        return int(_TASKS.qsize())
    except Exception:
        return 0


def _drain() -> Optional[float]:
    """Run up to ``MAX_DRAIN_PER_TICK`` tasks so one timer tick cannot hitch UI."""
    ran = 0
    try:
        while ran < MAX_DRAIN_PER_TICK:
            fn = _TASKS.get_nowait()
            ran += 1
            try:
                fn()
            except Exception as exc:  # noqa: BLE001 — surface in status, don't kill timer
                print(format_logged_error(exc, label="task error"))
    except queue.Empty:
        pass
    return _TIMER_INTERVAL


def register() -> None:
    global _REGISTERED
    if _REGISTERED:
        return
    bg_jobs.start_pool()
    bpy.app.timers.register(_drain, first_interval=_TIMER_INTERVAL, persistent=True)
    _REGISTERED = True


def unregister() -> None:
    global _REGISTERED
    if _REGISTERED and bpy.app.timers.is_registered(_drain):
        bpy.app.timers.unregister(_drain)
    _REGISTERED = False
    while not _TASKS.empty():
        try:
            _TASKS.get_nowait()
        except queue.Empty:
            break


def run_in_background(work: Callable[[], None], on_done: Optional[Callable[[], None]] = None) -> None:
    """Run ``work`` off-main-thread; optionally schedule ``on_done`` on main thread.

    Always returns immediately. Overflow jobs wait on the pool queue — the
    caller (operator / timer) is never blocked waiting for a worker slot.
    """

    def _complete() -> None:
        if on_done is not None:
            add_task(on_done)

    bg_jobs.submit(work, on_complete=_complete if on_done is not None else None)
