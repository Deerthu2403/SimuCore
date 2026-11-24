import asyncio
import math
import re
from datetime import UTC, datetime

import httpx
import pytest
import yaml

from monitor.catalog import METRICS
from monitor.config import ROOT, Settings, check_host, load_services
from monitor.models import Sample, SourceUnavailable
from monitor.quality import inspect
from monitor.sources.direct import DirectSource, Scrape, compute, increase, parse, quantile
from monitor.sources.prometheus import INFRA_QUERIES, SERVICE_QUERIES, PrometheusSource
from monitor.sources.synthetic import SyntheticSource

SERVICES = load_services(Settings().services_file)
SETTINGS = Settings()


# ------------------------------------------------------------------ configuration
def test_prometheus_scrapes_exactly_the_mapped_services():
    cfg = yaml.safe_load((ROOT / "infra/prometheus/prometheus.yml").read_text(encoding="utf-8"))
    bank = next(j for j in cfg["scrape_configs"] if j["job_name"] == "bank")
    found = {(t["labels"]["service"], int(t["targets"][0].rsplit(":", 1)[1]))
             for t in bank["static_configs"]}
    assert found == {(s.name, s.port) for s in SERVICES}


def test_every_queried_bank_series_is_really_exported():
    source = "".join(p.read_text(encoding="utf-8")
                     for p in (ROOT / "banking-sim").rglob("*.py") if ".venv" not in p.parts)
    exported = set(re.findall(r'"(bank_[a-z_]+)"', source))
    for q in SERVICE_QUERIES.values():
        for name in re.findall(r"bank_[a-z_]+", q):
            base = re.sub(r"_(total|bucket|sum|count)$", "", name)
            assert name in exported or base in exported, name


def test_every_metric_has_a_query():
    assert set(SERVICE_QUERIES) == {m.key for m in METRICS}


@pytest.mark.parametrize("url", ["https://api.realbank.com", "http://10.0.0.1:9090",
                                 "http://prometheus.evil.com", "http://prometheus@evil.com"])
def test_only_the_simulated_bank_can_be_monitored(url):
    with pytest.raises(ValueError):
        check_host(url, SERVICES)


# ------------------------------------------------------------------ direct: arithmetic
def test_counter_increase_handles_restarts():
    assert increase([10, 15, 30]) == 20
    assert increase([100, 120, 5, 8]) == 20 + 5 + 3
    assert increase([7]) == 0


@pytest.mark.parametrize(("buckets", "q", "expected"), [
    ({0.1: 0, 0.2: 100, math.inf: 100}, 0.5, 0.15),
    ({0.1: 50, 0.2: 100, math.inf: 100}, 0.95, 0.19),
    ({0.1: 0, 0.2: 0, math.inf: 10}, 0.95, 0.2),  # beyond the last finite bound
])
def test_quantile_matches_prometheus_interpolation(buckets, q, expected):
    assert quantile(q, buckets) == pytest.approx(expected)


def test_quantile_of_nothing_is_nan():
    assert math.isnan(quantile(0.95, {0.1: 0, math.inf: 0}))


def page(service="account", **change) -> str:
    v = {"cpu": 10.0, "mem": 50e6, "ok": 100.0, "err": 0.0, "sum": 2.0, "n": 100.0, "rx": 1000.0,
         "tx": 2000.0, "dbs": 1.0, "dbn": 100.0, "conn": 4.0, "queue": 0.0, "fly": 1.0,
         "avail": 1.0}
    v.update(change)
    s = f'service="{service}"'
    return "\n".join([
        f"bank_process_cpu_seconds_total{{{s}}} {v['cpu']}",
        f"bank_memory_bytes{{{s}}} {v['mem']}", f"bank_memory_limit_bytes{{{s}}} 100e6",
        f'bank_service_requests_total{{outcome="ok",{s}}} {v["ok"]}',
        f'bank_service_requests_total{{outcome="client_error",{s}}} 0',
        f'bank_service_requests_total{{outcome="server_error",{s}}} {v["err"]}',
        f'bank_service_request_seconds_bucket{{le="0.02",{s}}} {v["n"]}',
        f'bank_service_request_seconds_bucket{{le="+Inf",{s}}} {v["n"]}',
        f"bank_service_request_seconds_sum{{{s}}} {v['sum']}",
        f"bank_service_request_seconds_count{{{s}}} {v['n']}",
        f"bank_network_receive_bytes_total{{{s}}} {v['rx']}",
        f"bank_network_transmit_bytes_total{{{s}}} {v['tx']}",
        f"bank_db_response_seconds_sum{{{s}}} {v['dbs']}",
        f"bank_db_response_seconds_count{{{s}}} {v['dbn']}",
        f"bank_active_connections{{{s}}} {v['conn']}",
        f"bank_request_queue_length{{{s}}} {v['queue']}",
        f"bank_in_flight_requests{{{s}}} {v['fly']}", f"bank_available{{{s}}} {v['avail']}", ""])


def sample() -> Sample:
    return Sample("account", "bank-account", datetime.now(UTC), "up")


