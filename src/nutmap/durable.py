import hashlib
import io
import json
import logging
import os
import random
import signal
import threading
from pathlib import Path

import numpy as np
import zstandard

log = logging.getLogger("nutmap")


class Interrupted(Exception):
    pass


class ChaosError(BaseException):
    pass


_stop = threading.Event()
_signals_seen = 0


def _on_signal(signum, frame):
    global _signals_seen
    _signals_seen += 1
    if _signals_seen >= 2:
        os.write(2, b"\nnutmap: second interrupt, exiting immediately\n")
        os._exit(130)
    _stop.set()
    os.write(2, b"\nnutmap: stopping after in-flight work (Ctrl+C again to exit now)\n")


def install_signal_handlers():
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)


def stopping():
    return _stop.is_set()


def check_stop():
    if _stop.is_set():
        raise Interrupted()


def sleep(seconds):
    if _stop.wait(seconds):
        raise Interrupted()


_chaos_p = float(os.environ.get("NUTMAP_CHAOS") or 0)
_chaos_rng = random.Random(int.from_bytes(os.urandom(8), "little"))


def chaos(point):
    if _chaos_p and _chaos_rng.random() < _chaos_p:
        raise ChaosError(f"chaos at {point}")


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    chaos(f"replace {path.name}")
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def atomic_write_text(path, text):
    atomic_write_bytes(path, text.encode("utf-8"))


def dumps(obj, indent=None):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=indent)


def atomic_write_json(path, obj, indent=1):
    atomic_write_text(path, dumps(obj, indent=indent) + "\n")


def atomic_write_jsonl(path, rows):
    atomic_write_text(path, "".join(dumps(r) + "\n" for r in rows))


def packed_path(path):
    path = Path(path)
    return path.with_name(path.name + ".zst")


def compress(data):
    return zstandard.ZstdCompressor(level=19, write_checksum=True, write_content_size=True).compress(data)


def decompress(data):
    return zstandard.ZstdDecompressor().decompress(data)


def atomic_write_packed(path, data, mtime_ns=None):
    packed = packed_path(path)
    atomic_write_bytes(packed, compress(data))
    if mtime_ns is not None:
        os.utime(packed, ns=(mtime_ns, mtime_ns))


def newest_source(path):
    plain, packed = Path(path), packed_path(path)
    if plain.exists() and (not packed.exists() or plain.stat().st_mtime_ns >= packed.stat().st_mtime_ns):
        return plain
    return packed if packed.exists() else None


def exists(path):
    return newest_source(path) is not None


def read_bytes(path):
    source = newest_source(path)
    if source is None:
        raise FileNotFoundError(path)
    data = source.read_bytes()
    return decompress(data) if source != Path(path) else data


def read_text(path):
    return read_bytes(path).decode("utf-8")


def atomic_save_npy(path, array):
    buf = io.BytesIO()
    np.save(buf, array, allow_pickle=False)
    atomic_write_bytes(path, buf.getvalue())


def read_json(path, default=None):
    if not exists(path):
        return default
    return json.loads(read_text(path))


def read_jsonl(path):
    return [json.loads(line) for line in read_text(path).split("\n") if line.strip()]


def sweep_tmp(root):
    root = Path(root)
    if not root.exists():
        return
    for p in root.rglob(".*.tmp"):
        p.unlink(missing_ok=True)
    for p in root.rglob("*.part"):
        p.unlink(missing_ok=True)


FINAL = ("ok", "refused", "dead")


class Journal:
    def __init__(self, path, readonly=False):
        self.path = Path(path)
        self.readonly = readonly
        self._lock = threading.Lock()
        self.final = {}
        self.failures = {}
        self.failed_in = set()
        self.errors = {}
        self.tokens = {}
        self._fh = None
        self._load()
        if not readonly:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if newest_source(self.path) not in (None, self.path):
                if self.path.exists():
                    log.warning("journal %s: replacing the working copy with its newer packed copy", self.path)
                atomic_write_bytes(self.path, read_bytes(self.path))
            self._fh = open(self.path, "ab")

    def _load(self):
        if not exists(self.path):
            return
        data = read_bytes(self.path)
        good_end = 0
        pos = 0
        while pos < len(data):
            nl = data.find(b"\n", pos)
            if nl == -1:
                break
            line = data[pos:nl]
            pos = nl + 1
            if not line.strip():
                good_end = pos
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            self._absorb(rec)
            good_end = pos
        if good_end < len(data) and not self.readonly and newest_source(self.path) == self.path:
            log.warning("journal %s: dropping %d torn trailing bytes", self.path, len(data) - good_end)
            with open(self.path, "r+b") as f:
                f.truncate(good_end)
                f.flush()
                os.fsync(f.fileno())

    def _absorb(self, rec):
        usage = rec.get("usage")
        if usage:
            t = self.tokens.setdefault(rec.get("mode", "sync"), {})
            for k, v in usage.items():
                t[k] = t.get(k, 0) + (v or 0)
        key = rec["key"]
        if key in self.final:
            return
        if rec["status"] in FINAL:
            self.final[key] = rec
            return
        batch = rec.get("batch")
        if batch is not None:
            if (key, batch) in self.failed_in:
                return
            self.failed_in.add((key, batch))
        self.failures[key] = self.failures.get(key, 0) + 1
        reason = str(rec.get("error", "unknown"))[:80]
        self.errors[reason] = self.errors.get(reason, 0) + 1

    def append(self, rec, sync=True):
        line = (dumps(rec) + "\n").encode("utf-8")
        with self._lock:
            if rec["key"] in self.final:
                return False
            chaos(f"journal {self.path.name}")
            self._fh.write(line)
            self._fh.flush()
            if sync:
                os.fsync(self._fh.fileno())
            self._absorb(rec)
            return True

    def sync(self):
        with self._lock:
            self._fh.flush()
            os.fsync(self._fh.fileno())

    def done(self, key):
        return key in self.final

    def ok(self):
        return {k: r for k, r in self.final.items() if r["status"] == "ok"}

    def close(self):
        if self._fh is None:
            return
        with self._lock:
            self._fh.flush()
            os.fsync(self._fh.fileno())
            self._fh.close()
            self._fh = None


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    return sha256_bytes(read_bytes(path))


def digest(obj):
    return sha256_bytes(dumps(obj).encode("utf-8"))


def with_retries(fn, *, retryable, attempts=6, base=2.0, cap=60.0, what="call"):
    is_retryable = retryable if callable(retryable) and not isinstance(retryable, type) else (lambda e: isinstance(e, retryable))
    for i in range(attempts):
        check_stop()
        try:
            return fn()
        except Exception as e:
            if not is_retryable(e) or i == attempts - 1:
                raise
            delay = min(cap, base * 2**i) * (0.5 + random.random())
            log.warning("%s failed (%s: %s), retrying in %.1fs", what, type(e).__name__, e, delay)
            sleep(delay)
