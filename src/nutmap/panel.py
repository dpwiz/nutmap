import concurrent.futures as cf
import logging
import random
import time

from pydantic import BaseModel

from . import durable
from .batches import BatchRunner
from .context import PipelineError, Stage, WaitingForApproval, register
from .inventory import load_entries, load_resolver
from .llm import is_retryable, merge_tokens, parse_structured, usage_of, usd
from .progress import make_progress

log = logging.getLogger("nutmap")

PROMPT_VERSION = 1

SYSTEM = """You are one member of a panel that describes emoji. Every member looks at the emoji from a different viewpoint.

Your viewpoint: {title}. {instruction}

For the emoji you are given, return JSON with:
- "description": 2 to 4 sentences describing the emoji strictly from your viewpoint.
- "keywords": up to 8 short keywords from your viewpoint.
- "links": 3 to 8 other standard Unicode emoji that this emoji naturally leads to from your viewpoint. Each link has "emoji" (exactly one emoji and nothing else, no words) and "relation" (a short phrase under 12 words saying how the two connect). Never link the emoji to itself."""

USER = "Emoji: {char}\nName: {name}\nKeywords: {keywords}\nCategory: {group} / {subgroup}"

SCHEMA = {
    "type": "object",
    "properties": {
        "description": {"type": "string"},
        "keywords": {"type": "array", "items": {"type": "string"}},
        "links": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"emoji": {"type": "string"}, "relation": {"type": "string"}},
                "required": ["emoji", "relation"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["description", "keywords", "links"],
    "additionalProperties": False,
}


class Link(BaseModel):
    emoji: str
    relation: str


class Reply(BaseModel):
    description: str
    keywords: list[str]
    links: list[Link]


def custom_id(angle, entry):
    return f"{angle['id']}:{entry['id']}"


def params_for(ctx, angle, entry):
    pc = ctx.cfg["panel"]
    return {
        "model": pc["model"],
        "max_tokens": pc["max_tokens"],
        "system": SYSTEM.format(title=angle["title"], instruction=angle["instruction"]),
        "messages": [
            {
                "role": "user",
                "content": USER.format(
                    char=entry["char"],
                    name=entry["name"],
                    keywords=", ".join(entry["keywords"]) or "(none)",
                    group=entry["group"],
                    subgroup=entry["subgroup"],
                ),
            }
        ],
        "output_config": {"effort": pc["effort"], "format": {"type": "json_schema", "schema": SCHEMA}},
    }


def resolved_links(rec, resolver):
    out = []
    for lk in rec["links"]:
        target = resolver(lk["emoji"])
        if target != rec["emoji_id"]:
            out.append(dict(lk, id=target))
    return out


def make_interpret(resolver):
    def interpret(cid, msg):
        status, payload = parse_structured(msg, Reply)
        angle_id, emoji_id = cid.split(":", 1)
        rec = {"key": cid, "status": status, "angle": angle_id, "emoji_id": emoji_id, "usage": usage_of(msg)}
        if status != "ok":
            rec.update(payload)
            return rec
        if not payload["description"].strip():
            rec.update(status="failed", error="empty description")
            return rec
        links = []
        for link in payload["links"][:8]:
            target = resolver(link["emoji"])
            if target == emoji_id:
                continue
            links.append({"emoji": link["emoji"], "relation": link["relation"].strip(), "id": target})
        rec.update(
            description=payload["description"].strip(),
            keywords=[k.strip() for k in payload["keywords"][:8] if k.strip()],
            links=links,
        )
        return rec

    return interpret


def pilot_entries(ctx, entries, resolver):
    by_id = {e["id"]: e for e in entries}
    size = min(ctx.cfg["panel"]["pilot_size"], len(entries))
    ids = []
    for c in [ctx.cfg["root"]] + ctx.cfg["panel"]["pilot_probes"]:
        i = resolver(c)
        if i and i not in ids:
            ids.append(i)
    rng = random.Random(1234)
    groups = sorted({e["group"] for e in entries})
    pool = {g: [e["id"] for e in entries if e["group"] == g and e["id"] not in ids] for g in groups}
    for g in groups:
        rng.shuffle(pool[g])
    while len(ids) < size and any(pool.values()):
        for g in groups:
            if pool[g] and len(ids) < size:
                ids.append(pool[g].pop())
    return [by_id[i] for i in ids[:size]]


def pilot_journal(ctx):
    return ctx.path("pilot", "pilot.journal.jsonl")


@register
class Pilot(Stage):
    name = "pilot"
    dir = "pilot"
    needs = ("inventory",)

    def config_slice(self, ctx):
        pc = ctx.cfg["panel"]
        return {
            "model": pc["model"],
            "effort": pc["effort"],
            "max_tokens": pc["max_tokens"],
            "size": pc["pilot_size"],
            "probes": pc["pilot_probes"],
            "angles": ctx.angles(),
            "prompt": PROMPT_VERSION,
        }

    def outputs(self, ctx):
        return ["pilot/report.md", "pilot/report.json"]

    def describe(self, ctx):
        j = durable.Journal(pilot_journal(ctx), readonly=True)
        return f"{len(j.final)} calls journaled, ${usd(j.tokens, ctx.cfg['panel']):.3f} spent"

    def run(self, ctx):
        entries = load_entries(ctx)
        resolver = load_resolver(ctx, entries)
        chosen = pilot_entries(ctx, entries, resolver)
        angles = ctx.angles()
        journal = durable.Journal(pilot_journal(ctx))
        interpret = make_interpret(resolver)
        max_attempts = ctx.cfg["panel"]["max_attempts"]
        todo = [(a, e) for e in chosen for a in angles if not journal.done(custom_id(a, e))]
        client = ctx.client() if todo else None
        total = len(chosen) * len(angles)

        def work(angle, entry):
            cid = custom_id(angle, entry)
            p = params_for(ctx, angle, entry)
            while True:
                if journal.failures.get(cid, 0) >= max_attempts:
                    journal.append({"key": cid, "status": "dead", "error": f"failed {max_attempts} attempts", "mode": "sync"})
                    return
                try:
                    msg = durable.with_retries(lambda: client.messages.create(**p), retryable=is_retryable, what=f"pilot {cid}")
                except durable.Interrupted:
                    raise
                except Exception as e:
                    if is_retryable(e):
                        raise
                    journal.append({"key": cid, "status": "failed", "error": f"{type(e).__name__}: {e}", "mode": "sync"})
                    continue
                durable.chaos("pilot after call")
                rec = interpret(cid, msg)
                rec["mode"] = "sync"
                journal.append(rec)
                if rec["status"] != "failed":
                    return

        with make_progress(ctx.console) as prog:
            task = prog.add_task("pilot", total=total, completed=total - len(todo), note="")
            with cf.ThreadPoolExecutor(max_workers=ctx.cfg["panel"]["pilot_concurrency"]) as pool:
                it = iter(todo)
                inflight = set()
                while True:
                    while not durable.stopping() and len(inflight) < ctx.cfg["panel"]["pilot_concurrency"]:
                        nxt = next(it, None)
                        if nxt is None:
                            break
                        inflight.add(pool.submit(work, *nxt))
                    if not inflight:
                        break
                    done, inflight = cf.wait(inflight, return_when=cf.FIRST_COMPLETED)
                    for f in done:
                        f.result()
                        prog.advance(task)
                    prog.update(task, description=f"pilot ${usd(journal.tokens, ctx.cfg['panel']):.3f}")
        journal.close()
        durable.check_stop()
        write_pilot_report(ctx, chosen, angles, durable.Journal(pilot_journal(ctx), readonly=True), len(entries))


def write_pilot_report(ctx, chosen, angles, journal, n_entries):
    resolver = load_resolver(ctx)
    recs = list(journal.final.values())
    ok = [dict(r, links=resolved_links(r, resolver)) for r in recs if r["status"] == "ok"]
    links = [lk for r in ok for lk in r["links"]]
    resolved = [lk for lk in links if lk["id"]]
    calls = len(recs) + sum(journal.failures.values())
    tin = sum(t.get("in", 0) for t in journal.tokens.values())
    tout = sum(t.get("out", 0) for t in journal.tokens.values())
    pc = ctx.cfg["panel"]
    per_call_in = tin / max(1, calls)
    per_call_out = tout / max(1, calls)
    full_calls = n_entries * len(angles)
    projected = usd({"batch": {"in": per_call_in * full_calls, "out": per_call_out * full_calls}}, pc)
    unresolved = {}
    for lk in links:
        if not lk["id"]:
            unresolved[lk["emoji"]] = unresolved.get(lk["emoji"], 0) + 1
    stats = {
        "emoji": len(chosen),
        "angles": len(angles),
        "ok": len(ok),
        "refused": sum(1 for r in recs if r["status"] == "refused"),
        "dead": sum(1 for r in recs if r["status"] == "dead"),
        "failed_attempts": sum(journal.failures.values()),
        "links": len(links),
        "links_resolved": len(resolved),
        "link_resolution_rate": round(len(resolved) / max(1, len(links)), 4),
        "avg_in_tokens": round(per_call_in, 1),
        "avg_out_tokens": round(per_call_out, 1),
        "pilot_usd": round(usd(journal.tokens, pc), 4),
        "full_calls": full_calls,
        "projected_full_usd": round(projected, 2),
    }
    root_id = resolver(ctx.cfg["root"])
    ok_by_key = {r["key"]: r for r in ok}
    lines = [f"# Panel pilot report\n", "| metric | value |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in stats.items()]
    lines.append("\n## Failed attempts by reason\n")
    for reason, n in sorted(journal.errors.items(), key=lambda kv: -kv[1]):
        lines.append(f"- {reason} ×{n}")
    lines += [f"- refused ({r.get('category')})" for r in recs if r["status"] == "refused"]
    if not journal.errors and not stats["refused"]:
        lines.append("- none")
    lines.append("\n## Most common unresolved link targets\n")
    for emo, n in sorted(unresolved.items(), key=lambda kv: -kv[1])[:20]:
        lines.append(f"- `{emo}` ×{n}")
    if not unresolved:
        lines.append("- none")
    lines.append(f"\n## {ctx.cfg['root']} through every viewpoint\n")
    for a in angles:
        key = f"{a['id']}:{root_id}"
        r = ok_by_key.get(key)
        if r is None:
            status = journal.final[key]["status"] if key in journal.final else "missing"
            lines.append(f"### {a['title']}\n\n_{status}_\n")
            continue
        lines.append(f"### {a['title']}\n\n{r['description']}\n\nkeywords: {', '.join(r['keywords'])}\n\nlinks:\n")
        lines += [f"- {x['emoji']}: {x['relation']}" + ("" if x["id"] else " (unresolved)") for x in r["links"]]
        lines.append("")
    durable.atomic_write_text(ctx.path("pilot", "report.md"), "\n".join(lines) + "\n")
    durable.atomic_write_json(ctx.path("pilot", "report.json"), stats)
    log.info("pilot: %s", stats)


def approve(ctx):
    report = ctx.path("pilot", "report.json")
    if ctx.stage_state(PILOT) != "done":
        raise PipelineError("pilot is not done yet; run `python -m nutmap pilot` first")
    stats = durable.read_json(report)
    durable.atomic_write_json(
        ctx.path("pilot", "approved.json"),
        {"report_sha256": durable.sha256_file(report), "projected_full_usd": stats["projected_full_usd"], "approved_at": time.time()},
    )
    log.info("approved full panel run (projected $%.2f)", stats["projected_full_usd"])


PILOT = Pilot()


def angle_journal(ctx, angle_id):
    return ctx.path("panel", f"{angle_id}.journal.jsonl")


@register
class Panel(Stage):
    name = "panel"
    dir = "panel"
    needs = ("inventory", "pilot")

    def config_slice(self, ctx):
        return PILOT.config_slice(ctx) | {"size": None, "probes": None}

    def outputs(self, ctx):
        return ["panel/panel.jsonl", "panel/summary.json"]

    def describe(self, ctx):
        n = 0
        tokens = {}
        for a in ctx.angles():
            j = durable.Journal(angle_journal(ctx, a["id"]), readonly=True)
            n += len(j.final)
            tokens = merge_tokens(tokens, j.tokens)
        return f"{n} results journaled, ${usd(tokens, ctx.cfg['panel']):.2f} spent"

    def run(self, ctx):
        approval = durable.read_json(ctx.path("pilot", "approved.json"))
        if approval is None:
            raise WaitingForApproval(f"panel: waiting for approval; read {ctx.path('pilot', 'report.md')}, then run `python -m nutmap approve`")
        if approval["report_sha256"] != durable.sha256_file(ctx.path("pilot", "report.json")):
            raise WaitingForApproval(f"panel: the pilot report changed since it was approved; re-read {ctx.path('pilot', 'report.md')}, then run `python -m nutmap approve`")
        entries = load_entries(ctx)
        resolver = load_resolver(ctx, entries)
        angles = ctx.angles()
        journals = {a["id"]: durable.Journal(angle_journal(ctx, a["id"])) for a in angles}
        pilot = durable.Journal(pilot_journal(ctx), readonly=True)
        for rec in pilot.final.values():
            j = journals.get(rec.get("angle"))
            if j is not None and rec["status"] == "ok" and not j.done(rec["key"]):
                j.append(dict(rec, mode="pilot-reuse", usage={}), sync=False)
        for j in journals.values():
            j.sync()
        groups = {a["id"]: {custom_id(a, e): params_for(ctx, a, e) for e in entries} for a in angles}
        runner = BatchRunner(
            ctx,
            ctx.path("panel", "batches.json"),
            groups,
            journals,
            make_interpret(resolver),
            ctx.cfg["panel"]["max_attempts"],
            ctx.cfg["panel"],
            "panel",
        )
        runner.run()
        rows = []
        stats = {"ok": 0, "refused": 0, "dead": 0, "links": 0, "links_resolved": 0}
        for a in angles:
            for cid in groups[a["id"]]:
                r = journals[a["id"]].final.get(cid)
                if r is None:
                    raise PipelineError(f"panel: {cid} has no final result")
                stats[r["status"]] += 1
                if r["status"] != "ok":
                    continue
                links = resolved_links(r, resolver)
                stats["links"] += len(links)
                stats["links_resolved"] += sum(1 for lk in links if lk["id"])
                rows.append({k: r[k] for k in ("key", "angle", "emoji_id", "description", "keywords")} | {"links": links})
        for j in journals.values():
            j.close()
        rows.sort(key=lambda r: r["key"])
        tokens = merge_tokens(*(j.tokens for j in journals.values()))
        stats["usd"] = round(usd(tokens, ctx.cfg["panel"]), 4)
        durable.atomic_write_jsonl(ctx.path("panel", "panel.jsonl"), rows)
        durable.atomic_write_json(ctx.path("panel", "summary.json"), stats)
        log.info("panel: %s", stats)
