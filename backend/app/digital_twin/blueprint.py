"""The structure of the virtual infrastructure: which components exist and how they connect.

Built from the dependency map (a file) and from what monitoring reports about where services run.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from functools import cached_property
from pathlib import Path

from app.digital_twin.model import ComponentSpec, ComponentType, LinkKind, LinkSpec

C, K = ComponentType, LinkKind
BANK_STORE, PLATFORM_STORE = "store-bank", "store-platform"
BANK_HOST, DOCKER_HOST = "server-bank-host", "server-docker"
BANK_NET = "net-bank"
CLIENT_ENTRY, EXTERNAL_NET = "lb-clients", "gw-external"
#: services that customers call from outside
ENTRY_SERVICES = ("authentication", "customer", "account", "transaction")
PROMETHEUS, MONITOR = "mon-prometheus", "mon-service"
#: monitoring's name for an infrastructure component -> the virtual component it describes
INFRA_IDS = {"docker-host": DOCKER_HOST, "monitor-host": BANK_HOST, "synthetic-host": BANK_HOST,
             "platform-postgres": PLATFORM_STORE, "prometheus": PROMETHEUS}


@dataclass(frozen=True)
class Blueprint:
    components: dict[str, ComponentSpec]
    links: dict[str, LinkSpec]
    service_ids: dict[str, str]  # service name -> component id

    @cached_property
    def checksum(self) -> str:
        data = [[asdict(c) for c in self.components.values()],
                [asdict(k) for k in self.links.values()]]
        return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()

    @cached_property
    def outgoing(self) -> dict[str, list[LinkSpec]]:
        out: dict[str, list[LinkSpec]] = {c: [] for c in self.components}
        for k in self.links.values():
            out[k.source].append(k)
        return out

    @cached_property
    def incoming(self) -> dict[str, list[LinkSpec]]:
        out: dict[str, list[LinkSpec]] = {c: [] for c in self.components}
        for k in self.links.values():
            out[k.target].append(k)
        return out

    @cached_property
    def dependencies_first(self) -> list[str]:
        """Component ids ordered so that everything comes after what it depends on."""
        order: list[str] = []
        state: dict[str, int] = {}

        def visit(node: str) -> None:
            if state.get(node) == 1:
                raise ValueError(f"dependency cycle through {node}")
            if node in state:
                return
            state[node] = 1
            for k in self.outgoing[node]:
                visit(k.target)
            state[node] = 2
            order.append(node)

        for c in self.components:
            visit(c)
        return order


def link_id(source: str, target: str) -> str:
    return f"{source}->{target}"


def load_map(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def build(dependency_map: dict, deployments: dict[str, str]) -> Blueprint:
    services = dependency_map["services"]
    ids = {name: s["node_id"] for name, s in services.items()}
    components: list[ComponentSpec] = []
    links: list[LinkSpec] = []

    def link(source, target, kind, mode="structural", weight=1.0, purpose="", timeout=None):
        links.append(LinkSpec(link_id(source, target), source, target, kind, mode, weight, purpose,
                              timeout))

    # ------------------------------------------------------------ microservices
    for name, s in services.items():
        components.append(ComponentSpec(
            ids[name], C.MICROSERVICE, s["title"], f"Banking microservice '{name}'", "application",
            "bank", s["criticality"],
            {"service": name, "port": s["port"], "max_concurrency": s["max_concurrency"],
             "baseline_latency_ms": s["baseline_latency_ms"],
             "deployment": deployments.get(name)}))
    for d in dependency_map["dependencies"]:
        link(ids[d["source"]], ids[d["target"]], K.CALLS, d["mode"], d["weight"], d["purpose"],
             d["timeout_ms"])

    # --------------------------------------------------- where traffic enters and leaves
    components.append(ComponentSpec(
        CLIENT_ENTRY, C.LOAD_BALANCER, "Client entry", "Where customer requests enter the bank. "
        "There is no separate load balancer process: its traffic is each service's measured "
        "requests minus the measured internal calls.", "edge", "bank", 5))
    for name in ENTRY_SERVICES:
        if name in ids:
            link(CLIENT_ENTRY, ids[name], K.ROUTES_TO, "degradable", 0.8, "customer requests")
    components.append(ComponentSpec(
        EXTERNAL_NET, C.GATEWAY, "External payment network", "The card and clearing network. The "
        "Payment Gateway simulates it, so it is seen only through that service.", "edge", "bank",
        4))
    if "payment" in ids:
        link(ids["payment"], EXTERNAL_NET, K.ROUTES_TO, "required", 0.6, "external payments")

    # ---------------------------------------------------------------- databases
    components.append(ComponentSpec(
        BANK_STORE, C.DATABASE, "Bank database", "The storage behind the Database Service, "
        "observed through its connection pool", "data", "bank", 5, {"engine": "SQLite"}))
    link(ids["database"], BANK_STORE, K.STORES_IN, "required", 1.0, "all banking data")
    components.append(ComponentSpec(
        PLATFORM_STORE, C.DATABASE, "Platform database", "PostgreSQL with monitoring and twin "
        "history", "data", "platform", 3, {"engine": "PostgreSQL"}))

    # ------------------------------------------------------------------ servers
    components.append(ComponentSpec(
        DOCKER_HOST, C.SERVER, "Docker machine", "The Linux machine containers run on",
        "compute", "platform", 4))
    if any(v == "process" for v in deployments.values()):
        components.append(ComponentSpec(
            BANK_HOST, C.SERVER, "Bank host", "The computer the bank's processes run on", "compute",
            "bank", 5))
    for name, deployment in sorted(deployments.items()):
        if name in ids:
            link(ids[name], BANK_HOST if deployment == "process" else DOCKER_HOST, K.HOSTED_ON,
                 "required", 0.8, "runs on")
    link(PLATFORM_STORE, DOCKER_HOST, K.HOSTED_ON, "required", 0.8, "runs on")

    # ------------------------------------------------------------------ network
    components.append(ComponentSpec(
        BANK_NET, C.NETWORK, "Bank network", "Carries the connections between the services",
        "network", "bank", 5))
    for name in ids:
        link(ids[name], BANK_NET, K.CONNECTED_TO)

    # --------------------------------------------------------------- monitoring
    components.append(ComponentSpec(
        PROMETHEUS, C.MONITORING, "Prometheus", "Collects the metrics", "platform", "platform", 2))
    components.append(ComponentSpec(
        MONITOR, C.MONITORING, "Monitoring service", "Checks the metrics and feeds this twin",
        "platform", "platform", 3))
    link(MONITOR, PROMETHEUS, K.MONITORS, "degradable", 0.5, "source of metrics")
    link(MONITOR, PLATFORM_STORE, K.STORES_IN, "degradable", 0.5, "metric history")
    link(PROMETHEUS, DOCKER_HOST, K.HOSTED_ON, "required", 0.8, "runs on")

    by_id = {c.id: c for c in components}
    by_link = {k.id: k for k in links}
    if len(by_id) != len(components) or len(by_link) != len(links):
        raise ValueError("component and link ids must be unique")
    blueprint = Blueprint(by_id, by_link, ids)
    _ = blueprint.dependencies_first  # refuses cycles
    return blueprint
