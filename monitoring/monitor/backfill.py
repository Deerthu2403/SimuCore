"""Write synthetic history into the database (origin = synthetic, never mixed with measured data).

    python -m monitor.backfill --hours 24 [--step 10] [--until 2026-01-31T00:00:00Z]

Running it again for the same period and seed adds nothing.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta

from monitor.config import Settings, load_services
from monitor.models import utcnow
from monitor.sources.synthetic import SyntheticSource
from monitor.store import Store


async def backfill(settings: Settings, hours: float, step_s: int, until: datetime) -> int:
    if not settings.database_url:
        raise SystemExit("MONITOR_DATABASE_URL is not set")
    source = SyntheticSource(settings, load_services(settings.services_file))
    store = Store(settings.database_url, "synthetic", 10**9, settings.retention_days)
    await store.connect(close_events=False)
    end = until.replace(microsecond=0)
    t = end - timedelta(hours=hours)
    while t < end:
        store.add(source.at(t))
        t += timedelta(seconds=step_s)
        if len(store.buffer) >= 5000:
            await store._flush()
    await store._flush()
    written = store.written
    await store.aclose()
    return written


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--step", type=int, default=10)
    ap.add_argument("--until", type=datetime.fromisoformat)
    a = ap.parse_args()
    if a.until and a.until.tzinfo is None:
        ap.error("--until needs a time zone, e.g. 2026-01-31T00:00:00+00:00")
    if not 0 < a.hours <= 24 * 60 or not 1 <= a.step <= 3600:
        ap.error("--hours must be at most 1440 and --step between 1 and 3600")
    n = asyncio.run(backfill(Settings(), a.hours, a.step, a.until or utcnow()))
    print(f"{n} synthetic rows offered to the database")


if __name__ == "__main__":
    main()
