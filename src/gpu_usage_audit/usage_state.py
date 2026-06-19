"""NVML-only live GPU usage state classifier.

This is the cloud-facing active/idle_held/idle signal. It is intentionally
separate from classify.py, which is the older retrospective report classifier.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .model import Snapshot

U_IDLE = 5
LOW_UTIL_STREAK_WINDOW = 10


@dataclass(slots=True)
class UsageStateTracker:
    """Daemon-lifetime per-GPU state for active/idle_held/idle classification."""

    low_util_streak_by_uuid: dict[str, int] = field(default_factory=dict)
    seen_active_by_uuid: set[str] = field(default_factory=set)

    def apply(self, snapshot: Snapshot, *, sync_once: bool = False) -> None:
        compute_resident_uuids = {
            proc.gpu_uuid for proc in snapshot.procs if proc.process_type == "compute"
        }

        for gpu in snapshot.gpus:
            current_util_pct = gpu.util_pct
            if current_util_pct > U_IDLE:
                self.low_util_streak_by_uuid[gpu.uuid] = 0
                self.seen_active_by_uuid.add(gpu.uuid)
            else:
                self.low_util_streak_by_uuid[gpu.uuid] = (
                    self.low_util_streak_by_uuid.get(gpu.uuid, 0) + 1
                )

            if gpu.uuid in snapshot.compute_processes_unavailable_uuids:
                gpu.usage_state = None
                continue

            process_resident = gpu.uuid in compute_resident_uuids
            if not process_resident:
                gpu.usage_state = "idle"
            elif current_util_pct > U_IDLE:
                gpu.usage_state = "active"
            elif (
                sync_once
                or self.low_util_streak_by_uuid[gpu.uuid] >= LOW_UTIL_STREAK_WINDOW
                or gpu.uuid not in self.seen_active_by_uuid
            ):
                gpu.usage_state = "idle_held"
            else:
                gpu.usage_state = "active"


def classify_usage_states(snapshot: Snapshot, *, sync_once: bool = False) -> None:
    UsageStateTracker().apply(snapshot, sync_once=sync_once)
