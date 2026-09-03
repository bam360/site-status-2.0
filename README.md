# site-status 2.0

Uptime, latency and throughput monitor for your local network, with a
self-hosted web dashboard.

- **Ping checks** — ICMP ping (via the system `ping`) with round-trip
  min/avg/max latency and packet loss per round.
- **TCP checks** — connect latency to a host:port, for devices that block
  ICMP or to watch a specific service.
- **Throughput tests** — periodic download-speed measurement, either an
  HTTP download from a URL you point it at (a file on your NAS, router,
  etc.) or `iperf3` against a host running `iperf3 -s`.
- **History** — samples land in SQLite with configurable retention;
  the dashboard shows uptime %, latency charts, loss and throughput over
  1h / 6h / 24h / 7d / 30d, with light and dark themes.

## Quick start

Requires Python 3.10+.

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

cp config.example.yaml config.yaml
# edit config.yaml: list your hosts

python -m sitestatus --config config.yaml
```

Then open http://localhost:8080 (or whatever `listen` says) from any
machine on your network.

## Configuration

See [config.example.yaml](config.example.yaml) for the full annotated
example. Each host gets a name, an address, and one or more checks:

```yaml
hosts:
  - name: NAS
    address: 192.168.1.20
    checks:
      ping: {}                          # ICMP, defaults from `defaults:`
      throughput:
        method: http
        url: http://192.168.1.20/testfile.bin
  - name: Web app
    address: 192.168.1.40
    checks:
      tcp: { port: 443 }                # TCP connect latency
```

Notes:

- **ICMP ping** shells out to the system `ping`, so it works unprivileged
  on Linux and macOS. If a device drops ICMP, use a `tcp` check instead.
- **HTTP throughput** downloads for at most `max_seconds` (default 8) and
  reports Mbit/s. Point it at a reasonably large file (≥ 50 MB) served on
  the LAN so the measurement saturates the link, not the file.
- **iperf3 throughput** requires the `iperf3` binary locally and
  `iperf3 -s` running on the target — the most accurate way to measure
  LAN throughput.
- A host is **degraded** when loss ≥ `degraded_loss_pct` (default 20%) or
  latency ≥ `degraded_latency_ms` (default 250 ms); **down** when the last
  check failed; **no data** when samples stop arriving.

## HTTP API

- `GET /api/status?hours=24` — per-host summary: current status, uptime %,
  average/max latency, average loss, last throughput result.
- `GET /api/history/<host>?hours=24&max_points=400` — ping samples
  (bucket-averaged down to `max_points`) and throughput samples.

## Running as a service

A minimal systemd unit:

```ini
[Unit]
Description=site-status network monitor
After=network-online.target

[Service]
WorkingDirectory=/opt/site-status
ExecStart=/opt/site-status/.venv/bin/python -m sitestatus --config config.yaml
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

## Development

```sh
pip install -r requirements.txt
python -m pytest        # runs tests in tests/
```
