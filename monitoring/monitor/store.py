"""History in PostgreSQL. During a database outage, rows wait in a bounded buffer."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from datetime import datetime
from typing import Any

import asyncpg

from monitor.catalog import BY_KEY
from monitor.models import Collection, utcnow
from monitor.quality import KINDS, Event

log = logging.getLogger("monitor.store")
COLUMNS = list(BY_KEY)  # catalogue keys are the column names
INSERT_SERVICE = (
    f"INSERT INTO metric_sample (ts, node_id, source_id, status, origin, {', '.join(COLUMNS)}) "
    f"VALUES ({', '.join(f'${i}' for i in range(1, 6 + len(COLUMNS)))}) ON CONFLICT DO NOTHING")
INSERT_INFRA = ("INSERT INTO infra_metric_sample (ts, component, status, metrics, origin) "
                "VALUES ($1, $2, $3, $4, $5) ON CONFLICT DO NOTHING")
UPSERT_EVENT = (
    "INSERT INTO monitoring_event (id, opened_at, last_seen, closed_at, kind, severity, service, "
    "metric, message) VALUES ($1,$2,$3,$4,$5,$6::text::severity,$7,$8,$9) "
    "ON CONFLICT (id) DO UPDATE SET last_seen = EXCLUDED.last_seen, closed_at = EXCLUDED.closed_at")
BATCH = 500
MAX_POINTS = 5000
UNAVAILABLE = (OSError, TimeoutError, asyncpg.PostgresError, asyncpg.InterfaceError)


BAD_ROW = (asyncpg.DataError, asyncpg.IntegrityConstraintViolationError)


class StoreUnavailable(RuntimeError):
    pass


class Store:
    def __init__(self, url: str, source: str, buffer_rows: int, retention_days: int) -> None:
        self.url, self.source, self.retention_days = url, source, retention_days
        self.limit = buffer_rows
        self.pool: asyncpg.Pool | None = None
        self.source_id = None
        self.buffer: deque[tuple[str, tuple]] = deque()
        self.written = 0
        self.dropped = 0
        self.rejected = 0
        self.error: str | None = None
        self.default_rows = 0  # rows that arrived before their partition existed
        self._wake = asyncio.Event()
        self._connected_before = False
        self.created_at = utcnow()

    # ------------------------------------------------------------------ queueing
    def add(self, c: Collection) -> None:
        for s in c.samples:
            # down and unresponsive are history too: availability is computed from them
            values = [s.metrics.get(k) for k in COLUMNS]
            self._push(INSERT_SERVICE, (c.ts, s.node_id, None, s.status, c.origin, *values))
        for i in c.infra:
            self._push(INSERT_INFRA, (c.ts, i.component, i.status, json.dumps(i.metrics),
                                      c.origin))

    def add_event(self, e: Event) -> None:
        self._push(UPSERT_EVENT, (e.id, e.opened_at, e.last_seen, e.closed_at, e.issue.kind,
                                  KINDS[e.issue.kind][0], e.issue.service or None,
                                  e.issue.metric or None, e.issue.message))

    def _push(self, sql: str, row: tuple) -> None:
        if len(self.buffer) >= self.limit:
            self.buffer.popleft()  # oldest first; counted, and raised as an event
            self.dropped += 1
        self.buffer.append((sql, row))
        self._wake.set()

    # ------------------------------------------------------------------- writing
    async def connect(self, close_events: bool = True) -> None:
        self.pool = await asyncpg.create_pool(
            self.url, min_size=1, max_size=6, command_timeout=10, timeout=5)
        async with self.pool.acquire() as conn:
            self.source_id = await conn.fetchval(
                "SELECT id FROM telemetry_source WHERE name = $1", self.source)
            if self.source_id is None:
                self.source_id = await conn.fetchval(
                    "INSERT INTO telemetry_source (name, adapter) VALUES ($1, $1) RETURNING id",
                    self.source)
            # Whoever opened these is gone; this run re-opens what is still true.
            # (A reconnect is not a restart: this run's own open events stay open.)
            if close_events and not self._connected_before:
                await conn.execute(
                    "UPDATE monitoring_event SET closed_at = greatest(now(), opened_at), "
                    "message = message || ' (closed: monitor restarted)' WHERE closed_at IS NULL "
                    "AND opened_at < $1", self.created_at)
            self._connected_before = True
        await self.maintain()

    async def maintain(self) -> None:
        async with self._conn() as conn:
            created = await conn.fetchval("SELECT ensure_metric_partitions(2)")
            dropped = await conn.fetchval("SELECT drop_old_metric_partitions($1)",
                                          self.retention_days)
            self.default_rows = await conn.fetchval(
                "SELECT (SELECT count(*) FROM metric_sample WHERE tableoid = "
                "'metric_sample_default'::regclass)")
            if created or dropped:
                log.info("partitions: %s created, %s dropped", created, dropped)

    async def writer(self) -> None:
        """Runs forever: connects when needed, writes batches, retries after failures."""
        while True:
            try:
                if self.pool is None:
                    await self.connect()
                    self.error = None
                    log.info("connected to the database")
                await self._wake.wait()
                await self._flush()
                self.error = None
            except UNAVAILABLE as exc:
                if self.error is None:
                    log.warning("database unavailable: %s", exc)
                self.error = f"{type(exc).__name__}: {exc}"[:200]
                await self._drop_pool()
                await asyncio.sleep(1.0)

    async def _flush(self) -> None:
        while self.buffer:
            batch = [self.buffer[i] for i in range(min(BATCH, len(self.buffer)))]
            async with self._conn() as conn:
                try:
                    async with conn.transaction():
                        for sql in dict.fromkeys(s for s, _ in batch):
                            rows = [self._row(s, r) for s, r in batch if s == sql]
                            await conn.executemany(sql, rows)
                except BAD_ROW:
                    await self._one_by_one(conn, batch)
            # Only now remove them: a failure above leaves everything queued.
            for item in batch:
                if self.buffer and self.buffer[0] is item:
                    self.buffer.popleft()
            self.written += len(batch)
        self._wake.clear()

    async def _one_by_one(self, conn: asyncpg.Connection, batch: list[tuple[str, tuple]]) -> None:
        """Some row in the batch is unacceptable: keep the good ones, count the bad ones."""
        for sql, row in batch:
            try:
                await conn.execute(sql, *self._row(sql, row))
            except BAD_ROW as exc:
                self.rejected += 1
                log.error("row rejected by the database: %s", exc)

    def _row(self, sql: str, row: tuple) -> tuple:
        return (*row[:2], self.source_id, *row[3:]) if sql is INSERT_SERVICE else row

    async def _drop_pool(self) -> None:
        pool, self.pool = self.pool, None
        if pool:
            pool.terminate()

    def _conn(self):
        if self.pool is None:
            raise StoreUnavailable("no database connection")
        return self.pool.acquire(timeout=5)

    async def aclose(self) -> None:
        try:
            async with asyncio.timeout(5):
                if self.pool and self.buffer:
                    await self._flush()
        except (*UNAVAILABLE, StoreUnavailable):
            log.warning("%s rows could not be written at shutdown", len(self.buffer))
        await self._drop_pool()

    # ------------------------------------------------------------------- reading
    async def _fetch(self, sql: str, *args: Any) -> list[asyncpg.Record]:
        try:
            async with self._conn() as conn:
                return await conn.fetch(sql, *args)
        except UNAVAILABLE as exc:
            raise StoreUnavailable(str(exc)) from exc

    async def history(self, node_id: str, metrics: list[str], start: datetime, end: datetime,
                      step_s: int, origin: str) -> list[dict]:
        """Averages (and extremes) per time step. Column names come from the catalogue only."""
        unknown = [m for m in metrics if m not in BY_KEY]
        if unknown:
            raise ValueError(f"unknown metric(s): {unknown}")
        cols = ", ".join(f"avg({m})::float8 AS {m}, min({m})::float8 AS {m}_min, "
                         f"max({m})::float8 AS {m}_max" for m in metrics)
        rows = await self._fetch(
            f"SELECT date_bin(make_interval(secs => $4), ts, 'epoch'::timestamptz) AS t, "
            f"count(*) AS n, {cols} FROM metric_sample WHERE node_id = $1 AND ts >= $2 AND ts < $3 "
            f"AND origin = $5 GROUP BY t ORDER BY t", node_id, start, end, step_s, origin)
        return [{"ts": r["t"].isoformat(), "samples": r["n"],
                 **{m: _point(r, m) for m in metrics}} for r in rows]

    async def infra_history(self, component: str, start: datetime, end: datetime, step_s: int,
                            origin: str) -> list[dict]:
        rows = await self._fetch(
            "SELECT t, jsonb_object_agg(key, v) AS metrics FROM (SELECT "
            "date_bin(make_interval(secs => $4), ts, 'epoch'::timestamptz) AS t, m.key, "
            "round(avg(m.value::text::numeric), 4) AS v FROM infra_metric_sample, "
            "jsonb_each(metrics) AS m WHERE component = $1 AND ts >= $2 AND ts < $3 "
            "AND origin = $5 GROUP BY 1, m.key) q GROUP BY t ORDER BY t",
            component, start, end, step_s, origin)
        return [{"ts": r["t"].isoformat(), "metrics": json.loads(r["metrics"])} for r in rows]

    async def availability(self, start: datetime, end: datetime, origin: str) -> list[dict]:
        rows = await self._fetch(
            "SELECT node_id, count(*) AS n, count(*) FILTER (WHERE status = 'up') AS up, "
            "count(*) FILTER (WHERE status = 'down') AS down FROM metric_sample "
            "WHERE ts >= $1 AND ts < $2 AND origin = $3 GROUP BY node_id ORDER BY node_id",
            start, end, origin)
        return [{"node_id": r["node_id"], "samples": r["n"], "up": r["up"], "down": r["down"],
                 "unresponsive": r["n"] - r["up"] - r["down"],
                 "availability": round(r["up"] / r["n"], 5)} for r in rows]

    async def events(self, start: datetime, end: datetime, service: str | None,
                     limit: int) -> list[dict]:
        rows = await self._fetch(
            "SELECT * FROM monitoring_event WHERE opened_at < $2 AND coalesce(closed_at, now()) >= "
            "$1 AND ($3::text IS NULL OR service = $3) ORDER BY opened_at DESC LIMIT $4",
            start, end, service, limit)
        return [{**{k: r[k] for k in ("kind", "severity", "service", "metric", "message")},
                 "id": str(r["id"]), **{k: r[k].isoformat() if r[k] else None
                                        for k in ("opened_at", "last_seen", "closed_at")}}
                for r in rows]


def _point(r: asyncpg.Record, m: str) -> dict | None:
    if r[m] is None:
        return None
    return {"avg": round(r[m], 4), "min": round(r[f"{m}_min"], 4), "max": round(r[f"{m}_max"], 4)}
