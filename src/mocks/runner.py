"""`uv run mocks ...`: start, reset and inspect the mock services (NF-1)."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Annotated

import httpx
import typer
from rich.console import Console
from rich.table import Table

from mocks.app import host_port
from mocks.services import SPECS
from router.config import ConfigError, LoadedConfig, load_config
from router.config.loader import OVERLAYS_ENV

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Local mock backend services.")
console = Console()

OverlayOpt = Annotated[list[Path] | None, typer.Option("--config", "-c", help="Overlay YAML file(s).")]


def _load(overlays: list[Path] | None) -> LoadedConfig:
    try:
        return load_config(overlays or ())
    except ConfigError as exc:
        console.print(f"[bold red]{exc}[/]")
        raise typer.Exit(2) from None


def _local_services(loaded: LoadedConfig) -> list[str]:
    """Services whose base_url points at this machine; real remote services are not started."""
    return [n for n in SPECS if host_port(loaded.settings.services[n].base_url)[0] in {"127.0.0.1", "localhost"}]


@app.command()
def up(config: OverlayOpt = None) -> None:
    """Start every local mock service, each in its own process. Ctrl+C stops them all."""
    loaded = _load(config)
    env = {**os.environ, OVERLAYS_ENV: os.pathsep.join(
        [p for p in os.environ.get(OVERLAYS_ENV, "").split(os.pathsep) if p] + [str(p.resolve()) for p in config or []]
    )}
    def _stop(signum: int, frame: object) -> None:
        raise KeyboardInterrupt

    # Explicit handlers: SIGINT may be inherited as ignored when launched from a script.
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    procs: dict[str, subprocess.Popen] = {}
    for name in _local_services(loaded):
        procs[name] = subprocess.Popen([sys.executable, "-m", "mocks.serve", name], env=env)
    try:
        _wait_healthy(loaded, list(procs))
        _print_status(loaded)
        console.print("[green]Mocks running.[/] Ctrl+C to stop.")
        while all(p.poll() is None for p in procs.values()):
            time.sleep(0.5)
        dead = [n for n, p in procs.items() if p.poll() is not None]
        console.print(f"[red]Service(s) exited: {', '.join(dead)}[/]")
    except KeyboardInterrupt:
        pass
    finally:
        for p in procs.values():
            if p.poll() is None:
                p.terminate()
        for p in procs.values():
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()


def _wait_healthy(loaded: LoadedConfig, names: list[str], timeout_s: float = 15) -> None:
    deadline = time.monotonic() + timeout_s
    pending = set(names)
    while pending and time.monotonic() < deadline:
        for n in list(pending):
            try:
                httpx.get(f"{loaded.settings.services[n].base_url}/_admin/health", timeout=0.5).raise_for_status()
                pending.discard(n)
            except httpx.HTTPError:
                pass
        time.sleep(0.2)
    if pending:
        raise RuntimeError(f"mock services did not become healthy: {sorted(pending)}")


def _admin(loaded: LoadedConfig, method: str, path: str, **kw) -> dict[str, dict]:
    out = {}
    for n in _local_services(loaded):
        try:
            r = httpx.request(method, f"{loaded.settings.services[n].base_url}{path}", timeout=5, **kw)
            r.raise_for_status()
            out[n] = r.json()
        except httpx.HTTPError as exc:
            out[n] = {"error": str(exc)}
    return out


def _print_status(loaded: LoadedConfig) -> None:
    table = Table("service", "url", "now", "seeded at", "calls", "faults")
    for n, h in _admin(loaded, "GET", "/_admin/health").items():
        url = loaded.settings.services[n].base_url
        if "error" in h:
            table.add_row(n, url, "[red]down[/]", "", "", "")
        else:
            now = h["now"] + (" (pinned)" if h["clock_pinned"] else "")
            table.add_row(n, url, now, h["seeded_now"], str(h["calls"]), "on" if h["faults_enabled"] else "off")
    console.print(table)


@app.command()
def status(config: OverlayOpt = None) -> None:
    """Show health, clock and call counts of each mock."""
    _print_status(_load(config))


@app.command()
def reset(
    config: OverlayOpt = None,
    now: Annotated[str | None, typer.Option(help="Pin 'now' (ISO-8601 with offset) before reseeding.")] = None,
) -> None:
    """Reset every mock to its seed state and clear call logs (MS-8, EV-5)."""
    loaded = _load(config)
    body = {"now": now} if now else None
    results = _admin(loaded, "POST", "/_admin/reset", json=body)
    failed = {n: r["error"] for n, r in results.items() if "error" in r}
    if failed:
        console.print(f"[red]Reset failed for: {failed}[/]")
        raise typer.Exit(1)
    _print_status(loaded)


if __name__ == "__main__":
    app()
