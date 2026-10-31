import hashlib
import json
import sys
from pathlib import Path

DETERMINISTIC = [
    "inventory/emoji.jsonl",
    "inventory/aliases.json",
    "img/index.json",
    "panel/panel.jsonl",
    "emb/text.npy",
    "emb/text_mask.npy",
    "emb/cldr.npy",
    "emb/image.npy",
    "emb/image_mask.npy",
    "graph/smix.npy",
    "graph/edges.jsonl",
    "race/dist.npy",
    "race/pred.npy",
    "race/root.jsonl",
    "explain/edge_notes.jsonl",
    "map/umap.json",
    "viewpoints/sweeps.json",
    "viewpoints/sweeps.bin.zst",
    "out/nutmap.json",
    "flow/flow.jsonl",
    "explain/step_notes.jsonl",
    "explain/step_notes.jsonl.zst",
    "inventory/emoji.jsonl.zst",
    "web/public/meta.json",
    "web/public/nodes.json",
    "web/public/relations.json",
    "web/public/tree/1f330.bin",
    "web/public/views/race.bin",
    "web/public/views/literal.bin",
    "release/hub/README.md",
    "release/hub/data/descriptions.parquet",
    "release/hub/data/races.parquet",
    "release/hub/data/maps.parquet",
]


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def journal_problems(path):
    finals = {}
    problems = []
    data = path.read_bytes()
    if data and not data.endswith(b"\n"):
        problems.append(f"{path}: torn tail left behind")
    for line in data.split(b"\n"):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            problems.append(f"{path}: unparseable line {line[:60]!r}")
            continue
        if rec["status"] in ("ok", "refused", "dead"):
            finals[rec["key"]] = finals.get(rec["key"], 0) + 1
    problems += [f"{path}: {k} has {n} final records" for k, n in finals.items() if n > 1]
    return problems


def server_problems(data):
    problems = []
    server = {p.stem for p in (data / "fake_server" / "batches").glob("*.json")}
    known = set()
    for state in (data / "panel" / "batches.json", data / "explain" / "batches.json"):
        if state.exists():
            for e in json.loads(state.read_text())["entries"]:
                if e.get("id"):
                    if e["id"] in known:
                        problems.append(f"{state}: batch {e['id']} referenced twice")
                    known.add(e["id"])
    for b in sorted(server - known):
        problems.append(f"server batch {b} is not tracked in any state file (double submission)")
    return problems


def main(clean, chaos):
    clean, chaos = Path(clean), Path(chaos)
    problems = []
    for rel in DETERMINISTIC:
        a, b = clean / rel, chaos / rel
        if not a.exists() or not b.exists():
            problems.append(f"{rel}: missing in {'clean' if not a.exists() else 'chaos'} run")
        elif sha(a) != sha(b):
            problems.append(f"{rel}: differs between clean and chaos runs")
    for d in (clean, chaos):
        for j in d.rglob("*.journal.jsonl"):
            problems += journal_problems(j)
        problems += server_problems(d)
    imgs_a = {p.relative_to(clean): sha(p) for p in (clean / "img").rglob("*.png")}
    imgs_b = {p.relative_to(chaos): sha(p) for p in (chaos / "img").rglob("*.png")}
    if imgs_a != imgs_b:
        problems.append(f"images differ: {len(set(imgs_a.items()) ^ set(imgs_b.items()))} mismatches")
    summary = json.loads((chaos / "panel" / "summary.json").read_text())
    print(f"chaos panel summary: {summary}")
    print(f"compared {len(DETERMINISTIC)} artifacts and {len(imgs_a)} images")
    for p in problems:
        print("PROBLEM:", p)
    print("CHAOS CHECK", "FAILED" if problems else "PASSED")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:3]))
