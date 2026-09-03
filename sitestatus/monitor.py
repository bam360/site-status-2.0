"""Background scheduler: one asyncio task per (host, check)."""

from __future__ import annotations

import asyncio
import logging
import random
import time

from . import checks
from .config import Config, Host
from .db import Database

log = logging.getLogger("sitestatus.monitor")

PRUNE_INTERVAL = 24 * 3600


class Monitor:
    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        for host in self.config.hosts:
            if host.ping:
                self._tasks.append(asyncio.create_task(self._ping_loop(host)))
            if host.tcp:
                self._tasks.append(asyncio.create_task(self._tcp_loop(host)))
            if host.throughput:
                self._tasks.append(asyncio.create_task(self._throughput_loop(host)))
        self._tasks.append(asyncio.create_task(self._prune_loop()))
        log.info("monitor started: %d tasks", len(self._tasks))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _ping_loop(self, host: Host) -> None:
        c = host.ping
        assert c is not None
        await asyncio.sleep(random.uniform(0, min(5, c.interval)))
        while True:
            started = time.monotonic()
            try:
                r = await checks.icmp_ping(host.address, c.count, c.timeout)
                self.db.add_ping(
                    host.name, "icmp", r["ok"],
                    rtt_avg=r.get("rtt_avg"), rtt_min=r.get("rtt_min"),
                    rtt_max=r.get("rtt_max"), loss_pct=r.get("loss_pct"),
                    error=r.get("error"),
                )
                if not r["ok"]:
                    log.warning("ping %s (%s): %s", host.name, host.address, r.get("error"))
            except Exception:
                log.exception("ping loop error for %s", host.name)
            await asyncio.sleep(max(1.0, c.interval - (time.monotonic() - started)))

    async def _tcp_loop(self, host: Host) -> None:
        c = host.tcp
        assert c is not None
        await asyncio.sleep(random.uniform(0, min(5, c.interval)))
        while True:
            started = time.monotonic()
            try:
                r = await checks.tcp_ping(host.address, c.port, c.timeout)
                self.db.add_ping(
                    host.name, "tcp", r["ok"],
                    rtt_avg=r.get("rtt_avg"), rtt_min=r.get("rtt_min"),
                    rtt_max=r.get("rtt_max"), loss_pct=r.get("loss_pct"),
                    error=r.get("error"),
                )
                if not r["ok"]:
                    log.warning("tcp %s (%s:%d): %s", host.name, host.address, c.port, r.get("error"))
            except Exception:
                log.exception("tcp loop error for %s", host.name)
            await asyncio.sleep(max(1.0, c.interval - (time.monotonic() - started)))

    async def _throughput_loop(self, host: Host) -> None:
        c = host.throughput
        assert c is not None
        await asyncio.sleep(random.uniform(2, 10))
        while True:
            started = time.monotonic()
            try:
                if c.method == "http":
                    assert c.url is not None
                    r = await checks.http_throughput(c.url, c.max_seconds)
                else:
                    r = await checks.iperf3_throughput(host.address, c.port, c.max_seconds)
                self.db.add_throughput(
                    host.name, c.method, r["ok"],
                    mbps=r.get("mbps"), nbytes=r.get("bytes"),
                    seconds=r.get("seconds"), error=r.get("error"),
                )
                if not r["ok"]:
                    log.warning("throughput %s: %s", host.name, r.get("error"))
            except Exception:
                log.exception("throughput loop error for %s", host.name)
            await asyncio.sleep(max(5.0, c.interval - (time.monotonic() - started)))

    async def _prune_loop(self) -> None:
        while True:
            try:
                self.db.prune(time.time() - self.config.retention_days * 86400)
            except Exception:
                log.exception("prune failed")
            await asyncio.sleep(PRUNE_INTERVAL)
