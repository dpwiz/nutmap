import logging
import re
import socket
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter

from . import durable
from .context import PipelineError, Stage, register

log = logging.getLogger("nutmap")

VS16 = 0xFE0F
ZWJ = 0x200D
SKIN = set(range(0x1F3FB, 0x1F400))
HAIR = {0x1F9B0, 0x1F9B1, 0x1F9B2, 0x1F9B3}
GENDER_SIGNS = {0x2640, 0x2642}
TO_NEUTRAL = {0x1F468: 0x1F9D1, 0x1F469: 0x1F9D1, 0x1F466: 0x1F9D2, 0x1F467: 0x1F9D2}
KISS_MARK = 0x1F48B
HEART = 0x2764
KISS = (0x1F48F,)
COUPLE_WITH_HEART = (0x1F491,)
TAGS = set(range(0xE0020, 0xE0080))

RAW_FILES = {
    "raw/unicode/emoji-test.txt": "emoji_test_url",
    "raw/cldr/annotations-en.xml": "cldr_annotations_url",
    "raw/cldr/annotationsDerived-en.xml": "cldr_derived_url",
}


def key_of(cps):
    return tuple(cp for cp in cps if cp != VS16)


def key_of_str(s):
    return key_of(ord(c) for c in s)


def id_of(key):
    return "_".join(f"{cp:x}" for cp in key)


def str_of(key):
    return "".join(chr(cp) for cp in key)


def split_zwj(key):
    parts, cur = [], []
    for cp in key:
        if cp == ZWJ:
            parts.append(tuple(cur))
            cur = []
        else:
            cur.append(cp)
    parts.append(tuple(cur))
    return parts


def join_zwj(parts):
    out = []
    for i, p in enumerate(parts):
        if i:
            out.append(ZWJ)
        out.extend(p)
    return tuple(out)


def neutral_of(key, known):
    if ZWJ not in key or not (set(key) & (GENDER_SIGNS | TO_NEUTRAL.keys())):
        return None
    parts = [p for p in split_zwj(key) if not (len(p) == 1 and p[0] in GENDER_SIGNS)]
    parts = [tuple(TO_NEUTRAL.get(cp, cp) for cp in p) for p in parts]
    n = join_zwj(parts)
    if n != key and n in known:
        return n
    if KISS_MARK in key:
        return KISS
    if HEART in key:
        return COUPLE_WITH_HEART
    return None


@register
class Fetch(Stage):
    name = "fetch"
    dir = "raw"

    def config_slice(self, ctx):
        return {k: ctx.cfg["sources"][v] for k, v in RAW_FILES.items()}

    def outputs(self, ctx):
        return list(RAW_FILES)

    def run(self, ctx):
        lock_path = ctx.path("raw", "sources.lock")
        lock = durable.read_json(lock_path, {})
        for rel, cfg_key in RAW_FILES.items():
            durable.check_stop()
            url = ctx.cfg["sources"][cfg_key]
            target = ctx.path(rel)
            entry = lock.get(rel)
            if entry and entry["url"] != url:
                log.warning("fetch: %s url changed, re-pinning", rel)
                entry = None
            if durable.exists(target) and entry and durable.sha256_file(target) == entry["sha256"]:
                continue
            data = durable.with_retries(
                lambda: _download(url),
                retryable=(urllib.error.URLError, socket.timeout, ConnectionError, TimeoutError),
                what=f"download {url}",
            )
            sha = durable.sha256_bytes(data)
            if entry and entry["sha256"] != sha:
                raise PipelineError(f"fetch: {rel} sha256 {sha} does not match sources.lock {entry['sha256']}")
            durable.atomic_write_bytes(target, data)
            lock[rel] = {"url": url, "sha256": sha, "bytes": len(data)}
            durable.atomic_write_json(lock_path, lock)
            log.info("fetch: %s (%d bytes)", rel, len(data))


