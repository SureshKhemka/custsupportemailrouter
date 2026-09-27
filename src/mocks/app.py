"""Build a mock service app from configuration."""

from __future__ import annotations

from urllib.parse import urlparse

from fastapi import FastAPI

from mocks.base import FaultInjector, MockClock, MockService, create_app
from mocks.services import SPECS
from router.config import LoadedConfig


def build_service(name: str, loaded: LoadedConfig, *, persist: bool = True) -> MockService:
    s = loaded.settings
    return MockService(
        spec=SPECS[name],
        seed_dir=s.paths.seed,
        clock=MockClock(s.clock.fixed_now),
        faults=FaultInjector(s.faults.enabled, s.faults.services.get(name), s.faults.seed, s.faults.timeout_hang_s),
        state_dir=s.paths.mock_state if persist else None,
        extra={"outbox_dir": s.paths.outbox} if name == "outbox" else {},
    )


def build_app(name: str, loaded: LoadedConfig, *, persist: bool = True) -> FastAPI:
    return create_app(build_service(name, loaded, persist=persist))


def host_port(base_url: str) -> tuple[str, int]:
    u = urlparse(base_url)
    return u.hostname or "127.0.0.1", u.port or (443 if u.scheme == "https" else 80)