def test_rates_are_computed_from_two_scrapes():
    a = parse(page(), "account", 0.0)
    b = parse(page(cpu=15, ok=170, err=30, n=200, sum=3.0, rx=6000, tx=12000, dbs=1.5, dbn=200),
              "account", 10.0)
    s = sample()
    compute([a, b], s)
    m = s.metrics
    assert m["cpu_pct"] == 50 and m["rps"] == 10 and m["error_rate"] == 0.3
    assert m["net_rx_bps"] == 500 and m["net_tx_bps"] == 1000
    assert m["latency_mean_ms"] == 10 and m["db_response_ms"] == 5
    assert m["latency_ms"] == pytest.approx(19)
    assert m["mem_pct"] == 50 and m["active_connections"] == 4
    assert set(m) == {x.key for x in METRICS}


def test_idle_service_has_no_latency_but_nothing_is_missing():
    s = sample()
    compute([parse(page(), "account", 0), parse(page(), "account", 10)], s)
    for key in ("latency_ms", "latency_mean_ms", "error_rate", "db_response_ms"):
        assert s.metrics[key] is None
    assert s.metrics["rps"] == 0
    other = [x for x in inspect_one(s) if x.service == "account"]
    assert other == []


def inspect_one(s: Sample):
    from monitor.models import Collection
    return inspect(Collection(s.ts, "measured", [s]), SERVICES)


def test_a_service_cannot_pose_as_another():
    with pytest.raises(ValueError):
        parse(page("payment"), "account", 0)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -5.0, 1e20])
def test_impossible_values_are_rejected_not_stored(bad):
    s = sample()
    s.put("active_connections", bad)
    assert "active_connections" not in s.metrics and "active_connections" in s.invalid


# ------------------------------------------------------------------ direct: collection
class FakeBank:
    def __init__(self):
        self.answer = {}
        self.requests = []
        self.concurrent = self.peak = 0

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.concurrent += 1
        self.peak = max(self.peak, self.concurrent)
        try:
            await asyncio.sleep(0.02)
            name = next(s.name for s in SERVICES if s.port == request.url.port)
            a = self.answer.get(name)
            if isinstance(a, Exception):
                raise a
            return a if a is not None else httpx.Response(200, text=page(name))
        finally:
            self.concurrent -= 1


@pytest.fixture
def fake():
    return FakeBank()


async def collect(fake, n=2):
    source = DirectSource(SETTINGS, SERVICES, httpx.MockTransport(fake))
    for _ in range(n):
        c = await source.collect(True)
    await source.aclose()
    return c, {s.service: s for s in c.samples}


async def test_all_services_are_read_at_the_same_time_with_get_only(fake):
    c, by = await collect(fake)
    assert len(by) == 8 and fake.peak == 8
    assert {(r.method, r.url.path) for r in fake.requests} == {("GET", "/metrics")}
    assert all("x-internal-key" not in r.headers for r in fake.requests)
    assert c.infra[0].component == "monitor-host"


@pytest.mark.parametrize(("answer", "status"), [
    (httpx.ConnectError("refused"), "down"), (httpx.ConnectTimeout("closed port"), "down"),
    (httpx.ReadError("reset"), "down"), (httpx.ReadTimeout("hung"), "unresponsive"),
    (httpx.Response(500), "unresponsive"), (httpx.Response(200, text="<html>"), "unresponsive"),
    (httpx.Response(200, text=page("payment")), "unresponsive"),
    (httpx.Response(200, text="x" * 3_000_000), "unresponsive"),
    (httpx.Response(302, headers={"location": "http://evil.example/"}), "unresponsive"),
    (httpx.Response(200, text=page("account", avail=0.0)), "down"),
])
async def test_one_bad_service_never_affects_the_others(fake, answer, status):
    fake.answer["account"] = answer
    _, by = await collect(fake)
    assert by["account"].status == status
    assert all(s.status == "up" for n, s in by.items() if n != "account")


# ------------------------------------------------------------------ prometheus
class FakePrometheus:
    """Evaluates nothing; answers each known query from a table."""

    def __init__(self):
        self.up = {s.name: 1 for s in SERVICES}
        self.age = {s.name: 0.5 for s in SERVICES}
        self.errors = {}
        self.override = {}
        self.fail = None
        self.skew = 0.0
        self.queries = {q.format(w="10s"): k for k, q in SERVICE_QUERIES.items()}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        if self.fail:
            if isinstance(self.fail, Exception):
                raise self.fail
            return self.fail
        if request.url.path == "/api/v1/targets":
            return httpx.Response(200, json={"status": "success", "data": {"activeTargets": [
                {"labels": {"service": s}, "lastError": e} for s, e in self.errors.items()]}})
        q = request.url.params["query"]
        if q == "time()":
            import time
            return self.ok([time.time() - self.skew, "0"], "scalar")
        if q == 'up{job="bank"}':
            return self.vector(self.up)
        if q.startswith("time() - timestamp"):
            return self.vector(self.age)
        if q in self.queries:
            key = self.queries[q]
            values = {s: 1.0 for s in self.up}
            values.update(self.override.get(key, {}))
            return self.vector({s: v for s, v in values.items() if v is not ...})
        return self.ok([{"metric": {}, "value": [0, "1"]}])  # infrastructure

    def vector(self, values):
        return self.ok([{"metric": {"service": s}, "value": [0, str(v)]}
                        for s, v in values.items()])

    @staticmethod
    def ok(result, kind="vector"):
        if kind == "scalar":
            result = [0, str(result[0])]
        return httpx.Response(200, json={"status": "success", "data": {
            "resultType": kind, "result": result}})


