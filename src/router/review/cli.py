"""`router review ...`: the agent's command-line interface (FR-34). No authentication (out of scope)."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from router.clients import ServiceError
from router.review.service import CaseView, ReviewError, ReviewService

app = typer.Typer(no_args_is_help=True, help="Review cases that need a human: queues, show, approve, reject, reassign.")
out = Console()
err = Console(stderr=True)

ConfigOpt = Annotated[list[Path] | None, typer.Option("--config", "-c", help="Overlay YAML file(s).")]
AgentOpt = Annotated[str, typer.Option(help="Agent name recorded with the action.")]
NowOpt = Annotated[str | None, typer.Option(help="Pin 'now' (ISO-8601 with offset) for SLA status.")]
SLA_STYLE = {"breached": "[bold red]BREACHED[/]", "approaching": "[yellow]approaching[/]", "ok": "[green]ok[/]", None: "-"}


def _service(config: list[Path] | None, now: str | None) -> tuple[ReviewService, datetime]:
    from router.cli import _load
    from router.clients import Backends
    from router.core.clock import FixedClock, make_clock
    from router.store.db import Store

    loaded = _load(config)
    s = loaded.settings
    clock = FixedClock(datetime.fromisoformat(now)) if now else make_clock(s.clock.fixed_now)
    return ReviewService(Store(s.paths.db_dir / "router.db"), Backends.from_config(s), s), clock.now()


def _fail(exc: Exception) -> None:
    """Review errors and backend outages end the command with a clear message."""
    err.print(f"[red]{exc}[/]")
    raise typer.Exit(code=1)


@app.command()
def queues(config: ConfigOpt = None, now: NowOpt = None) -> None:
    """Open cases per queue, with SLA warnings (FR-35, FR-37)."""
    svc, t = _service(config, now)
    table = Table("queue", "open", "priority", "approaching", "breached")
    for q in svc.queues(t):
        table.add_row(q["queue"], str(q["open"]), str(q["priority"]), str(q["approaching"]),
                      f"[red]{q['breached']}[/]" if q["breached"] else "0")
    out.print(table)


@app.command("list")
def list_cases(queue: Annotated[str | None, typer.Argument()] = None, config: ConfigOpt = None, now: NowOpt = None) -> None:
    """Cases waiting for a human, priority first, then by SLA due time."""
    svc, t = _service(config, now)
    table = Table("case", "queue", "SLA due", "SLA", "customer", "order", "intents", "flags")
    for c in svc.list_cases(queue, t):
        table.add_row(c["case_id"], c["queue"] + (" [bold magenta]P[/]" if c["priority"] else ""),
                      (c["sla_due"] or "-")[:16], SLA_STYLE[c["sla"]], c["customer_id"] or c["sender"],
                      c["primary_order_id"] or "-", ", ".join(c["intents"] or []), ", ".join(c["flags"] or []))
    out.print(table)


@app.command()
def show(case_id: str, config: ConfigOpt = None, now: NowOpt = None) -> None:
    """Everything an agent needs: summary, intents + evidence, facts, decision, draft (FR-34)."""
    svc, t = _service(config, now)
    try:
        v = svc.view(case_id, t)
    except (ReviewError, ServiceError) as exc:
        _fail(exc)
    _render(v)


def _render(v: CaseView) -> None:
    c = v.case
    out.print(Panel(f"[bold]{c['case_id']}[/]  queue [cyan]{c['queue']}[/]{'  [magenta]PRIORITY[/]' if c['priority'] else ''}"
                    f"  mode {c['mode']}  SLA {SLA_STYLE[v.sla]} (due {c['sla_due']})\n"
                    f"from {c['sender']}  customer {c['customer_id'] or 'NOT VERIFIED'}  flags {c['flags'] or []}",
                    title="Case"))
    if not c.get("summary"):
        out.print(Panel("No summary for this case (summaries off, or the summariser failed).", title="Summary"))
    else:
        s = c["summary"]
        out.print(Panel(f"{escape(s['summary'])}\n\n[bold]Asks:[/] {'; '.join(s['customer_asks'])}\n"
                        f"[bold]Key facts:[/] {'; '.join(s['key_facts'])}\n[bold]Risks:[/] {'; '.join(s['risk_flags']) or 'none'}\n"
                        f"[bold]Suggested next step:[/] {s['suggested_next_step']}", title="Summary (LLM, grounded)"))
    for e in v.emails:
        out.print(Panel(f"[bold]{e['subject']}[/]\n{e['body'].strip()}\n[dim]attachments: {e['attachments'] or 'none'}[/]",
                        title=f"Email {e['received_at']} ({e['outcome']})"))
    if v.understanding:
        u = v.understanding
        rows = "\n".join(f"- {d['intent']} ({d['confidence']:.2f}{'' if d['evidence_found'] else ', evidence NOT found'}): "
                         f"\"{d['evidence']}\"" for d in u.get("details", [])) or ", ".join(u["intents"])
        out.print(Panel(f"{rows}\nlanguage {u['language']}{' (code-mixed)' if u['code_mixed'] else ''}, tone {u['tone']}, "
                        f"injection {u['injection']}{' ' + repr(u['injection_evidence']) if u['injection'] else ''}"
                        f"{', UNCERTAIN: ' + str(u['uncertain']) if u['uncertain'] else ''}", title="Understanding (LLM)"))
    facts = "\n".join(
        f"- {o.order_id}: {o.status}, {', '.join(f'{l.name} x{l.qty} @ {l.unit_price:g}' for l in o.items)}; total {o.total:g}; "
        f"tracking {o.tracking_number or '-'}; delivered {o.delivered_at.date() if o.delivered_at else '-'}; "
        f"refunds {[(r.amount, r.status) for r in v.facts.refunds.get(o.order_id, [])]}; "
        f"charges {[(ch.amount, ch.status) for ch in v.facts.charges.get(o.order_id, [])]}" for o in v.facts.orders)
    out.print(Panel(facts or "no verified orders (sender not verified or no order)", title="Backend facts (live)"))
    if v.decision:
        d = v.decision
        rows = "\n".join(f"- {i['intent']} {i['order_id'] or ''}: {json.dumps(i['decision'])}" for i in d["intents"])
        acts = "\n".join(escape(f"- {a['type']} {a['order_id']} [{a['status']}] "
                                f"{a['request'].get('lines') or a['request'].get('amount') or ''}") for a in v.actions) or "none"
        out.print(Panel(f"{rows}\nsignals {d['signals']}  escalated {d['escalated']}\n[bold]Actions:[/]\n{acts}",
                        title="Decision (code)"))
    if v.draft:
        gate = v.draft["gate"] or {}
        out.print(Panel(v.draft["text"], title=f"Draft #{v.draft['seq']} gate: " +
                        ("[green]passed[/]" if not gate else f"[red]{gate}[/]")))
    else:
        out.print(Panel("No draft (legal/abuse cases are never drafted, or drafting was not possible). "
                        "Write the reply with: router review approve CASE --edit", title="Draft"))
    for a in v.agent_actions:
        out.print(f"[dim]{a['at']} {a['agent']}: {a['action']} {a['reason'] or ''} {a['edit_class'] or ''}[/]")


@app.command()
def approve(
    case_id: str,
    agent: AgentOpt = os.environ.get("USER", "agent"),
    edit: Annotated[bool, typer.Option("--edit", help="Open the draft in $EDITOR before sending.")] = False,
    text_file: Annotated[Path | None, typer.Option(help="Send this text instead of the draft.")] = None,
    no_actions: Annotated[bool, typer.Option("--no-actions", help="Do not run the proposed actions (a decision change).")] = False,
    config: ConfigOpt = None, now: NowOpt = None,
) -> None:
    """Approve (optionally edit) and send; runs proposed actions first; the reply is re-gated (FR-31)."""
    svc, t = _service(config, now)
    try:
        v = svc.view(case_id, t)
        text = text_file.read_text(encoding="utf-8") if text_file else None
        if edit:
            text = _edit(text or (v.draft["text"] if v.draft else ""))
        res = svc.approve(case_id, agent, t, text=text, run_actions=not no_actions)
    except (ReviewError, ServiceError) as exc:
        _fail(exc)
    for a in res.actions:
        out.print(f"action {a['type']}: {a['status']} {a.get('error', '')}")
    if res.sent:
        out.print(f"[green]Sent[/] {res.message_id} ({res.edit_class}{'' if res.edit_size is None else f', edit {res.edit_size}'})")
    else:
        err.print(f"[red]Not sent:[/] {res.problem}")
        if res.gate and not res.gate.passed:
            for check, reasons in res.gate.failures.items():
                err.print(f"  {check}: {reasons}")
        raise typer.Exit(code=1)


def _edit(text: str) -> str:
    editor = os.environ.get("EDITOR", "vi")
    with tempfile.NamedTemporaryFile("w+", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write(text)
        path = f.name
    subprocess.run([editor, path], check=True)
    edited = Path(path).read_text(encoding="utf-8")
    os.unlink(path)
    return edited


@app.command()
def reject(case_id: str, reason: Annotated[str, typer.Option(help="Why (recorded, FR-36).")],
           agent: AgentOpt = os.environ.get("USER", "agent"),
           close: Annotated[bool, typer.Option(help="Close the case without replying.")] = False,
           override: Annotated[bool, typer.Option(help="The system's decision was wrong (counts as an override).")] = False,
           config: ConfigOpt = None, now: NowOpt = None) -> None:
    """Reject the draft (the case stays open unless --close)."""
    svc, t = _service(config, now)
    try:
        svc.reject(case_id, agent, reason, t, close=close, decision_changed=override)
    except (ReviewError, ServiceError) as exc:
        _fail(exc)
    out.print("[yellow]Rejected[/]" + (" and closed" if close else "; the case stays open"))


@app.command()
def reassign(case_id: str, queue: str, agent: AgentOpt = os.environ.get("USER", "agent"),
             reason: Annotated[str, typer.Option()] = "", config: ConfigOpt = None, now: NowOpt = None) -> None:
    """Move a case to another queue."""
    svc, t = _service(config, now)
    try:
        svc.reassign(case_id, agent, queue, t, reason)
    except (ReviewError, ServiceError) as exc:
        _fail(exc)
    out.print(f"Moved {case_id} to {queue}")
