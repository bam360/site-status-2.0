"""Entry point: python -m sitestatus --config config.yaml"""

from __future__ import annotations

import argparse
import logging
import sys

import uvicorn

from .app import create_app
from .config import ConfigError, load_config


def main() -> int:
    parser = argparse.ArgumentParser(prog="sitestatus",
                                     description="Local-network uptime, latency and throughput monitor")
    parser.add_argument("--config", default="config.yaml", help="path to config file")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        config = load_config(args.config)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    app = create_app(config)
    uvicorn.run(app, host=config.listen_host, port=config.listen_port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
