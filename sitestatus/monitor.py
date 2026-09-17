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


class EventHub:
    """Fan-out of monitor events to live-view (SSE) subscribers."""

    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def publish(self, event: dict) -> None:
        for q in list(self._subs):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass  # slow consumer; it will catch up on its next poll


class Monitor:
    def __init__(self, config: Config, db: Database, hub: EventHub | None = None):
        self.config = config
        self.db = db
        self.hub = hub
        self._host_tasks: dict[str, list[asyncio.Task]] = {}
        self._prune_task: asyncio.Task | None = None

    def _notify(self, host_name: str) -> None:
        if self.hub:
            self.hub.publish({"type": "sample", "host": host_name})

    async def start(self) -> None:
        for host in self.config.hosts:
            self.add_host(host)
        self._prune_task = asyncio.create_task(self._prune_loop())
        log.info("monitor started: %d hosts", len(self._host_tasks))

    def add_host(self, host: Host) -> None:
        """Start check loops for a host; call on startup or a live add."""
        tasks = []
        if host.ping:
            tasks.append(asyncio.create_task(self._ping_loop(host)))
        if host.tcp:
            tasks.append(asyncio.create_task(self._tcp_loop(host)))
        if host.throughput:
            tasks.append(asyncio.create_task(self._throughput_loop(host)))
        self._host_tasks[host.name] = tasks

    async def remove_host(self, name: str) -> None:
        tasks = self._host_tasks.pop(name, [])
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def stop(self) -> None:
        all_tasks = [t for ts in self._host_tasks.values() for t in ts]
        if self._prune_task:
            all_tasks.append(self._prune_task)
        for t in all_tasks:
            t.cancel()
        await asyncio.gather(*all_tasks, return_exceptions=True)
        self._host_tasks.clear()
        self._prune_task = None

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
                self._notify(host.name)
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
                self._notify(host.name)
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
                self._notify(host.name)
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
