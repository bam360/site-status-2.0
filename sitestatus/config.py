"""Configuration loading and validation."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import yaml

DEFAULTS = {
    "ping_interval": 30,
    "ping_count": 5,
    "ping_timeout": 2,
    "tcp_interval": 30,
    "tcp_timeout": 3,
    "throughput_interval": 900,
    "throughput_max_seconds": 8,
    "degraded_loss_pct": 20,
    "degraded_latency_ms": 250,
}


@dataclasses.dataclass
class PingCheck:
    interval: int
    count: int
    timeout: int


@dataclasses.dataclass
class TcpCheck:
    port: int
    interval: int
    timeout: int


@dataclasses.dataclass
class ThroughputCheck:
    method: str  # "http" or "iperf3"
    interval: int
    max_seconds: int
    url: str | None = None   # http method
    port: int = 5201         # iperf3 method


@dataclasses.dataclass
class Host:
    name: str
    address: str
    ping: PingCheck | None = None
    tcp: TcpCheck | None = None
    throughput: ThroughputCheck | None = None


@dataclasses.dataclass
class Config:
    listen_host: str
    listen_port: int
    database: Path
    retention_days: int
    degraded_loss_pct: float
    degraded_latency_ms: float
    hosts: list[Host]


class ConfigError(Exception):
    pass


def _build_host(raw: dict[str, Any], d: dict[str, Any]) -> Host:
    try:
        name = str(raw["name"])
        address = str(raw["address"])
    except KeyError as e:
        raise ConfigError(f"host entry missing required key {e}: {raw!r}") from None

    checks = raw.get("checks") or {}
    if not isinstance(checks, dict) or not checks:
        raise ConfigError(f"host {name!r} has no checks configured")
    unknown = set(checks) - {"ping", "tcp", "throughput"}
    if unknown:
        raise ConfigError(f"host {name!r} has unknown check(s): {', '.join(sorted(unknown))}")

    host = Host(name=name, address=address)

    if "ping" in checks:
        c = checks["ping"] or {}
        host.ping = PingCheck(
            interval=int(c.get("interval", d["ping_interval"])),
            count=int(c.get("count", d["ping_count"])),
            timeout=int(c.get("timeout", d["ping_timeout"])),
        )

    if "tcp" in checks:
        c = checks["tcp"] or {}
        if "port" not in c:
            raise ConfigError(f"host {name!r}: tcp check requires a port")
        host.tcp = TcpCheck(
            port=int(c["port"]),
            interval=int(c.get("interval", d["tcp_interval"])),
            timeout=int(c.get("timeout", d["tcp_timeout"])),
        )

    if "throughput" in checks:
        c = checks["throughput"] or {}
        method = c.get("method", "http")
        if method not in ("http", "iperf3"):
            raise ConfigError(f"host {name!r}: throughput method must be http or iperf3")
        if method == "http" and not c.get("url"):
            raise ConfigError(f"host {name!r}: http throughput check requires a url")
        host.throughput = ThroughputCheck(
            method=method,
            interval=int(c.get("interval", d["throughput_interval"])),
            max_seconds=int(c.get("max_seconds", d["throughput_max_seconds"])),
            url=c.get("url"),
            port=int(c.get("port", 5201)),
        )

    return host


def load_config(path: str | Path) -> Config:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    raw = yaml.safe_load(path.read_text()) or {}

    d = {**DEFAULTS, **(raw.get("defaults") or {})}
    listen = raw.get("listen") or {}
    hosts_raw = raw.get("hosts") or []
    if not hosts_raw:
        raise ConfigError("config has no hosts")

    hosts = [_build_host(h, d) for h in hosts_raw]
    names = [h.name for h in hosts]
    if len(names) != len(set(names)):
        raise ConfigError("host names must be unique")

    db = Path(raw.get("database", "sitestatus.db"))
    if not db.is_absolute():
        db = path.parent / db

    return Config(
        listen_host=str(listen.get("host", "127.0.0.1")),
        listen_port=int(listen.get("port", 8080)),
        database=db,
        retention_days=int(raw.get("retention_days", 30)),
        degraded_loss_pct=float(d["degraded_loss_pct"]),
        degraded_latency_ms=float(d["degraded_latency_ms"]),
        hosts=hosts,
    )