def _download(url):
    req = urllib.request.Request(url, headers={"User-Agent": "nutmap/0.1"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


LINE_RE = re.compile(r"^\s*(\S+)\s+E(\d+\.\d+)\s+(.*)$")


def parse_emoji_test(text):
    group = subgroup = None
    rows = []
    for line in text.splitlines():
        if line.startswith("# group:"):
            group = line.split(":", 1)[1].strip()
            continue
        if line.startswith("# subgroup:"):
            subgroup = line.split(":", 1)[1].strip()
            continue
        if not line.strip() or line.startswith("#"):
            continue
        left, _, comment = line.partition("#")
        cps_s, _, status = left.partition(";")
        m = LINE_RE.match(comment)
        if not m:
            raise PipelineError(f"inventory: cannot parse emoji-test line: {line}")
        rows.append(
            {
                "cps": [int(x, 16) for x in cps_s.split()],
                "status": status.strip(),
                "group": group,
                "subgroup": subgroup,
                "char": m.group(1),
                "version": m.group(2),
                "name": m.group(3).strip(),
            }
        )
    return rows


def load_cldr(paths):
    names, keywords = {}, {}
    for p in paths:
        for a in ET.fromstring(durable.read_bytes(p)).iter("annotation"):
            k = key_of_str(a.get("cp"))
            text = (a.text or "").strip()
            if a.get("type") == "tts":
                names.setdefault(k, text)
            else:
                keywords.setdefault(k, [w.strip() for w in text.split("|") if w.strip()])
    return names, keywords


def build_inventory(rows, cldr_keywords):
    fq = [r for r in rows if r["status"] == "fully-qualified"]
    hair = [r for r in rows if r["status"] == "component" and set(r["cps"]) & HAIR]
    known = {key_of(r["cps"]) for r in fq}
    stats = Counter()
    kept = []
    for r in rows:
        if r["status"] == "component":
            if r in hair:
                kept.append(r)
                stats["hair_component"] += 1
            continue
        if r["status"] != "fully-qualified":
            continue
        k = key_of(r["cps"])
        stats["fully_qualified"] += 1
        if set(k) & SKIN:
            stats["dropped_skin_tone"] += 1
            continue
        if neutral_of(k, known) is not None:
            stats["dropped_gendered"] += 1
            continue
        kept.append(r)
    entry_keys = {key_of(r["cps"]) for r in kept}
    entries = []
    for r in kept:
        k = key_of(r["cps"])
        parts = []
        if ZWJ in k:
            for p in split_zwj(k):
                if p in entry_keys and p != k and id_of(p) not in parts:
                    parts.append(id_of(p))
        entries.append(
            {
                "id": id_of(k),
                "char": r["char"],
                "key": str_of(k),
                "name": r["name"],
                "keywords": cldr_keywords.get(k, []),
                "group": r["group"],
                "subgroup": r["subgroup"],
                "version": r["version"],
                "parts": parts,
            }
        )
    aliases = {}
    for r in rows:
        k = key_of(r["cps"])
        base = tuple(cp for cp in k if cp not in SKIN)
        target = base if base in entry_keys else neutral_of(base, known)
        if target in entry_keys:
            aliases[str_of(k)] = id_of(target)
    for e in entries:
        aliases[e["key"]] = e["id"]
    stats["kept"] = len(entries)
    stats["flags"] = sum(1 for e in entries if e["group"] == "Flags")
    stats["zwj_sequences"] = sum(1 for e in entries if chr(ZWJ) in e["key"])
    stats["with_parts"] = sum(1 for e in entries if e["parts"])
    stats["with_cldr_keywords"] = sum(1 for e in entries if e["keywords"])
    return entries, aliases, stats


def subset(entries, limit, must_chars):
    if not limit or limit >= len(entries):
        return entries
    must = {key_of_str(c) for c in must_chars}
    chosen = [e for e in entries if key_of_str(e["key"]) in must]
    rest = [e for e in entries if key_of_str(e["key"]) not in must]
    need = max(0, limit - len(chosen))
    step = len(rest) / need if need else 1
    picked = [rest[int(i * step)] for i in range(need)]
    ids = {e["id"] for e in chosen + picked}
    out = [e for e in entries if e["id"] in ids]
    for e in out:
        e["parts"] = [p for p in e["parts"] if p in ids]
    return out


ESCAPE_RE = re.compile(r"\\U([0-9a-fA-F]{8})|\\u([0-9a-fA-F]{4})")


def unescape(text):
    if "\\" not in text:
        return text
    s = ESCAPE_RE.sub(lambda m: chr(int(m.group(1) or m.group(2), 16)), text)
    try:
        return s.encode("utf-16", "surrogatepass").decode("utf-16")
    except UnicodeDecodeError:
        return s


class Resolver:
    def __init__(self, aliases, ids):
        self.aliases = aliases
        self.ids = set(ids)

    def __call__(self, text):
        if not text:
            return None
        text = unescape(text).strip()
        for cand in (text, text.split()[0] if text else ""):
            k = key_of_str(cand)
            base = tuple(cp for cp in k if cp not in SKIN)
            for probe in (k, base):
                i = self.aliases.get(str_of(probe))
                if i in self.ids:
                    return i
        return None


def load_entries(ctx):
    return durable.read_jsonl(ctx.path("inventory", "emoji.jsonl"))


def load_resolver(ctx, entries=None):
    entries = entries or load_entries(ctx)
    return Resolver(durable.read_json(ctx.path("inventory", "aliases.json")), [e["id"] for e in entries])


@register
class Inventory(Stage):
    name = "inventory"
    dir = "inventory"
    needs = ("fetch",)

    def config_slice(self, ctx):
        return {
            "limit": ctx.cfg["subset"]["limit"],
            "root": ctx.cfg["root"],
            "probes": ctx.cfg["panel"]["pilot_probes"] + ctx.cfg["race"]["probes"],
            "version": 1,
        }

    def outputs(self, ctx):
        return ["inventory/emoji.jsonl", "inventory/aliases.json", "inventory/summary.json"]

    def run(self, ctx):
        rows = parse_emoji_test(durable.read_text(ctx.path("raw/unicode/emoji-test.txt")))
        _, keywords = load_cldr([ctx.path("raw/cldr/annotations-en.xml"), ctx.path("raw/cldr/annotationsDerived-en.xml")])
        entries, aliases, stats = build_inventory(rows, keywords)
        root_id = aliases.get(str_of(key_of_str(ctx.cfg["root"])))
        if root_id is None:
            raise PipelineError(f"inventory: root {ctx.cfg['root']} not in emoji-test.txt")
        must = [ctx.cfg["root"]] + ctx.cfg["panel"]["pilot_probes"] + ctx.cfg["race"]["probes"]
        entries = subset(entries, ctx.cfg["subset"]["limit"], must)
        for i, e in enumerate(entries):
            e["index"] = i
        stats["final"] = len(entries)
        durable.atomic_write_jsonl(ctx.path("inventory", "emoji.jsonl"), entries)
        durable.atomic_write_json(ctx.path("inventory", "aliases.json"), aliases, indent=None)
        durable.atomic_write_json(ctx.path("inventory", "summary.json"), dict(stats))
        log.info("inventory: %s", dict(stats))

    def describe(self, ctx):
        s = durable.read_json(ctx.path("inventory", "summary.json"))
        return f"{s['final']} emoji ({s['flags']} flags, {s['zwj_sequences']} ZWJ)" if s else ""
