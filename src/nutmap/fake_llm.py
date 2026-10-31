import datetime as dt
import hashlib
import json
import random
import re
import time
from pathlib import Path
from types import SimpleNamespace

from . import durable

POOL = ["🌰", "🐿️", "🥜", "🍂", "🚀", "🌍", "😂", "🧑‍🌾", "🌾", "🐻", "❄️", "🍕", "🎃", "🌳", "🔥", "💧", "⭐", "🇯🇵", "🏳️‍🌈", "🐻‍❄️", "🦊", "☕", "not-an-emoji"]
_rng = random.Random()


class FakeTransientError(Exception):
    pass


class FakeBadRequest(Exception):
    pass


CUSTOM_ID = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def _h(*parts):
    return int(hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:12], 16)


def _seed_text(params):
    return params.get("system", "") + "\n" + params["messages"][0]["content"]


def _reply(params):
    seed = _h(_seed_text(params))
    props = params["output_config"]["format"]["schema"]["properties"]
    if set(props) == {"relation"}:
        return {"relation": f"fake connection {seed % 997}"}
    n = 3 + seed % 4
    links = [{"emoji": POOL[(seed >> (4 * i)) % len(POOL)], "relation": f"fake relation {i}"} for i in range(n)]
    return {
        "description": f"Fake description {seed % 100003}. " + params["messages"][0]["content"].splitlines()[0],
        "keywords": [f"kw{(seed >> i) % 50}" for i in range(4)],
        "links": links,
    }


def _message(params):
    seed = _h(_seed_text(params))
    usage = SimpleNamespace(
        input_tokens=len(_seed_text(params)) // 4,
        output_tokens=0,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    if seed % 97 == 0:
        return SimpleNamespace(id=f"msg_{seed:x}", content=[], stop_reason="refusal", stop_details=SimpleNamespace(category="general_harms"), usage=usage)
    text = json.dumps(_reply(params), ensure_ascii=False)
    usage.output_tokens = len(text) // 4
    return SimpleNamespace(
        id=f"msg_{seed:x}",
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason="end_turn",
        stop_details=None,
        usage=usage,
    )


class _Messages:
    def __init__(self, server):
        self.batches = _Batches(server)

    def create(self, **params):
        time.sleep(_rng.uniform(0.005, 0.03))
        if _rng.random() < 0.05:
            raise FakeTransientError("fake connection reset")
        return _message(params)


class _Batches:
    def __init__(self, server):
        self.dir = Path(server) / "batches"
        self.dir.mkdir(parents=True, exist_ok=True)

    def _load(self, batch_id):
        return json.loads((self.dir / f"{batch_id}.json").read_text("utf-8"))

    def _all(self):
        return [json.loads(p.read_text("utf-8")) for p in self.dir.glob("*.json")]

    def create(self, requests):
        requests = list(requests)
        ids = [r["custom_id"] for r in requests]
        if not 0 < len(ids) <= 100_000:
            raise FakeBadRequest(f"batch must hold 1 to 100000 requests, got {len(ids)}")
        bad = [i for i in ids if not CUSTOM_ID.match(i)]
        if bad:
            raise FakeBadRequest(f"requests.{ids.index(bad[0])}.custom_id: String should match pattern '^[a-zA-Z0-9_-]{{1,64}}$'")
        if len(set(ids)) != len(ids):
            raise FakeBadRequest("custom_id values must be unique within a batch")
        seen = {}
        for b in self._all():
            for r in b["requests"]:
                seen[r["custom_id"]] = seen.get(r["custom_id"], 0) + 1
        fails = [r["custom_id"] for r in requests if seen.get(r["custom_id"], 0) == 0 and _h(r["custom_id"]) % 10 == 0]
        batch_id = f"msgbatch_fake_{_rng.getrandbits(48):012x}"
        created = time.time()
        rec = {"id": batch_id, "created_at": created, "requests": list(requests), "fails": fails, "duration": 0.5 + 0.002 * len(requests)}
        durable.atomic_write_text(self.dir / f"{batch_id}.json", json.dumps(rec, ensure_ascii=False))
        if _rng.random() < 0.1:
            raise FakeTransientError("fake response lost after batch creation")
        return self._view(rec)

    def _view(self, rec):
        n = len(rec["requests"])
        elapsed = time.time() - rec["created_at"]
        ended = elapsed >= rec["duration"]
        nfail = len(rec["fails"])
        if ended:
            counts = SimpleNamespace(processing=0, succeeded=n - nfail, errored=nfail, canceled=0, expired=0)
        else:
            done = int(n * elapsed / rec["duration"])
            counts = SimpleNamespace(processing=n - done, succeeded=done, errored=0, canceled=0, expired=0)
        return SimpleNamespace(
            id=rec["id"],
            processing_status="ended" if ended else "in_progress",
            request_counts=counts,
            created_at=dt.datetime.fromtimestamp(rec["created_at"], dt.timezone.utc),
        )

    def retrieve(self, batch_id):
        if _rng.random() < 0.05:
            raise FakeTransientError("fake retrieve timeout")
        return self._view(self._load(batch_id))

    def list(self, limit=20):
        return sorted((self._view(b) for b in self._all()), key=lambda v: v.created_at, reverse=True)

    def results(self, batch_id):
        rec = self._load(batch_id)
        if time.time() - rec["created_at"] < rec["duration"]:
            raise RuntimeError("fake batch not ended")
        reqs = list(rec["requests"])
        random.Random(batch_id).shuffle(reqs)
        fails = set(rec["fails"])
        for r in reqs:
            if r["custom_id"] in fails:
                yield SimpleNamespace(custom_id=r["custom_id"], result=SimpleNamespace(type="errored", error=SimpleNamespace(type="api_error", message="fake")))
            else:
                yield SimpleNamespace(custom_id=r["custom_id"], result=SimpleNamespace(type="succeeded", message=_message(r["params"])))


class FakeAnthropic:
    def __init__(self, server_dir):
        self.messages = _Messages(server_dir)
