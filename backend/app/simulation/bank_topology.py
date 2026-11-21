"""Reference banking infrastructure used while SimuCore runs on simulated data.

27 nodes across five layers: digital channels (gateways), core banking
microservices, data stores, compute hosts and network.
"""

from __future__ import annotations

from app.domain.models import Edge, EdgeKind, Node, NodeKind, NodeLayer, Topology

K, L, E = NodeKind, NodeLayer, EdgeKind


def _n(id_: str, name: str, kind: NodeKind, layer: NodeLayer, crit: int,
       replicas: int = 1, cpu: float = 4, mem: float = 8, max_rps: float = 1000,
       base_latency_ms: float = 20, base_rps: float = 300) -> Node:
    return Node(
        id=id_, name=name, kind=kind, layer=layer, criticality=crit,
        capacity={"replicas": replicas, "cpu_cores": cpu, "mem_gb": mem, "max_rps": max_rps},
        attributes={"base_latency_ms": base_latency_ms, "base_rps": base_rps},
    )


NODES: list[Node] = [
    # edge / channels
    _n("mobile-gateway", "Mobile Banking Gateway", K.GATEWAY, L.EDGE, 5, 3, max_rps=3000, base_latency_ms=8, base_rps=1200),
    _n("web-gateway", "Internet Banking Gateway", K.GATEWAY, L.EDGE, 5, 3, max_rps=2500, base_latency_ms=8, base_rps=900),
    _n("partner-gateway", "Open Banking / Partner API", K.GATEWAY, L.EDGE, 4, 2, max_rps=1500, base_latency_ms=10, base_rps=400),
    # application
    _n("auth-service", "Authentication & IAM", K.SERVICE, L.APPLICATION, 5, 3, max_rps=3000, base_latency_ms=15, base_rps=1500),
    _n("accounts-service", "Accounts Service", K.SERVICE, L.APPLICATION, 5, 3, max_rps=2000, base_latency_ms=25, base_rps=900),
    _n("payments-service", "Payments Service", K.SERVICE, L.APPLICATION, 5, 3, max_rps=1500, base_latency_ms=40, base_rps=700),
    _n("transactions-service", "Transactions Service", K.SERVICE, L.APPLICATION, 5, 2, max_rps=1500, base_latency_ms=30, base_rps=600),
    _n("fraud-service", "Fraud Detection", K.SERVICE, L.APPLICATION, 4, 2, max_rps=1200, base_latency_ms=55, base_rps=650),
    _n("loans-service", "Loans Service", K.SERVICE, L.APPLICATION, 3, 2, max_rps=600, base_latency_ms=45, base_rps=120),
    _n("cards-service", "Cards Service", K.SERVICE, L.APPLICATION, 4, 2, max_rps=1000, base_latency_ms=30, base_rps=350),
    _n("notification-service", "Notification Service", K.SERVICE, L.APPLICATION, 2, 2, max_rps=1500, base_latency_ms=20, base_rps=500),
    _n("ledger-service", "General Ledger Service", K.SERVICE, L.APPLICATION, 5, 2, max_rps=1500, base_latency_ms=35, base_rps=800),
    _n("core-banking-adapter", "Core Banking Adapter", K.SERVICE, L.APPLICATION, 5, 2, max_rps=1200, base_latency_ms=60, base_rps=500),
    # data
    _n("accounts-db", "Accounts DB (PostgreSQL)", K.DATABASE, L.DATA, 5, 2, 8, 32, 5000, 4, 1800),
    _n("payments-db", "Payments DB (PostgreSQL)", K.DATABASE, L.DATA, 5, 2, 8, 32, 4000, 5, 900),
    _n("ledger-db", "Ledger DB (PostgreSQL)", K.DATABASE, L.DATA, 5, 2, 8, 64, 4000, 6, 1000),
    _n("loans-db", "Loans DB (PostgreSQL)", K.DATABASE, L.DATA, 3, 1, 4, 16, 1500, 5, 150),
    _n("redis-cache", "Session & Rate Cache (Redis)", K.CACHE, L.DATA, 4, 3, 4, 16, 20000, 1, 4000),
    _n("kafka-bus", "Event Bus (Kafka)", K.QUEUE, L.DATA, 4, 3, 8, 32, 10000, 3, 1500),
    # compute
    _n("app-server-1", "App Host 1", K.SERVER, L.COMPUTE, 4, 1, 32, 128, 10000, 1, 2000),
    _n("app-server-2", "App Host 2", K.SERVER, L.COMPUTE, 4, 1, 32, 128, 10000, 1, 2000),
    _n("app-server-3", "App Host 3", K.SERVER, L.COMPUTE, 4, 1, 32, 128, 10000, 1, 2000),
    _n("db-server-1", "DB Host 1", K.SERVER, L.COMPUTE, 5, 1, 32, 256, 10000, 1, 2000),
    _n("db-server-2", "DB Host 2", K.SERVER, L.COMPUTE, 5, 1, 32, 256, 10000, 1, 2000),
    # network
    _n("load-balancer", "Load Balancer", K.LOAD_BALANCER, L.NETWORK, 5, 2, max_rps=20000, base_latency_ms=1, base_rps=2500),
    _n("firewall", "Perimeter Firewall", K.FIREWALL, L.NETWORK, 5, 2, max_rps=20000, base_latency_ms=1, base_rps=2500),
    _n("core-switch", "Core Switch", K.SWITCH, L.NETWORK, 5, 2, max_rps=50000, base_latency_ms=0.5, base_rps=8000),
]

