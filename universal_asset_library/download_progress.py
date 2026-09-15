# ##### BEGIN GPL LICENSE BLOCK #####
#  SPDX-License-Identifier: GPL-3.0-or-later
# ##### END GPL LICENSE BLOCK #####
"""Active download task tracker (BlenderKit-inspired progress + cancel).

Background workers update byte progress; the main thread copies values onto
``WindowManager.ual`` for the Downloads panel / progress bar.

Concurrency: up to ``MAX_CONCURRENT_DOWNLOADS`` active tasks at once.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

MAX_CONCURRENT_DOWNLOADS = 3


@dataclass
class DownloadTask:
    task_id: str
    name: str
    phase: str = "download"  # download | extract | save | import
    progress: float = 0.0  # 0–100
    done_bytes: int = 0
    total_bytes: int = 0
    error: str = ""
    cancel_requested: bool = False
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    # Speed / ETA (updated on byte progress)
    last_done_bytes: int = 0
    last_speed_at: float = field(default_factory=time.time)
    speed_bps: float = 0.0


_LOCK = threading.Lock()
_TASKS: Dict[str, DownloadTask] = {}
_ACTIVE_TLS = threading.local()


def can_start_download() -> bool:
    """True if another concurrent download slot is free."""
    with _LOCK:
        return len(_TASKS) < MAX_CONCURRENT_DOWNLOADS


def active_download_count() -> int:
    with _LOCK:
        return len(_TASKS)


def start_task(name: str, *, phase: str = "download") -> str:
    task_id = uuid.uuid4().hex[:12]
    task = DownloadTask(task_id=task_id, name=name or "Download", phase=phase)
    with _LOCK:
        if len(_TASKS) >= MAX_CONCURRENT_DOWNLOADS:
            raise RuntimeError(
                f"Download limit reached ({MAX_CONCURRENT_DOWNLOADS} concurrent)"
            )
        _TASKS[task_id] = task
    _ACTIVE_TLS.task_id = task_id
    return task_id


def set_active_task(task_id: Optional[str]) -> None:
    _ACTIVE_TLS.task_id = task_id


def get_active_task_id() -> Optional[str]:
    return getattr(_ACTIVE_TLS, "task_id", None)


def finish_task(task_id: str, *, error: str = "") -> None:
    with _LOCK:
        _TASKS.pop(task_id, None)
    if getattr(_ACTIVE_TLS, "task_id", None) == task_id:
        _ACTIVE_TLS.task_id = None


def request_cancel(task_id: str) -> None:
    with _LOCK:
        task = _TASKS.get(task_id)
        if task is not None:
            task.cancel_requested = True
            task.updated_at = time.time()


def is_cancelled(task_id: Optional[str] = None) -> bool:
    tid = task_id or get_active_task_id()
    if not tid:
        return False
    with _LOCK:
        task = _TASKS.get(tid)
        return bool(task and task.cancel_requested)


def set_phase(task_id: str, phase: str, *, progress: Optional[float] = None) -> None:
    with _LOCK:
        task = _TASKS.get(task_id)
        if task is None:
            return
        task.phase = phase
        if progress is not None:
            task.progress = max(0.0, min(float(progress), 100.0))
        task.updated_at = time.time()


def update_bytes(task_id: str, done: int, total: int) -> None:
    with _LOCK:
        task = _TASKS.get(task_id)
        if task is None:
            return
        now = time.time()
        done = max(0, int(done))
        total = max(0, int(total))
        elapsed = max(0.05, now - task.last_speed_at)
        delta = done - task.last_done_bytes
        if delta >= 0 and elapsed > 0:
            # EMA so the label doesn't jump wildly
            instant = float(delta) / elapsed
            task.speed_bps = (task.speed_bps * 0.6) + (instant * 0.4) if task.speed_bps else instant
        task.last_done_bytes = done
        task.last_speed_at = now
        task.done_bytes = done
        task.total_bytes = total
        if total and total > 0:
            task.progress = max(0.0, min(100.0 * done / float(total), 100.0))
        else:
            task.progress = min(95.0, task.progress + 0.5)
        task.updated_at = now


def update_fraction(task_id: str, current: int, total: int) -> None:
    """Member/index progress (ZIP extract, multi-file)."""
    with _LOCK:
        task = _TASKS.get(task_id)
        if task is None:
            return
        total = max(1, int(total))
        task.progress = max(0.0, min(100.0 * float(current) / float(total), 100.0))
        task.updated_at = time.time()


def snapshot_tasks() -> List[dict]:
    with _LOCK:
        return [
            {
                "task_id": t.task_id,
                "name": t.name,
                "phase": t.phase,
                "progress": float(t.progress),
                "done_bytes": t.done_bytes,
                "total_bytes": t.total_bytes,
                "cancel_requested": t.cancel_requested,
                "speed_bps": float(t.speed_bps),
                "eta_seconds": _eta_seconds(t),
            }
            for t in _TASKS.values()
        ]


def _eta_seconds(task: DownloadTask) -> float:
    if task.speed_bps <= 1.0 or task.total_bytes <= 0:
        return -1.0
    remaining = max(0, task.total_bytes - task.done_bytes)
    return remaining / task.speed_bps


def has_active_tasks() -> bool:
    with _LOCK:
        return bool(_TASKS)


def format_bytes(n: int) -> str:
    value = float(max(0, int(n or 0)))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024.0 or unit == "GB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} GB"


def format_speed(bps: float) -> str:
    if bps <= 0:
        return ""
    return f"{format_bytes(int(bps))}/s"


def format_eta(seconds: float) -> str:
    if seconds < 0:
        return ""
    sec = int(seconds)
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m {sec % 60}s"
    return f"{sec // 3600}h {(sec % 3600) // 60}m"


def lerp_display_factor(current: float, target: float, *, rate: float = 0.38) -> float:
    """Ease ``current`` toward ``target`` (0–1). Matches Houdini strip feel in N-panel ticks."""
    cur = max(0.0, min(1.0, float(current)))
    tgt = max(0.0, min(1.0, float(target)))
    delta = tgt - cur
    if abs(delta) < 0.004:
        return tgt
    stepped = cur + delta * max(0.08, min(1.0, float(rate)))
    if abs(tgt - stepped) < 0.004:
        return tgt
    return max(0.0, min(1.0, stepped))


def indeterminate_pulse_factor(now: float, *, period_s: float = 1.15) -> float:
    """0.18–0.82 sine pulse when byte total is unknown (indeterminate download)."""
    import math

    period = max(0.4, float(period_s))
    phase = (float(now) % period) / period
    eased = 0.5 + 0.5 * math.sin(phase * math.tau)
    return 0.18 + eased * 0.64


def task_bar_target_factor(task: dict, *, now: Optional[float] = None) -> Tuple[float, bool]:
    """Return ``(factor 0–1, indeterminate)`` for UILayout.progress."""
    total = int(task.get("total_bytes") or 0)
    phase = str(task.get("phase") or "download")
    pct = float(task.get("progress") or 0.0)
    if phase == "download" and total <= 0:
        return indeterminate_pulse_factor(now if now is not None else time.time()), True
    return max(0.0, min(1.0, pct / 100.0)), False


def progress_label(task: dict) -> str:
    phase = str(task.get("phase") or "download")
    name = str(task.get("name") or "asset")
    pct = int(task.get("progress") or 0)
    speed = format_speed(float(task.get("speed_bps") or 0))
    eta = format_eta(float(task.get("eta_seconds") or -1))
    extra = ""
    if speed:
        extra = f" · {speed}"
        if eta:
            extra += f" · ETA {eta}"
    if phase == "download":
        done = int(task.get("done_bytes") or 0)
        total = int(task.get("total_bytes") or 0)
        if total > 0:
            return f"Downloading {name} — {pct}% ({format_bytes(done)} / {format_bytes(total)}){extra}"
        return f"Downloading {name} — {pct}%{extra}"
    if phase == "extract":
        return f"Extracting {name} — {pct}%"
    if phase == "save":
        return f"Saving {name} — {pct}%"
    if phase == "import":
        return f"Importing {name} — {pct}%"
    return f"{phase.title()} {name} — {pct}%"
