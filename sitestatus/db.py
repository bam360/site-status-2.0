"""SQLite storage for check samples.

Sample volume is low (a handful of hosts, one row every few seconds at
worst), so a single connection guarded by a lock is plenty.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS ping_samples (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    host TEXT NOT NULL,
    kind TEXT NOT NULL,          -- 'icmp' or 'tcp'
    ok INTEGER NOT NULL,
    rtt_avg REAL,                -- ms
    rtt_min REAL,
    rtt_max REAL,
    loss_pct REAL,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_ping_host_ts ON ping_samples (host, ts);

CREATE TABLE IF NOT EXISTS throughput_samples (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    host TEXT NOT NULL,
    method TEXT NOT NULL,        -- 'http' or 'iperf3'
    ok INTEGER NOT NULL,
    mbps REAL,
    bytes INTEGER,
    seconds REAL,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_tp_host_ts ON throughput_samples (host, ts);
"""


class Database:
    def __init__(self, path: str | Path):
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def add_ping(self, host: str, kind: str, ok: bool, rtt_avg=None,
                 rtt_min=None, rtt_max=None, loss_pct=None, error=None,
                 ts: float | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO ping_samples (ts, host, kind, ok, rtt_avg, rtt_min,"
                " rtt_max, loss_pct, error) VALUES (?,?,?,?,?,?,?,?,?)",
                (ts or time.time(), host, kind, int(ok), rtt_avg, rtt_min,
                 rtt_max, loss_pct, error),
            )
            self._conn.commit()

    def add_throughput(self, host: str, method: str, ok: bool, mbps=None,
                       nbytes=None, seconds=None, error=None,
                       ts: float | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO throughput_samples (ts, host, method, ok, mbps,"
                " bytes, seconds, error) VALUES (?,?,?,?,?,?,?,?)",
                (ts or time.time(), host, method, int(ok), mbps, nbytes,
                 seconds, error),
            )
            self._conn.commit()

    def ping_history(self, host: str, since: float) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, ok, rtt_avg, rtt_min, rtt_max, loss_pct, error"
                " FROM ping_samples WHERE host=? AND ts>=? ORDER BY ts",
                (host, since),
            ).fetchall()
        return [dict(r) for r in rows]

    def throughput_history(self, host: str, since: float) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, ok, mbps, bytes, seconds, method, error"
                " FROM throughput_samples WHERE host=? AND ts>=? ORDER BY ts",
                (host, since),
            ).fetchall()
        return [dict(r) for r in rows]

    def last_ping(self, host: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT ts, ok, rtt_avg, loss_pct, error FROM ping_samples"
                " WHERE host=? ORDER BY ts DESC LIMIT 1",
                (host,),
            ).fetchone()
        return dict(row) if row else None

    def last_throughput(self, host: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT ts, ok, mbps, method, error FROM throughput_samples"
                " WHERE host=? AND ok=1 ORDER BY ts DESC LIMIT 1",
                (host,),
            ).fetchone()
        return dict(row) if row else None

    def uptime_stats(self, host: str, since: float) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n, SUM(ok) AS up, AVG(rtt_avg) AS avg_rtt,"
                " MAX(rtt_max) AS max_rtt, AVG(loss_pct) AS avg_loss"
                " FROM ping_samples WHERE host=? AND ts>=?",
                (host, since),
            ).fetchone()
        n = row["n"] or 0
        return {
            "samples": n,
            "uptime_pct": (100.0 * (row["up"] or 0) / n) if n else None,
            "avg_rtt": row["avg_rtt"],
            "max_rtt": row["max_rtt"],
            "avg_loss": row["avg_loss"],
        }

    def delete_host(self, host: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM ping_samples WHERE host=?", (host,))
            self._conn.execute("DELETE FROM throughput_samples WHERE host=?", (host,))
            self._conn.commit()

    def prune(self, older_than: float) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM ping_samples WHERE ts < ?", (older_than,))
            self._conn.execute("DELETE FROM throughput_samples WHERE ts < ?", (older_than,))
            self._conn.commit()
