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
dataset_app = typer.Typer(no_args_is_help=True, help="Labelled dataset: check, stats, write inbox.")
app.add_typer(dataset_app, name="dataset")

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


SplitOpt = Annotated[list[str] | None, typer.Option("--split", "-s", help="dev and/or test (default: both).")]


def _records(loaded: LoadedConfig, split: list[str] | None):
    from router.dataset.loader import SPLITS, load_records

    try:
        return load_records(loaded.settings.paths.dataset, tuple(split or SPLITS))
    except ValueError as exc:
        err.print(f"[bold red]{exc}[/]")
        raise typer.Exit(code=2) from None


@dataset_app.command("check")
def dataset_check(config: OverlayOpt = None, split: SplitOpt = None) -> None:
    """DS-5: verify every label against seed data, policy and routing config."""
    from rich.table import Table

    from router.dataset.check import check_dataset

    loaded = _load(config)
    res = check_dataset(_records(loaded, split), loaded.settings, loaded.settings.paths.dataset)
    st = res.stats
    table = Table("metric", "value", title="Dataset")
    for k in ("total", "by_split", "non_english_share", "multi_intent_share", "languages", "dispositions",
              "gate_cases_block", "gate_cases_pass", "rated_replies", "ratings_pending_human_review"):
        table.add_row(k, str(st.get(k)))
    Console().print(table)
    if res.problems:
        for p in res.problems:
            err.print(f"[red]-[/] {p}")
        err.print(f"[bold red]{len(res.problems)} problem(s)[/]")
        raise typer.Exit(code=1)
    typer.echo("Labels consistent with seed data, policy and config.")
    if st.get("ratings_pending_human_review"):
        err.print(f"[yellow]note:[/] {st['ratings_pending_human_review']} DS-7 rating(s) are Claude drafts awaiting human review.")


@dataset_app.command("inbox")
def dataset_inbox(
    config: OverlayOpt = None,
    split: SplitOpt = None,
    out: Annotated[Path | None, typer.Option(help="Target folder (default: paths.inbox).")] = None,
) -> None:
    """Write dataset emails into an inbox folder as email JSON files (FR-1)."""
    from router.dataset.loader import write_inbox

    loaded = _load(config)
    paths = write_inbox(_records(loaded, split), out or loaded.settings.paths.inbox)
    typer.echo(f"Wrote {len(paths)} email file(s) to {out or loaded.settings.paths.inbox}")


if __name__ == "__main__":
    app()
