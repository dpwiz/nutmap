import hashlib
import logging
import re
import time

from rich import box
from rich.live import Live
from rich.table import Table

from . import durable
from .context import PipelineError
from .llm import is_retryable, usd

log = logging.getLogger("nutmap")

ACTIVE = ("submitting", "submitted", "ended")
API_ID = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def api_id(key):
    s = key.replace(":", "-")
    return s if API_ID.match(s) else "h-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:40]


def _counts(b):
    c = b.request_counts
    return {k: getattr(c, k, 0) or 0 for k in ("processing", "succeeded", "errored", "canceled", "expired")}


def _ts(created_at):
    return created_at.timestamp() if hasattr(created_at, "timestamp") else float(created_at)


def typical_duration(entries):
    ds = sorted(e["ended_at"] - e["created_at"] for e in entries if e.get("ended_at") and e.get("created_at"))
    return ds[len(ds) // 2] if ds else None


def eta_seconds(entry, now=None, typical=None):
    c = entry.get("counts")
    if not c or entry["state"] != "submitted" or not entry.get("created_at"):
        return None, False
    done = c["succeeded"] + c["errored"] + c["canceled"] + c["expired"]
    if c["processing"] == 0:
        return 0.0, False
    elapsed = (now or time.time()) - entry["created_at"]
    if done > 0 and elapsed > 0:
        return c["processing"] * elapsed / done, False
    if typical is not None:
        return max(0.0, typical - elapsed), True
    return None, False


def fmt_eta(s, approx=False):
    if s is None:
        return "?"
    s = int(s)
    text = f"{s // 3600}h{s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m{s % 60:02d}s"
    return f"~{text}" if approx else text


def batch_table(title, entries, totals, done, journals, price_cfg):
    t = Table(title=title, expand=False, box=box.SIMPLE_HEAD, pad_edge=False, padding=(0, 1))
    for col, just in (("group", "left"), ("state", "left"), ("done", "right"), ("queued", "right"), ("err", "right"), ("eta", "right")):
        t.add_column(col, justify=just, no_wrap=True)
    now = time.time()
    typical = typical_duration(entries)
    for g, total in totals.items():
        es = [e for e in entries if e["group"] == g]
        e = es[-1] if es else None
        c = (e or {}).get("counts") or {}
        state = f"{e['state']} #{e['seq']}" if e else ("done" if done[g] == total else "planned")
        errs = c.get("errored", 0) + c.get("expired", 0) + c.get("canceled", 0) if c else ""
        t.add_row(
            g,
            state,
            f"{done[g]}/{total}",
            str(c.get("processing", "")),
            str(errs),
            fmt_eta(*eta_seconds(e, now, typical)) if e and e["state"] == "submitted" else "",
        )
    tokens = {}
    for j in journals.values():
        for mode, tk in j.tokens.items():
            d = tokens.setdefault(mode, {})
            for k, v in tk.items():
                d[k] = d.get(k, 0) + v
    etas = [eta_seconds(e, now, typical) for e in entries if e["state"] == "submitted"]
    known = [(s, a) for s, a in etas if s is not None]
    if not etas:
        overall = (0, False)
    elif len(known) < len(etas):
        overall = (None, False)
    else:
        overall = (max(s for s, _ in known), any(a for _, a in known))
    t.caption = f"spend so far ${usd(tokens, price_cfg):.3f} · overall eta {fmt_eta(*overall)}"
    return t


class BatchRunner:
    def __init__(self, ctx, state_path, groups, journals, interpret, max_attempts, price_cfg, title):
        self.ctx = ctx
        self.client = ctx.client()
        self.state_path = state_path
        self.groups = groups
        self.journals = journals
        self.interpret = interpret
        self.max_attempts = max_attempts
        self.price_cfg = price_cfg
        self.title = title
        self.poll = ctx.cfg["panel"]["poll_seconds"]
        self.settle = ctx.cfg["panel"]["lost_batch_settle_seconds"]
        self.state = durable.read_json(state_path, {"entries": []})
        self.key_of = {}
        for reqs in groups.values():
            for key in reqs:
                a = api_id(key)
                if self.key_of.setdefault(a, key) != key:
                    raise PipelineError(f"{title}: custom ids {key!r} and {self.key_of[a]!r} collide as {a!r}")

    def save(self):
        durable.atomic_write_json(self.state_path, self.state)

    def entries(self, group=None, states=None):
        return [e for e in self.state["entries"] if (group is None or e["group"] == group) and (states is None or e["state"] in states)]

    def pending(self, group):
        j = self.journals[group]
        out = []
        for cid in self.groups[group]:
            if j.done(cid):
                continue
            if j.failures.get(cid, 0) >= self.max_attempts:
                j.append({"key": cid, "status": "dead", "error": f"failed {self.max_attempts} attempts"})
                continue
            out.append(cid)
        return out

    def submit(self, group, cids):
        seq = 1 + max((e["seq"] for e in self.entries(group)), default=0)
        entry = {"group": group, "seq": seq, "state": "submitting", "n": len(cids), "custom_ids": cids, "submitting_at": time.time()}
        self.state["entries"].append(entry)
        self.save()
        durable.chaos("batch before create")
        requests = [{"custom_id": api_id(cid), "params": self.groups[group][cid]} for cid in cids]
        try:
            b = self.client.messages.batches.create(requests=requests)
        except Exception as e:
            if is_retryable(e):
                log.warning("%s: create for %s#%d raised %s; will reconcile against server", self.title, group, seq, type(e).__name__)
                return
            entry["state"] = "rejected"
            entry["error"] = f"{type(e).__name__}: {e}"
            self.save()
            raise PipelineError(f"{self.title}: batch for {group} rejected: {e}")
        durable.chaos("batch after create")
        entry.update(state="submitted", id=b.id, created_at=_ts(b.created_at), counts=_counts(b))
        self.save()
        log.info("%s: submitted %s#%d as %s (%d requests)", self.title, group, seq, b.id, len(cids))

    def has_orphan(self):
        return any(not e.get("id") for e in self.entries(states=("submitting",)))

    def reconcile(self):
        orphans = [e for e in self.entries(states=("submitting",)) if not e.get("id")]
        if not orphans:
            return
        known = {e.get("id") for e in self.state["entries"] if e.get("id")}
        oldest = min(e["submitting_at"] for e in orphans) - 300
        server = durable.with_retries(lambda: self._recent(oldest), retryable=is_retryable, what="list batches")
        for e in orphans:
            cands = [b for b in server if b.id not in known and _ts(b.created_at) >= e["submitting_at"] - 300 and sum(_counts(b).values()) == e["n"]]
            if len(cands) == 1:
                b = cands[0]
                e.update(state="submitted", id=b.id, created_at=_ts(b.created_at), counts=_counts(b), adopted=True)
                known.add(b.id)
                log.warning("%s: adopted orphaned batch %s for %s#%d", self.title, b.id, e["group"], e["seq"])
            elif len(cands) > 1:
                self.save()
                ids = ", ".join(b.id for b in cands)
                raise PipelineError(f"{self.title}: {e['group']}#{e['seq']} matches several server batches ({ids}); edit {self.state_path} to set the right id")
            elif time.time() - e["submitting_at"] >= self.settle:
                if not self.ctx.cfg["panel"]["auto_resubmit_lost"]:
                    self.save()
                    raise PipelineError(
                        f"{self.title}: {e['group']}#{e['seq']} has no matching server batch; set its state to \"lost\" in {self.state_path} to resubmit, or add its id to adopt it"
                    )
                e["state"] = "lost"
                log.warning("%s: %s#%d never reached the server, will resubmit", self.title, e["group"], e["seq"])
        self.save()

    def _recent(self, oldest):
        out = []
        for i, b in enumerate(self.client.messages.batches.list(limit=100)):
            if i >= 1000:
                break
            if _ts(b.created_at) >= oldest:
                out.append(b)
        return out

    def poll_one(self, e):
        b = durable.with_retries(lambda: self.client.messages.batches.retrieve(e["id"]), retryable=is_retryable, what=f"retrieve {e['id']}")
        e["counts"] = _counts(b)
        if b.processing_status == "ended":
            e["state"] = "ended"
            e["ended_at"] = time.time()
            log.info("%s: %s#%d ended %s", self.title, e["group"], e["seq"], e["counts"])

    def collect(self, e):
        j = self.journals[e["group"]]
        wanted = set(e["custom_ids"])

        def stream():
            n = 0
            for r in self.client.messages.batches.results(e["id"]):
                durable.check_stop()
                cid = self.key_of.get(r.custom_id)
                if cid not in wanted or j.done(cid) or (cid, e["id"]) in j.failed_in:
                    continue
                durable.chaos("collect item")
                if r.result.type == "succeeded":
                    rec = self.interpret(cid, r.result.message)
                    rec["mode"] = "batch"
                    rec["batch"] = e["id"]
                else:
                    err = getattr(r.result, "error", None)
                    detail = getattr(getattr(err, "error", None), "type", None) or getattr(err, "type", None)
                    rec = {"key": cid, "status": "failed", "error": f"{r.result.type}: {detail}", "mode": "batch", "batch": e["id"]}
                j.append(rec, sync=False)
                n += 1
                if n % 200 == 0:
                    j.sync()
            j.sync()

        durable.with_retries(stream, retryable=is_retryable, what=f"results {e['id']}")
        e["state"] = "collected"
        self.save()

    def table(self):
        totals = {g: len(c) for g, c in self.groups.items()}
        done = {g: sum(1 for c in self.groups[g] if self.journals[g].done(c)) for g in self.groups}
        return batch_table(self.title, self.state["entries"], totals, done, self.journals, self.price_cfg)

    def run(self):
        with Live(self.table(), console=self.ctx.console, refresh_per_second=2, transient=False) as live:
            while True:
                durable.check_stop()
                self.reconcile()
                busy = False
                for g in self.groups:
                    active = self.entries(g, ACTIVE)
                    if active:
                        busy = True
                        continue
                    p = self.pending(g)
                    if p:
                        busy = True
                        if not self.has_orphan():
                            self.submit(g, p)
                    live.update(self.table())
                if not busy:
                    break
                changed = False
                for e in self.entries(states=("submitted",)):
                    self.poll_one(e)
                    changed = changed or e["state"] == "ended"
                self.save()
                for e in self.entries(states=("ended",)):
                    self.collect(e)
                    changed = True
                live.update(self.table())
                if not changed:
                    durable.sleep(self.poll)
        for j in self.journals.values():
            j.sync()
