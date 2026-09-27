"""Shared fixtures: config pinned to the dataset's reference time, and in-process mock services."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from mocks.app import build_app
from mocks.services import SPECS
from router.config import load_config

ROOT = Path(__file__).resolve().parents[1]
REFERENCE_NOW = yaml.safe_load((ROOT / "dataset" / "dataset.yaml").read_text())["reference_now"]


@pytest.fixture(scope="session")
def pinned_config(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("cfg")
    overlay = tmp / "pinned.yaml"
    overlay.write_text(yaml.safe_dump({
        "clock": {"fixed_now": REFERENCE_NOW, "process_at_received_time": True},
        "routing": {"operating_mode": {"default": "live"}},
        "faults": {"timeout_hang_s": 0.01},
        "paths": {"outbox": str(tmp / "outbox"), "mock_state": str(tmp / "mocks"), "db_dir": str(tmp / "db")},
    }))
    return load_config([overlay], root=ROOT, use_local=False, env={})


@pytest.fixture(scope="session")
def mock_clients(pinned_config) -> dict[str, TestClient]:
    """One in-process app per mock service (no ports). Reset them with reset_mocks()."""
    return {name: TestClient(build_app(name, pinned_config, persist=False)) for name in SPECS}


def reset_mocks(clients: dict[str, TestClient]) -> None:
    for c in clients.values():
        c.post("/_admin/reset").raise_for_status()
        c.put("/_admin/faults", json={"enabled": False}).raise_for_status()


@pytest.fixture
def mocks(mock_clients) -> dict[str, TestClient]:
    """Mock services reset to seed state with faults off."""
    reset_mocks(mock_clients)
    return mock_clients


@pytest.fixture(scope="session")
def mocks_reset():
    return reset_mocks
