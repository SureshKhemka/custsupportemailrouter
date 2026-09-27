"""Run one mock service: `python -m mocks.serve <name>`. Used by `mocks up` (one process per service)."""

from __future__ import annotations

import sys

import uvicorn

from mocks.app import build_app, host_port
from router.config import load_config


def main() -> None:
    name = sys.argv[1]
    loaded = load_config()  # overlays arrive via ROUTER_CONFIG_OVERLAYS
    host, port = host_port(loaded.settings.services[name].base_url)
    uvicorn.run(build_app(name, loaded), host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
