import json

from pydantic import ValidationError

from .fake_llm import FakeTransientError


def is_retryable(e):
    if isinstance(e, FakeTransientError):
        return True
    import anthropic

    if isinstance(e, (anthropic.APIConnectionError, anthropic.RateLimitError)):
        return True
    if isinstance(e, anthropic.APIStatusError):
        return e.status_code >= 500 or e.status_code in (408, 409, 429)
    return isinstance(e, (ConnectionError, TimeoutError))


def usage_of(msg):
    u = getattr(msg, "usage", None)
    if u is None:
        return {}
    return {
        "in": getattr(u, "input_tokens", 0) or 0,
        "out": getattr(u, "output_tokens", 0) or 0,
        "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0,
        "cache_write": getattr(u, "cache_creation_input_tokens", 0) or 0,
    }


def parse_structured(msg, model):
    if msg.stop_reason == "refusal":
        details = getattr(msg, "stop_details", None)
        return "refused", {"category": getattr(details, "category", None)}
    if msg.stop_reason == "max_tokens":
        return "failed", {"error": "max_tokens"}
    text = next((b.text for b in msg.content if getattr(b, "type", None) == "text"), None)
    if text is None:
        return "failed", {"error": f"no text block (stop_reason={msg.stop_reason})"}
    try:
        return "ok", model.model_validate(json.loads(text)).model_dump()
    except (json.JSONDecodeError, ValidationError) as e:
        return "failed", {"error": f"unparseable: {type(e).__name__}: {str(e)[:200]}"}


def usd(tokens_by_mode, cfg):
    pin = cfg["price_in_per_mtok"] / 1e6
    pout = cfg["price_out_per_mtok"] / 1e6
    total = 0.0
    for mode, t in tokens_by_mode.items():
        factor = cfg["batch_discount"] if mode == "batch" else 1.0
        cost = t.get("in", 0) * pin + t.get("out", 0) * pout + t.get("cache_read", 0) * pin * 0.1 + t.get("cache_write", 0) * pin * 1.25
        total += cost * factor
    return total


def merge_tokens(*maps):
    out = {}
    for m in maps:
        for mode, t in m.items():
            o = out.setdefault(mode, {})
            for k, v in t.items():
                o[k] = o.get(k, 0) + v
    return out
