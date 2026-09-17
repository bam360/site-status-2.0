"""FastAPI application: JSON API + static dashboard."""

from __future__ import annotations

import contextlib
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Config, ConfigError, build_host, save_hosts
from .db import Database
from .monitor import Monitor

STATIC_DIR = Path(__file__).parent / "static"

# A host is considered stale (unknown) when its newest sample is older than
# this many multiples of its check interval.
STALE_FACTOR = 3


def host_status(cfg: Config, host, last: dict | None) -> str:
    """up / degraded / down / unknown from the newest ping/tcp sample."""
    check = host.ping or host.tcp
    if last is None:
        return "unknown"
    interval = check.interval if check else 60
    if time.time() - last["ts"] > max(60, interval * STALE_FACTOR):
        return "unknown"
    if not last["ok"]:
        return "down"
    loss = last.get("loss_pct") or 0.0
    rtt = last.get("rtt_avg")
    if loss >= cfg.degraded_loss_pct or (rtt is not None and rtt >= cfg.degraded_latency_ms):
        return "degraded"
    return "up"


def _downsample(rows: list[dict], max_points: int) -> list[dict]:
    """Average consecutive ping rows into at most max_points buckets."""
    if len(rows) <= max_points:
        return rows
    size = len(rows) / max_points
    out = []
    i = 0.0
    while i < len(rows):
        bucket = rows[int(i):int(i + size)] or rows[int(i):int(i) + 1]
        avg = [r["rtt_avg"] for r in bucket if r["rtt_avg"] is not None]
        mins = [r["rtt_min"] for r in bucket if r.get("rtt_min") is not None]
        maxs = [r["rtt_max"] for r in bucket if r.get("rtt_max") is not None]
        loss = [r["loss_pct"] for r in bucket if r["loss_pct"] is not None]
        out.append({
            "ts": sum(r["ts"] for r in bucket) / len(bucket),
            "ok": int(any(r["ok"] for r in bucket)),
            "rtt_avg": sum(avg) / len(avg) if avg else None,
            "rtt_min": min(mins) if mins else None,
            "rtt_max": max(maxs) if maxs else None,
            "loss_pct": sum(loss) / len(loss) if loss else None,
            "error": next((r.get("error") for r in bucket if r.get("error")), None),
        })
        i += size
    return out


class NewHost(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    address: str = Field(min_length=1, max_length=255)
    check: str = "ping"                     # "ping" or "tcp"
    port: int | None = Field(None, ge=1, le=65535)   # tcp check port
    throughput_url: str | None = None       # optional http speed-test URL


def create_app(config: Config) -> FastAPI:
    db = Database(config.database)
    monitor = Monitor(config, db)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        await monitor.start()
        try:
            yield
        finally:
            await monitor.stop()
            db.close()

    app = FastAPI(title="site-status", lifespan=lifespan)
    app.state.config = config
    app.state.db = db

    @app.get("/api/status")
    def api_status(hours: float = Query(24, gt=0, le=24 * 90)):
        since = time.time() - hours * 3600
        out = []
        for host in config.hosts:
            last = db.last_ping(host.name)
            stats = db.uptime_stats(host.name, since)
            tp = db.last_throughput(host.name)
            check = host.ping or host.tcp
            out.append({
                "name": host.name,
                "address": host.address,
                "kind": "icmp" if host.ping else ("tcp" if host.tcp else "throughput"),
                "interval": check.interval if check else None,
                "has_throughput": host.throughput is not None,
                "status": host_status(config, host, last),
                "last": last,
                "last_throughput": tp,
                **stats,
            })
        return {
            "now": time.time(),
            "hours": hours,
            "thresholds": {
                "loss_pct": config.degraded_loss_pct,
                "latency_ms": config.degraded_latency_ms,
            },
            "hosts": out,
        }

    @app.get("/api/history/{host_name}")
    def api_history(host_name: str, hours: float = Query(24, gt=0, le=24 * 90),
                    max_points: int = Query(400, ge=10, le=5000)):
        if not any(h.name == host_name for h in config.hosts):
            raise HTTPException(404, f"unknown host: {host_name}")
        since = time.time() - hours * 3600
        return {
            "host": host_name,
            "hours": hours,
            "ping": _downsample(db.ping_history(host_name, since), max_points),
            "throughput": db.throughput_history(host_name, since),
        }

    @app.post("/api/hosts", status_code=201)
    async def api_add_host(body: NewHost):
        name = body.name.strip()
        address = body.address.strip()
        if any(h.name.lower() == name.lower() for h in config.hosts):
            raise HTTPException(409, f"a host named {name!r} already exists")
        if body.check not in ("ping", "tcp"):
            raise HTTPException(422, "check must be 'ping' or 'tcp'")
        if body.check == "tcp" and body.port is None:
            raise HTTPException(422, "tcp check requires a port")
        url = (body.throughput_url or "").strip()
        if url and not (url.startswith("http://") or url.startswith("https://")):
            raise HTTPException(422, "throughput URL must start with http:// or https://")

        checks: dict = {body.check: {"port": body.port} if body.check == "tcp" else {}}
        if url:
            checks["throughput"] = {"method": "http", "url": url}
        try:
            host = build_host({"name": name, "address": address, "checks": checks},
                              config.check_defaults)
        except ConfigError as e:
            raise HTTPException(422, str(e)) from None

        config.hosts.append(host)
        monitor.add_host(host)
        save_hosts(config)
        return {"ok": True, "name": host.name}

    @app.delete("/api/hosts/{host_name}")
    async def api_remove_host(host_name: str):
        host = next((h for h in config.hosts if h.name == host_name), None)
        if host is None:
            raise HTTPException(404, f"unknown host: {host_name}")
        config.hosts.remove(host)
        await monitor.remove_host(host.name)
        db.delete_host(host.name)
        save_hosts(config)
        return {"ok": True}

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
