"""Command-line entry point: `uv run router ...`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from router.config import ConfigError, LoadedConfig, load_config, missing_secrets

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Customer support email router.")
config_app = typer.Typer(no_args_is_help=True, help="Inspect and validate configuration.")
app.add_typer(config_app, name="config")

err = Console(stderr=True)

OverlayOpt = Annotated[
    list[Path] | None,
    typer.Option("--config", "-c", help="Overlay YAML file(s), applied after base and local (repeatable)."),
]


def _load(overlays: list[Path] | None) -> LoadedConfig:
    try:
        return load_config(overlays or ())
    except ConfigError as exc:
        err.print(f"[bold red]{exc}[/]")
        raise typer.Exit(code=2) from None


@config_app.command("validate")
def config_validate(
    config: OverlayOpt = None,
    require_secrets: Annotated[
        bool, typer.Option(help="Also fail if API key env vars needed by configured LLM steps are unset.")
    ] = False,
) -> None:
    """Validate configuration; exit non-zero with every problem if invalid (CF-4)."""
    loaded = _load(config)
    missing = missing_secrets(loaded)
    if missing:
        msg = "Missing environment variables: " + ", ".join(missing)
        if require_secrets:
            err.print(f"[bold red]{msg}[/]")
            raise typer.Exit(code=2)
        err.print(f"[yellow]warning:[/] {msg}")
    eff = loaded.effective()
    typer.echo(f"Configuration OK (fingerprint {eff['fingerprint']}, layers: {', '.join(eff['layers'])})")


@config_app.command("show")
def config_show(config: OverlayOpt = None) -> None:
    """Print the effective configuration as JSON, secrets removed (CF-6)."""
    typer.echo(json.dumps(_load(config).effective(), indent=2))


if __name__ == "__main__":
    app()