async def prom(fake):
    source = PrometheusSource(SETTINGS, SERVICES, httpx.MockTransport(fake))
    c = await source.collect(True)
    return c, {s.service: s for s in c.samples}


async def test_prometheus_delivers_every_metric_of_every_service():
    c, by = await prom(FakePrometheus())
    assert all(set(s.metrics) == {m.key for m in METRICS} for s in by.values())
    assert {i.component for i in c.infra} == set(INFRA_QUERIES)
    assert inspect(c, SERVICES) == []


@pytest.mark.parametrize(("error", "status"), [
    ("dial tcp 1.2.3.4:8103: connect: connection refused", "down"), ("EOF", "down"),
    ("context deadline exceeded", "unresponsive"),
    ('expected a valid start token, got "<"', "unresponsive")])
async def test_prometheus_tells_down_from_hung(error, status):
    f = FakePrometheus()
    f.up["account"], f.errors["account"] = 0, error
    _, by = await prom(f)
    assert by["account"].status == status
    assert by["payment"].status == "up"


async def test_prometheus_missing_idle_and_invalid_are_three_different_things():
    f = FakePrometheus()
    f.override = {"cpu_pct": {"account": ...}, "latency_ms": {"account": "NaN"},
                  "mem_bytes": {"account": -5}, "active_connections": {"account": "NaN"}}
    c, by = await prom(f)
    s = by["account"]
    assert "cpu_pct" not in s.metrics and s.metrics["latency_ms"] is None
    kinds = {(i.kind, i.metric) for i in inspect(c, SERVICES)}
    assert kinds == {("metric_missing", "cpu_pct"), ("metric_invalid", "mem_bytes"),
                     ("metric_invalid", "active_connections")}


async def test_a_target_prometheus_stopped_scraping_is_not_trusted():
    f = FakePrometheus()
    f.age["account"] = 60
    del f.up["fraud"]
    _, by = await prom(f)
    assert by["account"].status == by["fraud"].status == "unresponsive"


@pytest.mark.parametrize("fail", [
    httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), httpx.Response(503),
    httpx.Response(200, text="<html>"), httpx.Response(200, json={"status": "error"}),
    httpx.Response(200, json=[])])
async def test_prometheus_failure_is_reported_as_such(fail):
    f = FakePrometheus()
    f.fail = fail
    with pytest.raises(SourceUnavailable):
        await prom(f)


async def test_no_targets_at_all_is_a_source_failure():
    f = FakePrometheus()
    f.up = {}
    with pytest.raises(SourceUnavailable):
        await prom(f)


async def test_clock_difference_is_noticed():
    f = FakePrometheus()
    f.skew = 42
    c, _ = await prom(f)
    assert 41 < c.clock_skew_s < 43
    assert "clock_skew" in {i.kind for i in inspect(c, SERVICES)}


# ------------------------------------------------------------------ synthetic
def test_synthetic_data_is_labelled_repeatable_and_plausible():
    a, b = SyntheticSource(SETTINGS, SERVICES), SyntheticSource(SETTINGS, SERVICES)
    seen = set()
    for minute in range(0, 24 * 60, 5):
        t = datetime(2026, 3, 1, minute // 60, minute % 60, tzinfo=UTC)
        c = a.at(t)
        assert c.origin == "synthetic" and c == b.at(t)
        assert inspect(c, SERVICES) == [] or all(i.kind == "service_down"
                                                 for i in inspect(c, SERVICES))
        seen |= {a.condition(s.name, t) for s in SERVICES}
    assert seen == {"normal", "overload", "errors", "outage"}


def test_synthetic_overload_moves_related_metrics_together():
    src = SyntheticSource(SETTINGS, SERVICES)
    groups = {"normal": [], "overload": []}
    for slot in range(2000):
        t = datetime.fromtimestamp(1_770_000_000 + slot * 300, UTC)
        cond = src.condition("account", t)
        if cond in groups:
            groups[cond].append(next(s for s in src.at(t).samples if s.service == "account"))
    for key in ("rps", "cpu_pct", "latency_ms", "queue_length", "db_response_ms"):
        def mean(g, key=key):
            return sum(s.metrics[key] for s in groups[g]) / len(groups[g])
        assert mean("overload") > mean("normal") * 1.3, key


def test_scrape_dataclass_defaults_are_not_shared():
    assert Scrape(0).values is not Scrape(0).values
