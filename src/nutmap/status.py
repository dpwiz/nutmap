import time

from rich.table import Table

from . import durable
from .batches import _counts, batch_table
from .context import STAGES
from .explain import journal_path as explain_journal
from .llm import merge_tokens, usd
from .pack import unpacked
from .panel import angle_journal, pilot_journal

ORDER = ["fetch", "inventory", "render", "pilot", "panel", "embed", "graph", "race", "explain", "layout", "flowmap", "web", "viewpoints", "export"]
COLORS = {"done": "green", "stale": "yellow", "partial": "cyan", "pending": "dim"}


def show(ctx, remote=False):
    con = ctx.console
    t = Table(title=f"nutmap · {ctx.data}")
    for col in ("stage", "state", "detail", "last run"):
        t.add_column(col)
    for name in ORDER:
        st = STAGES[name]
        try:
            state = ctx.stage_state(st)
        except Exception as e:
            state = f"error: {type(e).__name__}"
        stamp = ctx.read_stamp(st)
        try:
            detail = st.describe(ctx) if state != "pending" else ""
        except Exception:
            detail = ""
        t.add_row(name, f"[{COLORS.get(state, 'red')}]{state}[/]", detail, f"{stamp['seconds']}s" if stamp else "")
    con.print(t)
    stale = unpacked(ctx)
    if stale:
        con.print(f"[yellow]{len(stale)} tracked files changed since they were last packed[/] (python -m nutmap pack)")
    approval = durable.read_json(ctx.path("pilot", "approved.json"))
    report = durable.read_json(ctx.path("pilot", "report.json"))
    if report:
        con.print(f"pilot projection for the full panel: ${report['projected_full_usd']:.2f} ({report['full_calls']} calls)")
    con.print("full panel approved" if approval else "full panel [yellow]not approved[/] (python -m nutmap approve)")
    entries_path = ctx.path("inventory", "emoji.jsonl")
    n = len(durable.read_jsonl(entries_path)) if durable.exists(entries_path) else 0
    journals = {a["id"]: durable.Journal(angle_journal(ctx, a["id"]), readonly=True) for a in ctx.angles()}
    pj = durable.Journal(pilot_journal(ctx), readonly=True)
    xj = durable.Journal(explain_journal(ctx), readonly=True)
    for title, path, js, totals in (
        ("panel batches", ctx.path("panel", "batches.json"), journals, {g: n for g in journals}),
        ("explain batches", ctx.path("explain", "batches.json"), {"explain": xj}, None),
    ):
        state = durable.read_json(path)
        if not state:
            continue
        entries = state["entries"]
        if remote:
            client = ctx.client()
            for e in entries:
                if e["state"] == "submitted" and e.get("id"):
                    e["counts"] = _counts(client.messages.batches.retrieve(e["id"]))
        if totals is None:
            totals = {g: len({c for x in entries if x["group"] == g for c in x["custom_ids"]} | set(js[g].final)) for g in js}
        done = {g: len(j.final) for g, j in js.items()}
        con.print(batch_table(title, entries, totals, done, js, ctx.cfg["panel"]))
        stamp = max((x.get("submitting_at", 0) for x in entries), default=0)
        if stamp:
            con.print(f"last submission {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(stamp))}")
    tokens = merge_tokens(pj.tokens, xj.tokens, *(j.tokens for j in journals.values()))
    con.print(f"total spend so far: ${usd(tokens, ctx.cfg['panel']):.3f}")
