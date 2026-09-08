"""Deterministic wall-clock scheduling for the seeded MES dataset.

The seed's business data (which lots exist, which steps they ran, which
equipment and operator, what went wrong) comes from a shared ``Random(42)``.
Timing must NOT be drawn from that generator: inserting draws into it would
shift every subsequent value and change the dataset itself. Timing therefore
uses its own generator, seeded with ``SCHEDULE_SEED``.

This module knows nothing about SQLite. It takes planned runs, hands them
start/end timestamps that respect equipment contention, and shifts the whole
schedule so the very last run ends exactly at the anchor.
"""

from __future__ import annotations

import os
import random
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Protocol

SCHEDULE_SEED = 20260904

# Lots enter the fab this many minutes apart. Tuned so 16 lots / 91 runs span
# roughly 65 hours: shorter intervals saturate the bottleneck furnace and stop
# shortening the makespan, longer ones push it past three days.
RELEASE_INTERVAL_MIN = 240

# Inclusive minute bands per step, loosely following real fab step times.
STEP_DURATION_MIN: dict[str, tuple[int, int]] = {
    "DIFF": (90, 180),
    "PHOTO": (30, 90),
    "ETCH": (40, 120),
    "IMPL": (30, 60),
    "CVD": (60, 180),
    "CMP": (30, 60),
    "METRO": (15, 30),
    "TEST": (120, 240),
}

# Wafer transport + queue time between consecutive steps of one lot.
TRANSPORT_MIN = (10, 40)

ANCHOR_ENV = "MES_ANCHOR"

# The instant the newest seeded process result finishes, when nothing overrides
# it. A constant rather than a clock reading, because the whole dataset hangs
# off it: every (lot, step) in_time/out_time window is measured back from here.
#
# The /data volume is EmptyDir and the app scales to zero, so the seed re-runs
# on every replica start and again on every deploy. Anchoring to "now" would
# hand out different windows each time, and external stores that record sensor
# data against those windows would silently stop lining up. Pinning it here
# makes a rebuilt database byte-identical to the one it replaced.
DEFAULT_ANCHOR = "2026-09-01T00:00:00+00:00"


class PlannedRun(Protocol):
    lot_key: Any
    step_code: str
    eqp_id: str | None
    in_time: str
    out_time: str


def iso(dt: datetime) -> str:
    """Format like mes_core.db._now_iso: UTC, second precision, offset form."""
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def resolve_anchor() -> datetime:
    """The instant the newest process result finishes.

    Defaults to :data:`DEFAULT_ANCHOR` so the dataset is identical on every
    cold start *and* every deploy. ``MES_ANCHOR`` overrides it for a
    deliberate move; an empty value is treated as unset.
    """
    raw = os.environ.get(ANCHOR_ENV, "").strip() or DEFAULT_ANCHOR
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


def assign_times(
    runs: Iterable[PlannedRun],
    anchor: datetime,
    sched: random.Random,
) -> dict[Any, datetime]:
    """Fill in ``in_time``/``out_time`` and return each lot's release time.

    ``runs`` must be grouped by lot, and ordered by route sequence within each
    lot. Times are computed in minutes from an arbitrary zero, then the whole
    schedule is translated so ``max(out_time) == anchor``. Translating instead
    of clamping is what keeps every run in the past without dropping any.

    ``anchor`` must be timezone-aware. A naive one would reach
    ``datetime.astimezone`` in :func:`iso`, which reads naive input as *local*
    time -- a silent shift of the machine's UTC offset, invisible in a UTC
    container and nine hours wrong on a Seoul laptop.
    """
    if anchor.tzinfo is None:
        raise ValueError("anchor must be timezone-aware; got naive %r" % anchor)

    runs = list(runs)
    if not runs:
        return {}

    lot_order: list[Any] = []
    for run in runs:
        if run.lot_key not in lot_order:
            lot_order.append(run.lot_key)

    release = {lot: i * RELEASE_INTERVAL_MIN for i, lot in enumerate(lot_order)}
    lot_ready = dict(release)
    eqp_free: dict[str, int] = {}
    spans: list[tuple[int, int]] = []

    for run in runs:
        low, high = STEP_DURATION_MIN[run.step_code]
        duration = sched.randint(low, high)
        start = lot_ready[run.lot_key]
        if run.eqp_id is not None:
            start = max(start, eqp_free.get(run.eqp_id, 0))
            eqp_free[run.eqp_id] = start + duration
        end = start + duration
        spans.append((start, end))
        lot_ready[run.lot_key] = end + sched.randint(*TRANSPORT_MIN)

    makespan = max(end for _start, end in spans)
    for run, (start, end) in zip(runs, spans):
        run.in_time = iso(anchor - timedelta(minutes=makespan - start))
        run.out_time = iso(anchor - timedelta(minutes=makespan - end))

    return {
        lot: anchor - timedelta(minutes=makespan - minute)
        for lot, minute in release.items()
    }
