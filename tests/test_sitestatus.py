import asyncio
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from sitestatus import checks
from sitestatus.app import _downsample, create_app, host_status
from sitestatus.config import ConfigError, load_config
from sitestatus.db import Database

CONFIG_YAML = """
listen: {host: 127.0.0.1, port: 0}
database: test.db
hosts:
  - name: router
    address: 127.0.0.1
    checks:
      ping: {interval: 5}
  - name: web
    address: 127.0.0.1
    checks:
      tcp: {port: 80}
      throughput: {method: http, url: "http://127.0.0.1/f.bin"}
"""


@pytest.fixture
def config(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(CONFIG_YAML)
    return load_config(p)


def test_load_config(config):
    assert [h.name for h in config.hosts] == ["router", "web"]
    assert config.hosts[0].ping.interval == 5
    assert config.hosts[1].tcp.port == 80
    assert config.hosts[1].throughput.method == "http"
    assert config.database.name == "test.db"


@pytest.mark.parametrize("yaml_text,msg", [
    ("hosts: []", "no hosts"),
    ("hosts: [{name: a, address: x, checks: {}}]", "no checks"),
    ("hosts: [{name: a, address: x, checks: {tcp: {}}}]", "requires a port"),
    ("hosts: [{name: a, address: x, checks: {throughput: {method: http}}}]", "requires a url"),
    ("hosts: [{name: a, address: x, checks: {bogus: {}}}]", "unknown check"),
])
def test_config_errors(tmp_path, yaml_text, msg):
    p = tmp_path / "c.yaml"
    p.write_text(yaml_text)
    with pytest.raises(ConfigError, match=msg):
        load_config(p)


def test_db_roundtrip(tmp_path):
    db = Database(tmp_path / "t.db")
    now = time.time()
    db.add_ping("h", "icmp", True, rtt_avg=1.5, rtt_min=1.0, rtt_max=2.0,
                loss_pct=0.0, ts=now - 10)
    db.add_ping("h", "icmp", False, loss_pct=100.0, error="timeout", ts=now)
    db.add_throughput("h", "http", True, mbps=940.0, nbytes=10, seconds=1.0, ts=now)

    hist = db.ping_history("h", now - 60)
    assert len(hist) == 2 and hist[0]["ok"] == 1 and hist[1]["ok"] == 0
    stats = db.uptime_stats("h", now - 60)
    assert stats["samples"] == 2 and stats["uptime_pct"] == 50.0
    assert db.last_ping("h")["ok"] == 0
    assert db.last_throughput("h")["mbps"] == 940.0

    db.prune(now + 1)
    assert db.ping_history("h", 0) == []
    db.close()


def test_downsample():
    rows = [{"ts": i, "ok": 1, "rtt_avg": float(i), "rtt_min": float(i),
             "rtt_max": float(i), "loss_pct": 0.0, "error": None}
            for i in range(1000)]
    out = _downsample(rows, 100)
    assert len(out) <= 101
    assert out[0]["ts"] < out[-1]["ts"]
    assert all(r["ok"] == 1 for r in out)


def test_tcp_ping():
    srv = socket.create_server(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    threading.Thread(target=lambda: [srv.accept() for _ in range(3)],
                     daemon=True).start()
    r = asyncio.run(checks.tcp_ping("127.0.0.1", port, timeout=2))
    srv.close()
    assert r["ok"] and r["loss_pct"] == 0.0 and r["rtt_avg"] > 0

    r = asyncio.run(checks.tcp_ping("127.0.0.1", 1, timeout=1))
    assert not r["ok"] and r["loss_pct"] == 100.0


def test_http_throughput():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"x" * (1 << 20)
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}/f.bin"
    r = asyncio.run(checks.http_throughput(url, max_seconds=5))
    srv.shutdown()
    assert r["ok"] and r["bytes"] == 1 << 20 and r["mbps"] > 0


@pytest.mark.skipif(__import__("sys").platform == "win32", reason="non-windows fallback")
def test_win_ping_graceful_off_windows():
    # On non-Windows the module must import and fail cleanly, never raise
    from sitestatus import win_ping
    r = win_ping.ping_round("127.0.0.1", 1, 1)
    assert r["ok"] is False and "error" in r


def test_host_status(config):
    host = config.hosts[0]
    now = time.time()
    assert host_status(config, host, None) == "unknown"
    assert host_status(config, host, {"ts": now, "ok": 1, "loss_pct": 0, "rtt_avg": 5}) == "up"
    assert host_status(config, host, {"ts": now, "ok": 1, "loss_pct": 40, "rtt_avg": 5}) == "degraded"
    assert host_status(config, host, {"ts": now, "ok": 1, "loss_pct": 0, "rtt_avg": 900}) == "degraded"
    assert host_status(config, host, {"ts": now, "ok": 0, "loss_pct": 100, "rtt_avg": None}) == "down"
    assert host_status(config, host, {"ts": now - 3600, "ok": 1, "loss_pct": 0, "rtt_avg": 5}) == "unknown"


def test_api(config):
    app = create_app(config)
    db: Database = app.state.db
    now = time.time()
    db.add_ping("router", "icmp", True, rtt_avg=2.0, loss_pct=0.0, ts=now - 5)
    db.add_throughput("web", "http", True, mbps=500.0, ts=now - 5)

    # TestClient triggers lifespan (monitor starts and stops around the block)
    with TestClient(app) as client:
        r = client.get("/api/status")
        assert r.status_code == 200
        hosts = {h["name"]: h for h in r.json()["hosts"]}
        assert hosts["router"]["status"] == "up"
        assert hosts["router"]["uptime_pct"] == 100.0
        assert hosts["web"]["last_throughput"]["mbps"] == 500.0

        r = client.get("/api/history/router?hours=1")
        assert r.status_code == 200
        assert len(r.json()["ping"]) >= 1

        assert client.get("/api/history/nope").status_code == 404
        assert client.get("/").status_code == 200
