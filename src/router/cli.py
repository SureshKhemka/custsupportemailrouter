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
        err.print(f"[yellow]note:[/] {st['ratings_pending_human_review']} DS-7 rating(s) are rated by Claude, not yet reviewed by a person (D-049).")


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


# --------------------------------------------------------------------------- processing


def _router(loaded: LoadedConfig, now: str | None):
    from datetime import datetime

    from router.core.clock import FixedClock, make_clock
    from router.pipeline.runner import Router
    from router.store.db import Store

    from router.llm import LLMClient
    from router.pipeline.understanding import LLMUnderstander

    clock = FixedClock(datetime.fromisoformat(now)) if now else make_clock(loaded.settings.clock.fixed_now)
    store = Store(loaded.settings.paths.db_dir / "router.db")
    router = Router(loaded, store, clock)
    # Every model call lands in the case's audit trail (FR-39, LL-5).
    router.understander = LLMUnderstander(LLMClient(loaded, on_call=router.record_llm_call), loaded.settings)
    return router, store


NowOpt = Annotated[str | None, typer.Option(help="Pin 'now' (ISO-8601 with offset); overrides clock.fixed_now.")]


@app.command("process")
def process(
    config: OverlayOpt = None,
    inbox: Annotated[Path | None, typer.Option(help="Inbox folder (default: paths.inbox).")] = None,
    now: NowOpt = None,
) -> None:
    """Process every email in the inbox. Safe to re-run: seen emails are recognised as duplicates."""
    from rich.table import Table

    loaded = _load(config)
    router, store = _router(loaded, now)
    folder = inbox or loaded.settings.paths.inbox
    if not folder.is_dir():
        err.print(f"[red]Inbox folder not found: {folder}[/]")
        raise typer.Exit(code=2)
    summary = router.process_inbox(folder)
    table = Table("outcome", "count", title=f"Run {summary.run_id}")
    for k, v in sorted(summary.outcomes.items()):
        table.add_row(k, str(v))
    Console().print(table)
    for source, error in summary.failures:
        err.print(f"[red]failed[/] {source}: {error}")
    store.close()


@app.command("cases")
def cases(config: OverlayOpt = None, stage: Annotated[str | None, typer.Option()] = None) -> None:
    """List cases in the store."""
    from rich.table import Table

    from router.store.db import Store

    loaded = _load(config)
    store = Store(loaded.settings.paths.db_dir / "router.db")
    table = Table("case", "sender", "customer", "order", "stage", "disposition")
    for c in store.list_cases(**({"stage": stage} if stage else {})):
        table.add_row(c["case_id"], c["sender"], c["customer_id"] or "-", c["primary_order_id"] or "-", c["stage"],
                      c["disposition"] or "-")
    Console().print(table)


@app.command("case")
def case(case_id: str, config: OverlayOpt = None) -> None:
    """Show one case and its audit trail."""
    from router.store.db import Store

    loaded = _load(config)
    store = Store(loaded.settings.paths.db_dir / "router.db")
    c = store.get_case(case_id)
    if not c:
        err.print(f"[red]No case {case_id}[/]")
        raise typer.Exit(code=1)
    typer.echo(json.dumps(c, indent=2))
    for e in store.events(case_id):
        typer.echo(f"{e['seq']:>5} {e['at']} {e['step_id'] or '-':<32} {e['kind']}: {json.dumps(e['data'])[:160]}")


# --------------------------------------------------------------------------- evals

eval_app = typer.Typer(no_args_is_help=True, help="Evaluations (section 9).")
app.add_typer(eval_app, name="eval")


@eval_app.command("understand")
def eval_understand(
    config: OverlayOpt = None,
    split: Annotated[str, typer.Option(help="dev (default) or test. Test is for final measurement only.")] = "dev",
    mode: Annotated[str, typer.Option(help="live (calls the model, records outputs) or replay (no calls).")] = "live",
    limit: Annotated[int | None, typer.Option(help="Only the first N records.")] = None,
    ids: Annotated[str | None, typer.Option(help="Comma-separated record ids.")] = None,
    concurrency: Annotated[int | None, typer.Option(help="Parallel LLM calls (default: step config).")] = None,
    recording: Annotated[str, typer.Option(help="Recording file name under paths.recordings.")] = "eval",
) -> None:
    """Classification eval: intents, multi-intent, calibration, entities, signals, language, spam (9.3)."""
    import time

    from router.evals.understand import run_understand_eval

    if mode not in {"live", "replay"} or split not in {"dev", "test"}:
        err.print("[red]--mode must be live|replay and --split dev|test[/]")
        raise typer.Exit(code=2)
    if split == "test":
        err.print("[yellow]Held-out TEST split: use only for final measurement, never for tuning prompts.[/]")
    loaded = _load(config)
    started = time.monotonic()

    def progress(done: int, total: int) -> None:
        if done == total or done % 10 == 0:
            err.print(f"  {done}/{total} emails ({time.monotonic() - started:.0f}s)")

    out = run_understand_eval(loaded, split, mode, limit=limit, ids=ids.split(",") if ids else None,
                              concurrency=concurrency, recording=recording, progress=progress)
    report = json.loads((out / "report.json").read_text())
    for k, t in report["targets"].items():
        status = "[green]PASS[/]" if t["met"] else "[red]FAIL[/]"
        err.print(f"{status} {k} = {report['headline'][k]} (target {t['op']} {t['target']})")
    typer.echo(f"Report: {out / 'report.md'}")


@eval_app.command("e2e")
def eval_e2e(
    config: OverlayOpt = None,
    split: Annotated[str, typer.Option(help="dev (default) or test.")] = "dev",
    understanding: Annotated[str, typer.Option(help="oracle (labels, no LLM), replay (recorded LLM) or live.")] = "oracle",
    ids: Annotated[str | None, typer.Option(help="Comma-separated record ids (their whole groups run).")] = None,
) -> None:
    """End-to-end eval: dispositions, actions, hard gates, automation (9.2, 9.5)."""
    import time

    from router.evals.e2e import run_e2e_eval

    loaded = _load(config)
    if loaded.settings.routing.operating_mode.default != "live":
        err.print("[yellow]note:[/] labels assume live mode; add an overlay with routing.operating_mode.default: live")
    started = time.monotonic()
    out = run_e2e_eval(loaded, split, understanding, ids=ids.split(",") if ids else None,
                       progress=lambda d, t: err.print(f"  {d}/{t} emails ({time.monotonic() - started:.0f}s)")
                       if d == t or d % 50 < 3 else None)
    report = json.loads((out / "report.json").read_text())
    for k, v in report["hard_gates"].items():
        err.print(f"{'[green]PASS[/]' if v['passed'] else '[red]FAIL[/]'} {k}" + ("" if v["passed"] else f": {v['violations'][:3]}"))
    for k, t in report["targets"].items():
        err.print(f"{'[green]PASS[/]' if t['met'] else '[red]FAIL[/]'} {k} = {report['headline'][k]} (target {t['op']} {t['target']})")
    typer.echo(f"Report: {out / 'report.md'}")


if __name__ == "__main__":
    app()
