"""The actual network checks: ICMP ping, TCP connect, throughput tests.

All checks are async and return plain dicts ready to be stored.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import sys
import time
import urllib.request

_LOSS_RE = re.compile(r"([\d.]+)% packet loss")
_RTT_RE = re.compile(r"min/avg/max[^=]*= ([\d.]+)/([\d.]+)/([\d.]+)")


def _ping_argv(address: str, count: int, timeout: int) -> list[str]:
    if sys.platform == "darwin":
        # macOS/BSD ping: -W is per-packet wait in milliseconds
        return ["ping", "-n", "-c", str(count), "-W", str(timeout * 1000), address]
    # Linux iputils ping: -W is per-packet wait in seconds, -i >= 0.2 unprivileged
    return ["ping", "-n", "-c", str(count), "-i", "0.25", "-W", str(timeout), address]


async def icmp_ping(address: str, count: int, timeout: int) -> dict:
    """Ping `address` and return latency (min/avg/max ms) and packet loss %."""
    if shutil.which("ping") is None:
        return {"ok": False, "error": "ping binary not found"}

    argv = _ping_argv(address, count, timeout)
    hard_limit = count * (timeout + 1) + 5
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), hard_limit)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {"ok": False, "loss_pct": 100.0, "error": "ping timed out"}
    except OSError as e:
        return {"ok": False, "error": f"ping failed to start: {e}"}

    out = stdout.decode(errors="replace") + stderr.decode(errors="replace")
    loss_m = _LOSS_RE.search(out)
    rtt_m = _RTT_RE.search(out)

    if loss_m is None:
        err = out.strip().splitlines()[-1] if out.strip() else "no output"
        return {"ok": False, "error": f"unparseable ping output: {err[:200]}"}

    loss = float(loss_m.group(1))
    result: dict = {"ok": loss < 100.0, "loss_pct": loss}
    if rtt_m:
        result["rtt_min"] = float(rtt_m.group(1))
        result["rtt_avg"] = float(rtt_m.group(2))
        result["rtt_max"] = float(rtt_m.group(3))
    if loss >= 100.0:
        result["error"] = "100% packet loss"
    return result


async def tcp_ping(address: str, port: int, timeout: int, attempts: int = 3) -> dict:
    """Measure TCP connect latency; loss % is the share of failed attempts."""
    rtts: list[float] = []
    last_err = None
    for _ in range(attempts):
        start = time.perf_counter()
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(address, port), timeout
            )
            rtts.append((time.perf_counter() - start) * 1000.0)
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
        except (OSError, asyncio.TimeoutError) as e:
            last_err = str(e) or e.__class__.__name__
        await asyncio.sleep(0.1)

    loss = 100.0 * (attempts - len(rtts)) / attempts
    if not rtts:
        return {"ok": False, "loss_pct": loss,
                "error": f"connect to :{port} failed: {last_err}"}
    return {
        "ok": True,
        "loss_pct": loss,
        "rtt_min": min(rtts),
        "rtt_avg": sum(rtts) / len(rtts),
        "rtt_max": max(rtts),
    }


def _http_download(url: str, max_seconds: int) -> dict:
    """Blocking download-speed measurement; run in a thread."""
    start = time.perf_counter()
    total = 0
    try:
        with urllib.request.urlopen(url, timeout=max_seconds) as resp:
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if time.perf_counter() - start >= max_seconds:
                    break
    except OSError as e:
        return {"ok": False, "error": f"download failed: {e}"}
    elapsed = time.perf_counter() - start
    if total == 0 or elapsed <= 0:
        return {"ok": False, "error": "no data received"}
    return {
        "ok": True,
        "mbps": total * 8 / elapsed / 1e6,
        "bytes": total,
        "seconds": elapsed,
    }


async def http_throughput(url: str, max_seconds: int) -> dict:
    return await asyncio.to_thread(_http_download, url, max_seconds)


async def iperf3_throughput(address: str, port: int, max_seconds: int) -> dict:
    """Run `iperf3 -c` against a host running `iperf3 -s`."""
    if shutil.which("iperf3") is None:
        return {"ok": False, "error": "iperf3 binary not found"}
    duration = max(1, min(max_seconds, 30))
    try:
        proc = await asyncio.create_subprocess_exec(
            "iperf3", "-c", address, "-p", str(port), "-t", str(duration), "-J",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), duration + 15)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {"ok": False, "error": "iperf3 timed out"}
    except OSError as e:
        return {"ok": False, "error": f"iperf3 failed to start: {e}"}

    try:
        data = json.loads(stdout.decode(errors="replace"))
    except ValueError:
        return {"ok": False, "error": f"iperf3 output unparseable: {stderr.decode(errors='replace')[:200]}"}
    if "error" in data:
        return {"ok": False, "error": f"iperf3: {data['error']}"}
    try:
        recv = data["end"]["sum_received"]
        return {
            "ok": True,
            "mbps": recv["bits_per_second"] / 1e6,
            "bytes": int(recv["bytes"]),
            "seconds": float(recv["seconds"]),
        }
    except KeyError:
        return {"ok": False, "error": "iperf3 result missing sum_received"}
