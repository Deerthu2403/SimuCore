"""History in PostgreSQL. During an outage rows wait in a bounded buffer; scoring never waits."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg

from simucore_ai.engine import AnomalyEvent, Step, Warning_

log = logging.getLogger("ai.store")
UNAVAILABLE = (OSError, TimeoutError, asyncpg.PostgresError, asyncpg.InterfaceError)
BAD_ROW = (asyncpg.DataError, asyncpg.IntegrityConstraintViolationError)
BATCH = 300
BUFFER = 60_000
MAINTAIN_EVERY_S = 3600

ANOMALY = (
    "INSERT INTO anomaly_event (id, ts, node_id, score, severity, contributing_metrics, model_id, "
    "last_seen, closed_at, level, condition, origin) "
    "VALUES ($1,$2,$3,$4,$5::text::severity,$6,$7,$8,$9,$10,$11,$12) "
    "ON CONFLICT (id) DO UPDATE SET score = EXCLUDED.score, severity = EXCLUDED.severity, "
    "contributing_metrics = EXCLUDED.contributing_metrics, last_seen = EXCLUDED.last_seen, "
    "closed_at = EXCLUDED.closed_at, level = EXCLUDED.level, condition = EXCLUDED.condition")
WARNING = (
    "INSERT INTO prediction_warning (id, node_id, target, opened_at, closed_at, horizon_s, "
    "probability, peak, reasons, outcome, lead_s, model_id, origin) "
    "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13) "
    "ON CONFLICT (id) DO UPDATE SET closed_at = EXCLUDED.closed_at, peak = EXCLUDED.peak, "
    "reasons = EXCLUDED.reasons, outcome = EXCLUDED.outcome, lead_s = EXCLUDED.lead_s")
PREDICTION = (
    "INSERT INTO prediction (id, predicted_at, node_id, target, horizon_s, probability, "
    "top_features, model_id, origin) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) ON CONFLICT DO NOTHING")
OUTCOME = (
    "INSERT INTO prediction_outcome (prediction_id, actual_event, error) VALUES ($1,$2,$3) "
    "ON CONFLICT DO NOTHING")


class StoreUnavailable(RuntimeError):
    pass


class Store:
    def __init__(self, url: str, meta: dict[str, Any], retention_days: int) -> None:
        self.url, self.meta, self.retention_days = url, meta, retention_days
        self.pool: asyncpg.Pool | None = None
        self.model_id: UUID | None = None
        self.buffer: deque[tuple[str, tuple]] = deque()
        self.written = self.dropped = self.rejected = 0
        self.error: str | None = None
        self._wake = asyncio.Event()
        self._first = True
        self._maintained = 0.0

    # ------------------------------------------------------------------ queueing
    def add(self, step: Step, origin: str, horizon_s: int) -> None:
        for e in step.anomalies:
            self._push(ANOMALY, self._anomaly(e, origin))
        for w in step.warnings:
            self._push(WARNING, self._warning(w, origin))
        for p in step.predictions:
            self._push(PREDICTION, (UUID(p.id), p.ts, p.node_id, p.target, horizon_s,
                                    p.probability, json.dumps(p.reasons), None, origin))
        for p in step.outcomes:
            self._push(OUTCOME, (UUID(p.id), p.outcome,
                                 round((p.probability - float(bool(p.outcome))) ** 2, 6)))

    @staticmethod
    def _anomaly(e: AnomalyEvent, origin: str) -> tuple:
        return (UUID(e.id), e.opened_at, e.node_id, e.score, e.severity,
                json.dumps(e.contributions), None, e.last_seen, e.closed_at, e.level,
                e.condition, origin)

    @staticmethod
    def _warning(w: Warning_, origin: str) -> tuple:
        return (UUID(w.id), w.node_id, w.target, w.opened_at, w.closed_at, w.horizon_s,
                w.probability, w.peak, json.dumps(w.reasons), w.outcome, w.lead_s, None, origin)

    def _push(self, sql: str, row: tuple) -> None:
        if len(self.buffer) >= BUFFER:
            self.buffer.popleft()
            self.dropped += 1
        self.buffer.append((sql, row))
        self._wake.set()

    def _filled(self, sql: str, row: tuple) -> tuple:
        """The model's id is only known once the database has answered."""
        at = {ANOMALY: 6, WARNING: 11, PREDICTION: 7}.get(sql)
        return row if at is None else (*row[:at], self.model_id, *row[at + 1:])

    # ------------------------------------------------------------------- writing
    async def connect(self) -> None:
        self.pool = await asyncpg.create_pool(self.url, min_size=1, max_size=4,
                                              command_timeout=10, timeout=5)
        async with self.pool.acquire() as c:
            if self._first:
                await c.execute("SELECT close_stale_ai_rows()")
                self._first = False
            self.model_id = await self._register(c)

    async def _register(self, c: asyncpg.Connection) -> UUID:
        version = self.meta["version"]
        found = await c.fetchval("SELECT id FROM ml_model WHERE fingerprint = $1", version)
        if found:
            return found
        n = await c.fetchval("SELECT coalesce(max(version), 0) + 1 FROM ml_model "
                             "WHERE name = 'simucore-ai'")
        return await c.fetchval(
            "INSERT INTO ml_model (name, version, algorithm, params, metrics, is_active, "
            "trained_at, fingerprint) VALUES ('simucore-ai', $1, 'IsolationForest + XGBoost', $2, "
            "$3, true, $4, $5) RETURNING id",
            n, json.dumps({"horizon_s": self.meta["horizon_s"],
                           "thresholds": self.meta["thresholds"],
                           "trained_on": self.meta["trained_on"]}),
            json.dumps(self.meta["test"]), datetime.fromisoformat(self.meta["trained_at"]),
            version)

    async def writer(self) -> None:
        while True:
            try:
                if self.pool is None:
                    await self.connect()
                    log.info("connected to the database")
                self.error = None
                await self._wake.wait()
                await self._flush()
                if time.monotonic() - self._maintained > MAINTAIN_EVERY_S:
                    self._maintained = time.monotonic()
                    async with self.pool.acquire() as c:
                        await c.execute("SELECT prune_ai_history($1)", self.retention_days)
            except UNAVAILABLE as exc:
                if self.error is None:
                    log.warning("database unavailable: %s", exc)
                self.error = f"{type(exc).__name__}: {exc}"[:200]
                await self._drop()
                await asyncio.sleep(1.0)

    async def _flush(self) -> None:
        self._wake.clear()
        while self.buffer:
            batch = [self.buffer[i] for i in range(min(BATCH, len(self.buffer)))]
            assert self.pool
            async with self.pool.acquire() as c:
                try:
                    async with c.transaction():
                        for sql, row in batch:
                            await c.execute(sql, *self._filled(sql, row))
                except BAD_ROW:
                    await self._one_by_one(c, batch)
            for item in batch:
                if self.buffer and self.buffer[0] is item:
                    self.buffer.popleft()
            self.written += len(batch)

    async def _one_by_one(self, c: asyncpg.Connection, batch: list) -> None:
        for sql, row in batch:
            try:
                await c.execute(sql, *self._filled(sql, row))
            except BAD_ROW as exc:  # e.g. the outcome of a prediction that was never stored
                self.rejected += 1
                log.warning("row refused: %s", exc)

    async def _drop(self) -> None:
        if self.pool:
            self.pool.terminate()
            self.pool = None

    async def aclose(self) -> None:
        if self.pool:
            try:
                async with asyncio.timeout(3):
                    await self._flush()
                    await self.pool.close()
            except UNAVAILABLE:
                self.pool.terminate()

    def status(self) -> dict[str, Any]:
        return {"connected": self.pool is not None and self.error is None, "error": self.error,
                "buffered_rows": len(self.buffer), "written_rows": self.written,
                "dropped_rows": self.dropped, "rejected_rows": self.rejected}

    # ------------------------------------------------------------------- reading
    async def _fetch(self, sql: str, *args: Any) -> list[asyncpg.Record]:
        if self.pool is None:
            raise StoreUnavailable(self.error or "not connected yet")
        try:
            async with self.pool.acquire() as c:
                return await c.fetch(sql, *args)
        except UNAVAILABLE as exc:
            raise StoreUnavailable(str(exc)) from exc

    async def anomalies(self, start: datetime, end: datetime, node: str | None, origin: str,
                        limit: int) -> list[dict]:
        rows = await self._fetch(
            "SELECT * FROM anomaly_event WHERE ts >= $1 AND ts < $2 AND ($3::text IS NULL OR "
            "node_id = $3) AND origin = $4 ORDER BY ts DESC LIMIT $5", start, end, node, origin,
            limit)
        return [{"id": str(r["id"]), "node_id": r["node_id"], "opened_at": r["ts"],
                 "last_seen": r["last_seen"], "closed_at": r["closed_at"],
                 "score": round(r["score"], 4), "level": r["level"], "severity": r["severity"],
                 "condition": r["condition"],
                 "contributions": json.loads(r["contributing_metrics"])} for r in rows]

    async def warnings(self, start: datetime, end: datetime, node: str | None,
                       target: str | None, origin: str, limit: int) -> list[dict]:
        rows = await self._fetch(
            "SELECT * FROM prediction_warning WHERE opened_at >= $1 AND opened_at < $2 AND "
            "($3::text IS NULL OR node_id = $3) AND ($4::text IS NULL OR target = $4) AND "
            "origin = $5 ORDER BY opened_at DESC LIMIT $6", start, end, node, target, origin,
            limit)
        return [{"id": str(r["id"]), "node_id": r["node_id"], "target": r["target"],
                 "opened_at": r["opened_at"], "closed_at": r["closed_at"],
                 "horizon_s": r["horizon_s"], "probability": round(r["probability"], 4),
                 "peak": round(r["peak"], 4), "reasons": json.loads(r["reasons"]),
                 "outcome": r["outcome"], "lead_s": r["lead_s"]} for r in rows]

    async def predictions(self, start: datetime, end: datetime, node: str, target: str | None,
                          origin: str, limit: int) -> list[dict]:
        rows = await self._fetch(
            "SELECT p.*, o.actual_event FROM prediction p LEFT JOIN prediction_outcome o "
            "ON o.prediction_id = p.id WHERE p.predicted_at >= $1 AND p.predicted_at < $2 AND "
            "p.node_id = $3 AND ($4::text IS NULL OR p.target = $4) AND p.origin = $5 "
            "ORDER BY p.predicted_at DESC LIMIT $6", start, end, node, target, origin, limit)
        return [{"ts": r["predicted_at"], "target": r["target"], "horizon_s": r["horizon_s"],
                 "probability": round(r["probability"], 4),
                 "reasons": json.loads(r["top_features"]), "happened": r["actual_event"]}
                for r in rows]

    async def performance(self, start: datetime, end: datetime, origin: str) -> dict:
        """What became of the predictions and warnings in this period: evidence, not a promise."""
        rows = await self._fetch(
            "SELECT p.target, count(*) AS n, count(*) FILTER (WHERE o.actual_event) AS events, "
            "avg(o.error)::float8 AS brier FROM prediction p JOIN prediction_outcome o "
            "ON o.prediction_id = p.id WHERE p.predicted_at >= $1 AND p.predicted_at < $2 AND "
            "p.origin = $3 GROUP BY p.target", start, end, origin)
        warned = await self._fetch(
            "SELECT target, outcome, count(*) AS n, percentile_cont(0.5) WITHIN GROUP "
            "(ORDER BY lead_s)::float8 AS lead FROM prediction_warning WHERE opened_at >= $1 AND "
            "opened_at < $2 AND origin = $3 GROUP BY target, outcome", start, end, origin)
        out: dict[str, Any] = {t: {"predictions": 0, "events": 0, "brier": None, "warnings": {},
                                   "lead_s_median": None} for t in ("degradation", "failure")}
        for r in rows:
            out[r["target"]] |= {"predictions": r["n"], "events": r["events"],
                                 "brier": round(r["brier"], 4)}
        for r in warned:
            out[r["target"]]["warnings"][r["outcome"]] = r["n"]
            if r["outcome"] == "came_true":
                out[r["target"]]["lead_s_median"] = r["lead"]
        return out
