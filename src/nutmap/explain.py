import logging

import numpy as np
from pydantic import BaseModel

from . import durable
from .batches import BatchRunner
from .context import Stage, register
from .inventory import load_entries
from .llm import parse_structured, usage_of, usd

log = logging.getLogger("nutmap")

SYSTEM = "You explain single steps in a word-association race between emoji. Answer with JSON containing \"relation\": one short phrase (under 15 words) naming the most natural connection that leads from the first emoji to the second."

USER = "From {ca} ({na}) to {cb} ({nb})."

SCHEMA = {
    "type": "object",
    "properties": {"relation": {"type": "string"}},
    "required": ["relation"],
    "additionalProperties": False,
}


class Note(BaseModel):
    relation: str


def journal_path(ctx):
    return ctx.path("explain", "explain.journal.jsonl")


def focus_steps(ctx, ids):
    pred = np.load(ctx.path("race", "pred.npy")).astype(np.int64)
    n = len(ids)
    rows, cols = np.nonzero(~np.eye(n, dtype=bool))
    codes = np.unique(cols * n + pred[rows, cols])
    return [(ids[c // n], ids[c % n]) for c in codes]


@register
class Explain(Stage):
    name = "explain"
    dir = "explain"
    needs = ("inventory", "graph", "race")

    def config_slice(self, ctx):
        return {"explain": ctx.cfg["explain"], "version": 2}

    def outputs(self, ctx):
        return ["explain/edge_notes.jsonl", "explain/step_notes.jsonl"]

    def describe(self, ctx):
        j = durable.Journal(journal_path(ctx), readonly=True)
        return f"{len(j.final)} notes journaled, ${usd(j.tokens, ctx.cfg['panel']):.3f} spent"

    def run(self, ctx):
        entries = load_entries(ctx)
        by_id = {e["id"]: e for e in entries}
        edges = {}
        for r in durable.read_jsonl(ctx.path("graph", "edges.jsonl")):
            edges[(r["a"], r["b"])] = r
            edges[(r["b"], r["a"])] = r
        tree = [(r["id"], r["parent"]) for r in durable.read_jsonl(ctx.path("race", "root.jsonl")) if r["parent"]]
        xc = ctx.cfg["explain"]
        steps = focus_steps(ctx, [e["id"] for e in entries]) if xc["all_focuses"] else sorted(tree)
        params = {}
        for u, v in steps:
            if edges[(u, v)]["relation"]:
                continue
            ea, eb = by_id[u], by_id[v]
            params[f"e:{u}:{v}"] = {
                "model": xc["model"],
                "max_tokens": xc["max_tokens"],
                "system": SYSTEM,
                "messages": [{"role": "user", "content": USER.format(ca=ea["char"], na=ea["name"], cb=eb["char"], nb=eb["name"])}],
                "output_config": {"effort": xc["effort"], "format": {"type": "json_schema", "schema": SCHEMA}},
            }
        journal = durable.Journal(journal_path(ctx))

        def interpret(cid, msg):
            status, payload = parse_structured(msg, Note)
            return {"key": cid, "status": status, "usage": usage_of(msg), **payload}

        if params:
            BatchRunner(ctx, ctx.path("explain", "batches.json"), {"explain": params}, {"explain": journal}, interpret, ctx.cfg["panel"]["max_attempts"], ctx.cfg["panel"], "explain").run()
        def note(u, v):
            e = edges[(u, v)]
            if e["relation"]:
                return {"from": u, "to": v, "relation": e["relation"], "source": "parts" if "parts" in e["sources"] else "panel"}
            r = journal.final.get(f"e:{u}:{v}")
            return {"from": u, "to": v, "relation": r["relation"].strip() if r and r["status"] == "ok" else None, "source": "explain"}

        root_notes = [note(u, v) for u, v in sorted(tree)]
        step_notes = [note(u, v) for u, v in steps]
        journal.close()
        durable.atomic_write_jsonl(ctx.path("explain", "edge_notes.jsonl"), root_notes)
        durable.atomic_write_jsonl(ctx.path("explain", "step_notes.jsonl"), step_notes)
        log.info(
            "explain: %d steps across %s, %d needed a note, %d have none",
            len(step_notes),
            "all focuses" if xc["all_focuses"] else "the root's tree",
            len(params),
            sum(1 for x in step_notes if not x["relation"]),
        )
