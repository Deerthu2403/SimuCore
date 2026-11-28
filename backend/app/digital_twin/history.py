"""What the twin believed in the past. Statuses are stored here; metrics are read from the
monitoring tables, so that nothing is stored twice."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections import deque
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg

from app.digital_twin.blueprint import INFRA_IDS, Blueprint
from app.digital_twin.model import Change, LinkKind
from app.digital_twin.state import DigitalTwin

log = logging.getLogger(__name__)
SNAPSHOT_EVERY = timedelta(seconds=30)
#: Without evidence for longer than this, the twin was not running.
EVIDENCE_GAP = timedelta(seconds=75)
METRIC_WINDOW = timedelta(seconds=10)
LIMIT = 50_000
UNAVAILABLE = (OSError, TimeoutError, asyncpg.PostgresError, asyncpg.InterfaceError)
BAD_ROW = (asyncpg.DataError, asyncpg.IntegrityConstraintViolationError)
STRUCTURE = ("INSERT INTO twin_structure (checksum, components, links) VALUES ($1, $2, $3) "
             "ON CONFLICT DO NOTHING")
TRANSITION = ("INSERT INTO twin_transition (ts, run_id, version, object_kind, object_id, "
              "from_status, to_status, reason, root_causes, origin) "
              "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) ON CONFLICT DO NOTHING")
EVENT = ("INSERT INTO propagation_event (id, opened_at, run_id, origins, new_origins, graph, "
         "level, affected, expected, business_impact, predicted, report, origin) "
         "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13) ON CONFLICT DO NOTHING")
EVENT_CLOSED = "UPDATE propagation_event SET closed_at = $2, outcome = $3 WHERE id = $1"
SNAPSHOT = ("INSERT INTO twin_snapshot (ts, run_id, version, structure, origin, statuses) "
            "VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT DO NOTHING")


class HistoryUnavailable(RuntimeError):
    pass


class TwinHistory:
    def __init__(self, url: str, retention_days: int = 30) -> None:
        self.url, self.retention_days = url, retention_days
        self.pool: asyncpg.Pool | None = None
        self.buffer: deque[tuple[str, tuple]] = deque()
        self.written = self.dropped = self.rejected = 0
        self.error: str | None = None
        self._structure: str | None = None
        self._last_snapshot: datetime | None = None
        self._last_prune: datetime | None = None
        self._wake = asyncio.Event()
        self._stored: set[str] = set()  # propagation events already queued

    # ------------------------------------------------------------------- recording
    def record(self, twin: DigitalTwin, change: Change) -> None:
        for t in change.transitions:
            self._push(TRANSITION, (
                t.ts, twin.run_id, t.version, t.object_kind, t.object_id,
                t.from_status.value if t.from_status else None, t.to_status.value, t.reason[:500],
                list(t.root_causes), t.origin))
        for e in change.events:
            if e.id not in self._stored:
                r = e.report
                self._push(EVENT, (uuid.UUID(e.id), e.opened_at, twin.run_id, list(e.origins),
                                   list(e.new), r["graph"], r["level"], r["affected"],
                                   r["expected_affected"],
                                   r["business_impact"], list(e.predicted), _dump(r), twin.origin))
                self._stored.add(e.id)
            if e.closed_at is not None:
                self._push(EVENT_CLOSED, (uuid.UUID(e.id), e.closed_at, _dump(e.outcome)))
        if change.structure_changed:
            self.snapshot(twin)
        else:
            self.keep_alive(twin)

    def keep_alive(self, twin: DigitalTwin) -> None:
        """Regular snapshots, also while nothing arrives: running blind is not the same as off."""
        if self._last_snapshot is None \
                or datetime.now(UTC) - self._last_snapshot >= SNAPSHOT_EVERY:
            self.snapshot(twin)

    def snapshot(self, twin: DigitalTwin) -> None:
        """Every snapshot is also evidence that the twin was running at that moment."""
        if twin.version == 0:
            return
        b = twin.blueprint
        if self._structure != b.checksum:
            self._structure = b.checksum
            self._push(STRUCTURE, (b.checksum, _dump([asdict(c) for c in b.components.values()]),
                                   _dump([asdict(k) for k in b.links.values()])))
        now = datetime.now(UTC)
        self._last_snapshot = now
        self._push(SNAPSHOT, (now, twin.run_id, twin.version, b.checksum, twin.origin,
                              _dump(twin.statuses())))

    def _push(self, sql: str, row: tuple) -> None:
        if len(self.buffer) >= LIMIT:
            self.buffer.popleft()
            self.dropped += 1
        self.buffer.append((sql, row))
        self._wake.set()

    # --------------------------------------------------------------------- writing
    async def writer(self) -> None:
        while True:
            try:
                if self.pool is None:
                    self.pool = await asyncpg.create_pool(self.url, min_size=1, max_size=4,
                                                          command_timeout=10, timeout=5)
                    self.error = None
                    log.info("twin history connected")
                await self._wake.wait()
                await self._flush()
                await self._prune()
                self.error = None
            except UNAVAILABLE as exc:
                if self.error is None:
                    log.warning("twin history unavailable: %s", exc)
                self.error = f"{type(exc).__name__}: {exc}"[:200]
                pool, self.pool = self.pool, None
                if pool:
                    pool.terminate()
                await asyncio.sleep(1.0)

    async def _flush(self) -> None:
        while self.buffer:
            batch = [self.buffer[i] for i in range(min(200, len(self.buffer)))]
            async with self._conn() as conn:
                try:
                    async with conn.transaction():
                        for sql, row in batch:
                            await conn.execute(sql, *row)
                except BAD_ROW:
                    for sql, row in batch:
                        try:
                            await conn.execute(sql, *row)
                        except BAD_ROW as exc:
                            self.rejected += 1
                            log.error("history row rejected: %s", exc)
            for item in batch:
                if self.buffer and self.buffer[0] is item:
                    self.buffer.popleft()
            self.written += len(batch)
        self._wake.clear()

    async def _prune(self) -> None:
        now = datetime.now(UTC)
        if self._last_prune and now - self._last_prune < timedelta(hours=6):
            return
        self._last_prune = now
        async with self._conn() as conn:
            await conn.fetchval("SELECT prune_twin_history($1)", self.retention_days)

    def _conn(self):
        if self.pool is None:
            raise HistoryUnavailable("no database connection")
        return self.pool.acquire(timeout=5)

    async def aclose(self) -> None:
        try:
            async with asyncio.timeout(5):
                if self.pool and self.buffer:
                    await self._flush()
        except (*UNAVAILABLE, HistoryUnavailable):
            log.warning("%s history rows could not be written at shutdown", len(self.buffer))
        if self.pool:
            self.pool.terminate()
            self.pool = None

    def status(self) -> dict[str, Any]:
        return {"connected": self.pool is not None and self.error is None, "error": self.error,
                "buffered_rows": len(self.buffer), "written_rows": self.written,
                "dropped_rows": self.dropped, "rejected_rows": self.rejected}

    # --------------------------------------------------------------------- reading
    async def _fetch(self, sql: str, *args: Any) -> list[asyncpg.Record]:
        try:
            async with self._conn() as conn:
                return await conn.fetch(sql, *args)
        except UNAVAILABLE as exc:
            raise HistoryUnavailable(str(exc)) from exc

    async def transitions(self, start: datetime, end: datetime, object_id: str | None,
                          kind: str | None, limit: int) -> list[dict]:
        rows = await self._fetch(
            "SELECT * FROM twin_transition WHERE ts >= $1 AND ts < $2 AND ($3::text IS NULL OR "
            "object_id = $3) AND ($4::text IS NULL OR object_kind = $4) ORDER BY ts DESC, id DESC "
            "LIMIT $5", start, end, object_id, kind, limit)
        return [{k: r[k] for k in ("ts", "version", "object_kind", "object_id", "from_status",
                                   "to_status", "reason", "root_causes", "origin")}
                | {"run_id": str(r["run_id"])} for r in rows]

    async def propagation(self, start: datetime, end: datetime, component: str | None,
                          limit: int) -> list[dict]:
        rows = await self._fetch(
            "SELECT * FROM propagation_event WHERE opened_at >= $1 AND opened_at < $2 AND "
            "($3::text IS NULL OR $3 = ANY(origins) OR $3 = ANY(predicted)) "
            "ORDER BY opened_at DESC LIMIT $4", start, end, component, limit)
        return [{"id": str(r["id"]), "opened_at": r["opened_at"], "closed_at": r["closed_at"],
                 "origins": list(r["origins"]), "new_origins": list(r["new_origins"]),
                 "level": r["level"], "affected": r["affected"], "expected": r["expected"],
                 "business_impact": r["business_impact"], "predicted": list(r["predicted"]),
                 "outcome": json.loads(r["outcome"]) if r["outcome"] else None,
                 "report": json.loads(r["report"]), "run_id": str(r["run_id"])} for r in rows]

    async def state_at(self, ts: datetime) -> dict[str, Any] | None:
        """The twin as it was at ``ts``; None if it was not running then."""
        base = await self._fetch(
            "SELECT s.*, t.components, t.links FROM twin_snapshot s JOIN twin_structure t "
            "ON t.checksum = s.structure WHERE s.ts <= $1 ORDER BY s.ts DESC LIMIT 1", ts)
        if not base:
            return None
        snap = base[0]
        later = await self._fetch(
            "SELECT * FROM twin_transition WHERE run_id = $1 AND version > $2 AND ts <= $3 "
            "ORDER BY version, id", snap["run_id"], snap["version"], ts)
        evidence = max([snap["ts"], *(r["ts"] for r in later)])
        if ts - evidence > EVIDENCE_GAP:
            return None
        statuses = json.loads(snap["statuses"])
        reasons: dict[str, str] = {}
        for r in later:
            statuses[r["object_id"]], reasons[r["object_id"]] = r["to_status"], r["reason"]
        components, links = json.loads(snap["components"]), json.loads(snap["links"])
        measured = await self._metrics_at(ts, snap["origin"] or "measured")
        return {
            "ts": ts, "run_id": str(snap["run_id"]), "origin": snap["origin"],
            "version": max([snap["version"], *(r["version"] for r in later)]),
            "evidence_at": evidence,
            "components": [{"id": x["id"], "type": x["type"], "name": x["name"],
                            "status": statuses.get(x["id"], "unknown"),
                            "reason": reasons.get(x["id"]), "metrics": measured.get(x["id"], {})}
                           for x in components],
            "links": [{"id": k["id"], "source": k["source"], "target": k["target"],
                       "kind": k["kind"], "status": statuses.get(k["id"], "unknown"),
                       "metrics": measured.get(k["id"], {})} for k in links]}

    async def _metrics_at(self, ts: datetime, origin: str) -> dict[str, dict[str, float]]:
        since = ts - METRIC_WINDOW
        out: dict[str, dict[str, float]] = {}
        skip = {"ts", "node_id", "source_id", "extra", "status", "origin"}
        for r in await self._fetch(
                "SELECT DISTINCT ON (node_id) * FROM metric_sample WHERE ts > $1 AND ts <= $2 AND "
                "origin = $3 ORDER BY node_id, ts DESC", since, ts, origin):
            out[r["node_id"]] = {k: round(v, 4) for k, v in r.items()
                                 if k not in skip and v is not None}
        for r in await self._fetch(
                "SELECT DISTINCT ON (source, target) * FROM link_metric_sample WHERE ts > $1 AND "
                "ts <= $2 AND origin = $3 ORDER BY source, target, ts DESC", since, ts, origin):
            out[f"bank-{r['source']}->bank-{r['target']}"] = {
                k: round(r[k], 4) for k in ("calls_per_s", "failure_rate", "latency_ms", "circuit")
                if r[k] is not None}
        for r in await self._fetch(
                "SELECT DISTINCT ON (component) * FROM infra_metric_sample WHERE ts > $1 AND "
                "ts <= $2 AND origin = $3 ORDER BY component, ts DESC",
                ts - 3 * METRIC_WINDOW, ts, origin):
            if r["component"] in INFRA_IDS:
                out[INFRA_IDS[r["component"]]] = json.loads(r["metrics"])
        return out

    async def timeline(self, object_id: str, start: datetime, end: datetime) -> list[dict]:
        """Unbroken intervals of one status. Times when the twin was not running are ``untracked``.
        """
        marks = await self._fetch(
            "SELECT ts, run_id, to_status AS status, reason FROM twin_transition "
            "WHERE object_id = $1 AND ts >= $2 AND ts < $3 UNION ALL "
            "SELECT ts, run_id, statuses->>$1, NULL FROM twin_snapshot "
            "WHERE ts >= $2 AND ts < $3 AND statuses ? $1 ORDER BY ts", object_id,
            start - EVIDENCE_GAP, end)
        out: list[dict] = []

        def add(a: datetime, b: datetime, status: str, reason: str | None) -> None:
            a, b = max(a, start), min(b, end)
            if a >= b:
                return
            if out and out[-1]["status"] == status and out[-1]["end"] == a:
                out[-1]["end"] = b
            else:
                out.append({"start": a, "end": b, "status": status, "reason": reason})

        cursor = start
        for i, r in enumerate(marks):
            nxt = marks[i + 1]["ts"] if i + 1 < len(marks) else end
            until = min(nxt, r["ts"] + EVIDENCE_GAP)
            add(cursor, r["ts"], "untracked", None)
            add(r["ts"], until, r["status"], r["reason"])
            cursor = max(cursor, until)
        add(cursor, end, "untracked", None)
        for x in out:
            x["seconds"] = round((x["end"] - x["start"]).total_seconds(), 1)
        return out


def _dump(value: Any) -> str:
    return json.dumps(value, default=str)


def call_links(b: Blueprint) -> list[str]:
    return [k.id for k in b.links.values() if k.kind is LinkKind.CALLS]
