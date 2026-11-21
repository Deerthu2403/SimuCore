def test_health(client):
    r = client.get("/api/v1/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_ready(client):
    body = client.get("/api/v1/ready").json()
    assert body["ready"] is True and body["adapter"] == "simulated"


def test_topology_and_state(client):
    topo = client.get("/api/v1/topology").json()
    assert len(topo["nodes"]) == 27
    snap = client.get("/api/v1/twin/state").json()
    assert len(snap["states"]) == 27
    assert sum(snap["summary"].values()) == 27


def test_node_detail(client):
    d = client.get("/api/v1/twin/nodes/payments-service").json()
    assert "payments-db" in d["dependencies"]
    assert client.get("/api/v1/twin/nodes/missing").status_code == 404


def test_propagation_endpoint(client):
    r = client.post("/api/v1/propagation/analyze", json={"failed_nodes": ["redis-cache"]})
    assert r.status_code == 200
    body = r.json()
    assert body["result"]["impacted"][0]["node_id"] == "redis-cache"
    assert body["explanation"]["summary"]
    assert client.post("/api/v1/propagation/analyze",
                       json={"failed_nodes": ["x"]}).status_code == 404


def test_websocket_snapshot_then_delta(client):
    with client.websocket_connect("/ws/twin") as ws:
        snap = ws.receive_json()
        assert snap["type"] == "snapshot" and len(snap["states"]) == 27
        delta = ws.receive_json()
        assert delta["type"] == "delta" and delta["version"] >= snap["version"]


def test_prometheus_metrics_exposed(client):
    text = client.get("/metrics/").text
    assert "simucore_node_metric" in text
