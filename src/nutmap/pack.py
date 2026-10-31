import logging
import os

from . import durable

log = logging.getLogger("nutmap")

PACKED = [
    "raw/**/*.txt",
    "raw/**/*.xml",
    "inventory/emoji.jsonl",
    "inventory/aliases.json",
    "pilot/*.journal.jsonl",
    "panel/*.journal.jsonl",
    "explain/*.jsonl",
    "map/umap.json",
    "flow/flow.jsonl",
]


def working_copies(ctx):
    return sorted({p for pattern in PACKED for p in ctx.data.glob(pattern) if p.is_file()})


def unpacked(ctx):
    changed = []
    for plain in working_copies(ctx):
        packed = durable.packed_path(plain)
        if not packed.exists() or plain.stat().st_mtime_ns > packed.stat().st_mtime_ns:
            changed.append(plain)
    return changed


def pack(ctx):
    written = 0
    for plain in unpacked(ctx):
        durable.check_stop()
        data = plain.read_bytes()
        mtime = plain.stat().st_mtime_ns
        packed = durable.packed_path(plain)
        if packed.exists() and durable.decompress(packed.read_bytes()) == data:
            os.utime(packed, ns=(mtime, mtime))
            continue
        durable.atomic_write_packed(plain, data, mtime)
        written += 1
    if written:
        log.info("pack: %d tracked files packed", written)
    return written
