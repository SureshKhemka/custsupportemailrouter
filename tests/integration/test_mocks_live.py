"""`mocks up` starts every service as its own process on its own port (NF-1, MS-*)."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
PORT_BASE = 18100
SERVICES = ["customer", "order", "returns", "refund", "payments", "replacement", "outbox"]


@pytest.fixture
def running_mocks(tmp_path: Path):
    overlay = tmp_path / "live.yaml"
    overlay.write_text(yaml.safe_dump({
        "clock": {"fixed_now": "2026-09-27T10:00:00+05:30"},
        "paths": {"outbox": str(tmp_path / "outbox"), "mock_state": str(tmp_path / "state")},
        "services": {n: {"base_url": f"http://127.0.0.1:{PORT_BASE + i}"} for i, n in enumerate(SERVICES, 1)},
    }))
    proc = subprocess.Popen([sys.executable, "-m", "mocks.runner", "up", "-c", str(overlay)], cwd=ROOT,
                            env={**os.environ, "ROUTER_CONFIG_OVERLAYS": ""},
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    urls = {n: f"http://127.0.0.1:{PORT_BASE + i}" for i, n in enumerate(SERVICES, 1)}
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            if all(httpx.get(f"{u}/_admin/health", timeout=0.5).status_code == 200 for u in urls.values()):
                break
        except httpx.HTTPError:
            time.sleep(0.3)
    else:
        proc.kill()
        pytest.fail(f"mocks did not start:\n{proc.stdout.read() if proc.stdout else ''}")
    yield urls, overlay
    proc.send_signal(signal.SIGINT)
    proc.wait(timeout=10)


def test_all_services_run_as_separate_processes(running_mocks) -> None:
    urls, overlay = running_mocks
    health = {n: httpx.get(f"{u}/_admin/health").json() for n, u in urls.items()}
    assert {h["service"] for h in health.values()} == set(SERVICES)
    assert all(h["clock_pinned"] for h in health.values())

    cust = httpx.get(f"{urls['customer']}/customers/by-email", params={"email": "priya.patel@example.com"}).json()
    orders = httpx.get(f"{urls['order']}/customers/{cust['customer_id']}/orders").json()
    assert orders and all(o["customer_id"] == cust["customer_id"] for o in orders)

    msg = {"case_id": "C-1", "to": "priya.patel@example.com", "subject": "s", "body": "b", "language": "en"}
    assert httpx.post(f"{urls['outbox']}/messages", json=msg, headers={"Idempotency-Key": "C-1"}).status_code == 201

    # `mocks reset` resets every service over HTTP
    r = subprocess.run([sys.executable, "-m", "mocks.runner", "reset", "-c", str(overlay)], cwd=ROOT,
                       env={**os.environ, "ROUTER_CONFIG_OVERLAYS": ""}, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert all(httpx.get(f"{u}/_admin/calls").json() == [] for u in urls.values())
    assert httpx.get(f"{urls['outbox']}/messages").json() == []