_EDGES: list[tuple[str, str, EdgeKind, float]] = [
    # channels -> network path
    ("mobile-gateway", "load-balancer", E.ROUTES_THROUGH, 1.0),
    ("web-gateway", "load-balancer", E.ROUTES_THROUGH, 1.0),
    ("partner-gateway", "load-balancer", E.ROUTES_THROUGH, 1.0),
    ("load-balancer", "firewall", E.ROUTES_THROUGH, 1.0),
    ("firewall", "core-switch", E.ROUTES_THROUGH, 1.0),
    # channels -> services
    ("mobile-gateway", "auth-service", E.CALLS, 0.9),
    ("mobile-gateway", "accounts-service", E.CALLS, 0.7),
    ("mobile-gateway", "payments-service", E.CALLS, 0.7),
    ("mobile-gateway", "cards-service", E.CALLS, 0.5),
    ("web-gateway", "auth-service", E.CALLS, 0.9),
    ("web-gateway", "accounts-service", E.CALLS, 0.7),
    ("web-gateway", "payments-service", E.CALLS, 0.7),
    ("web-gateway", "loans-service", E.CALLS, 0.5),
    ("partner-gateway", "auth-service", E.CALLS, 0.9),
    ("partner-gateway", "payments-service", E.CALLS, 0.8),
    # service -> service
    ("payments-service", "fraud-service", E.CALLS, 0.8),
    ("payments-service", "accounts-service", E.CALLS, 0.7),
    ("payments-service", "ledger-service", E.CALLS, 0.9),
    ("payments-service", "kafka-bus", E.PUBLISHES, 0.6),
    ("transactions-service", "ledger-service", E.CALLS, 0.9),
    ("transactions-service", "kafka-bus", E.PUBLISHES, 0.5),
    ("cards-service", "accounts-service", E.CALLS, 0.7),
    ("cards-service", "fraud-service", E.CALLS, 0.6),
    ("cards-service", "transactions-service", E.CALLS, 0.6),
    ("loans-service", "accounts-service", E.CALLS, 0.6),
    ("loans-service", "core-banking-adapter", E.CALLS, 0.5),
    ("accounts-service", "core-banking-adapter", E.CALLS, 0.6),
    ("ledger-service", "core-banking-adapter", E.CALLS, 0.7),
    ("fraud-service", "transactions-service", E.CALLS, 0.4),
    ("notification-service", "kafka-bus", E.READS_WRITES, 0.9),
    # service -> data
    ("auth-service", "redis-cache", E.READS_WRITES, 0.7),
    ("auth-service", "accounts-db", E.READS_WRITES, 0.5),
    ("accounts-service", "accounts-db", E.READS_WRITES, 1.0),
    ("accounts-service", "redis-cache", E.READS_WRITES, 0.5),
    ("payments-service", "payments-db", E.READS_WRITES, 1.0),
    ("transactions-service", "redis-cache", E.READS_WRITES, 0.4),
    ("fraud-service", "redis-cache", E.READS_WRITES, 0.6),
    ("ledger-service", "ledger-db", E.READS_WRITES, 1.0),
    ("loans-service", "loans-db", E.READS_WRITES, 1.0),
    # hosting
    ("auth-service", "app-server-1", E.HOSTED_ON, 0.8),
    ("accounts-service", "app-server-1", E.HOSTED_ON, 0.8),
    ("payments-service", "app-server-2", E.HOSTED_ON, 0.8),
    ("transactions-service", "app-server-2", E.HOSTED_ON, 0.8),
    ("fraud-service", "app-server-2", E.HOSTED_ON, 0.8),
    ("loans-service", "app-server-3", E.HOSTED_ON, 0.8),
    ("cards-service", "app-server-3", E.HOSTED_ON, 0.8),
    ("notification-service", "app-server-3", E.HOSTED_ON, 0.8),
    ("ledger-service", "app-server-1", E.HOSTED_ON, 0.8),
    ("core-banking-adapter", "app-server-3", E.HOSTED_ON, 0.8),
    ("redis-cache", "app-server-3", E.HOSTED_ON, 0.9),
    ("accounts-db", "db-server-1", E.HOSTED_ON, 1.0),
    ("payments-db", "db-server-1", E.HOSTED_ON, 1.0),
    ("ledger-db", "db-server-2", E.HOSTED_ON, 1.0),
    ("loans-db", "db-server-2", E.HOSTED_ON, 1.0),
    ("kafka-bus", "db-server-2", E.HOSTED_ON, 0.9),
    # hosts -> network
    ("app-server-1", "core-switch", E.ROUTES_THROUGH, 0.9),
    ("app-server-2", "core-switch", E.ROUTES_THROUGH, 0.9),
    ("app-server-3", "core-switch", E.ROUTES_THROUGH, 0.9),
    ("db-server-1", "core-switch", E.ROUTES_THROUGH, 0.9),
    ("db-server-2", "core-switch", E.ROUTES_THROUGH, 0.9),
]

EDGES: list[Edge] = [Edge(source=s, target=t, kind=k, weight=w) for s, t, k, w in _EDGES]


def build_reference_topology() -> Topology:
    return Topology(version=1, nodes=[n.model_copy(deep=True) for n in NODES],
                    edges=[e.model_copy() for e in EDGES])
