"""
LLM backend for the two extraction/explanation seams defined in
engine/extract.py and engine/explain.py — backed by Google's Gemini API
(free tier, no billing required), via the official `google-genai` SDK.

Three fixes over the previous version, all found from real free-tier runs:
  1. PROACTIVE per-minute rate limiting (unchanged from before). Free-tier
     Gemini models cap out as low as 5 requests/minute — reacting to 429s
     after the fact just burns your retry budget instantly. RateLimiter
     below sleeps BEFORE a call if you're about to exceed the configured
     per-minute budget.
  2. DAILY-QUOTA FAST FAIL. RPM and RPD (requests/day) both surface as the
     same 429 RESOURCE_EXHAUSTED error, but only one of them heals itself
     after 60 seconds. If every retry for a single call still 429s, the
     daily quota is almost certainly gone for the rest of today — so we
     latch a module-level flag and skip the network (and the retry
     backoff!) for every subsequent call this process makes, falling
     straight to the mock. This is what turns "the whole remaining batch
     spends 3 retries x several seconds of backoff PER CALL, for hours"
     into "we notice once, and finish the run in seconds."
  3. ON-DISK RESPONSE CACHE. Keyed by a hash of the exact request content
     (system+user text, or image path+event_id), persisted to
     `.llm_cache.json` next to this file. A message, image, or explanation
     request that already got a real answer in a previous run is never
     re-sent — this is what makes it safe to re-run main.py after a daily
     quota reset without burning quota re-answering everything that
     already succeeded before the wall was hit.

Every call also records WHICH of {live, cached, mock} produced its answer
in `LAST_CALL_SOURCE`, read by engine/extract.py and engine/explain.py
immediately after the call so evaluation/usage_report.md can report the
real provider/model mix instead of a hardcoded guess.

Configure via environment variables (no code edits needed to tune):
    GEMINI_API_KEY   - required to use the real API at all
    GEMINI_MODEL     - defaults to a Flash-Lite variant, which gets a
                       meaningfully higher free RPM quota than the newest
                       preview Flash models. Google no longer publishes a
                       single free-tier number per model — check the LIVE
                       number for your account/model at
                       https://aistudio.google.com/rate-limit and set
                       GEMINI_RPM_LIMIT to match (a little under it, to
                       leave headroom for retries).
    GEMINI_RPM_LIMIT - defaults to 5 (a conservative floor). Raise or
                       lower it to match the number shown on the page
                       above for your account + model.
    LLM_CACHE_PATH   - defaults to ".llm_cache.json" next to this file.

Falls back to a deterministic mock (no network, no key needed) when
GEMINI_API_KEY is unset — so `python main.py` / `python api.py` keep
working out of the box for anyone who hasn't set a key yet.
"""
from __future__ import annotations
import os
import json
import time
import hashlib
import threading
from pathlib import Path
from collections import deque

MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
RPM_LIMIT = int(os.environ.get("GEMINI_RPM_LIMIT", "5"))
MAX_RETRIES = 3
BASE_BACKOFF_SECONDS = 5.0  # a real retry backoff, not a fraction of a second —
                             # on a 5 RPM quota, a sub-second retry just wastes the attempt

CACHE_PATH = Path(os.environ.get("LLM_CACHE_PATH", str(Path(__file__).parent / ".llm_cache.json")))

_client = None

# Set by every call_llm/call_vlm invocation to one of "live", "cached", "mock".
# Read by engine/extract.py and engine/explain.py right after the call so the
# usage report can log the real provider/model instead of a hardcoded guess.
LAST_CALL_SOURCE = "mock"

# When True, call_llm/call_vlm serve cache hits but NEVER make a live
# request — they fall straight through to the offline fallback instead.
#
# This exists because api.py preprocesses the whole dataset at startup (215
# messages + 16 images = 231 potential calls). At the default 5 req/min
# free-tier throttle that is ~46 minutes of a deployed server refusing
# traffic before it ever binds. A web process must start fast and
# deterministically; paying live-LLM latency during boot is the wrong place
# for it. Batch runs (main.py) leave this False and do pay that cost, which
# is what populates the cache in the first place.
OFFLINE_ONLY = os.environ.get("LLM_OFFLINE_ONLY", "").lower() in ("1", "true", "yes")


def set_offline_only(value: bool) -> None:
    global OFFLINE_ONLY
    OFFLINE_ONLY = value

# Latched True the first time a call exhausts all its retries still hitting a
# 429 — from then on we assume the DAILY quota is gone (not just the current
# minute's window, which would have healed) and skip the network entirely for
# the rest of this process.
_quota_exhausted = False


class RateLimiter:
    """Sliding 60s window, shared across call_llm/call_vlm since Gemini
    quotas are per-model, not per-function. Blocks (sleeps) the calling
    thread just long enough to stay under RPM_LIMIT — this is what
    actually prevents 429s instead of just retrying after one."""

    def __init__(self, limit_per_minute: int):
        self.limit = max(1, limit_per_minute)
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def wait_for_slot(self):
        with self._lock:
            now = time.monotonic()
            while self._calls and now - self._calls[0] > 60:
                self._calls.popleft()
            if len(self._calls) >= self.limit:
                sleep_for = 60 - (now - self._calls[0]) + 0.1
                if sleep_for > 0:
                    time.sleep(sleep_for)
                now = time.monotonic()
                while self._calls and now - self._calls[0] > 60:
                    self._calls.popleft()
            self._calls.append(time.monotonic())


_limiter = RateLimiter(RPM_LIMIT)


# ---------------------------------------------------------------------------
# On-disk cache: {cache_key: {"raw": str, "in_tok": int, "out_tok": int}}
# ---------------------------------------------------------------------------
_cache_lock = threading.Lock()


def _load_cache() -> dict:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


_cache = _load_cache()


def _save_cache():
    try:
        CACHE_PATH.write_text(json.dumps(_cache))
    except OSError:
        pass  # cache is a speed/quota optimization, never fatal


def _cache_key(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def _is_quota_error(e: Exception) -> bool:
    code = getattr(e, "code", None)
    msg = str(e)
    return code == 429 or "RESOURCE_EXHAUSTED" in msg or "429" in msg


def _strip_code_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        if t.endswith("```"):
            t = t.rsplit("```", 1)[0]
    return t.strip()


def _mock_signal_response() -> tuple[str, int, int]:
    return ('{"event_id": null, "action": "none", "new_amount": null, '
            '"new_date": null, "delay_days": null, "confidence": 0.0}', 0, 0)


def _mock_image_response(event_id: str) -> tuple[str, int, int]:
    """Signals UNRESOLVED (amount 0, confidence 0) rather than inventing a
    number. An earlier version returned amount=1.0 as a schema-satisfying
    placeholder — which meant a receipt whose amount could not be read was
    silently treated as a real 1-unit transaction and fed into the forecast
    as fact. Callers must check confidence and exclude unresolved events
    rather than trusting the amount. Fabricating a financial figure to
    satisfy a validator is strictly worse than admitting the gap."""
    return f'{{"event_id": "{event_id}", "amount": 0.0, "confidence": 0.0}}', 0, 0


def _usage(resp) -> tuple[int, int]:
    meta = getattr(resp, "usage_metadata", None)
    if meta is None:
        return 0, 0
    return getattr(meta, "prompt_token_count", 0) or 0, getattr(meta, "candidates_token_count", 0) or 0


def call_llm(system: str, user: str) -> tuple[str, int, int]:
    """Text-only call for message extraction and explanation generation.
    Returns (raw_text, input_tokens, output_tokens). Never raises —
    degrades to the mock response after MAX_RETRIES failures. Sets
    LAST_CALL_SOURCE to "live" / "cached" / "mock"."""
    global _quota_exhausted, LAST_CALL_SOURCE

    key = _cache_key("llm", system, user)
    with _cache_lock:
        hit = _cache.get(key)
    if hit is not None:
        LAST_CALL_SOURCE = "cached"
        return hit["raw"], hit["in_tok"], hit["out_tok"]

    if "GEMINI_API_KEY" not in os.environ or _quota_exhausted or OFFLINE_ONLY:
        LAST_CALL_SOURCE = "mock"
        return _mock_signal_response()

    last_err = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            from google import genai
            from google.genai import types
            global _client
            if _client is None:
                _client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
            config = types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
            )
            _limiter.wait_for_slot()  # proactive throttle — happens BEFORE the network call
            resp = _client.models.generate_content(model=MODEL, contents=user, config=config)
            in_tok, out_tok = _usage(resp)
            text = _strip_code_fence(resp.text)
            with _cache_lock:
                _cache[key] = {"raw": text, "in_tok": in_tok, "out_tok": out_tok}
                _save_cache()
            LAST_CALL_SOURCE = "live"
            return text, in_tok, out_tok
        except Exception as e:  # covers client init, config build, the network call, everything
            last_err = e
            if _is_quota_error(e) and attempt == MAX_RETRIES:
                # Every retry still 429'd — a per-MINUTE limit would have
                # healed after the backoff sleeps above, so this is almost
                # certainly the DAILY quota. Stop hammering it for the rest
                # of the run instead of retrying every remaining call.
                _quota_exhausted = True
                print("[llm_client] quota appears exhausted for today (daily RPD, not per-minute "
                      "RPM) — falling back to mock for all remaining calls this run. Re-run after "
                      "the quota resets (midnight Pacific) and the on-disk cache will skip every "
                      "call that already succeeded.")
            else:
                time.sleep(BASE_BACKOFF_SECONDS * (attempt + 1))
    print(f"[llm_client] call_llm failed after retries: {last_err}")
    LAST_CALL_SOURCE = "mock"
    return _mock_signal_response()


def call_vlm(system: str, image_path: str, event_id: str) -> tuple[str, int, int]:
    """Vision call for image amount extraction. Returns
    (raw_text, input_tokens, output_tokens). Never raises. Sets
    LAST_CALL_SOURCE to "live" / "cached" / "mock"."""
    global _quota_exhausted, LAST_CALL_SOURCE

    key = _cache_key("vlm", image_path, event_id)
    with _cache_lock:
        hit = _cache.get(key)
    if hit is not None:
        LAST_CALL_SOURCE = "cached"
        return hit["raw"], hit["in_tok"], hit["out_tok"]

    if "GEMINI_API_KEY" not in os.environ or _quota_exhausted or OFFLINE_ONLY:
        LAST_CALL_SOURCE = "mock"
        return _mock_image_response(event_id)

    last_err = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            from google import genai
            from google.genai import types
            global _client
            if _client is None:
                _client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
            with open(image_path, "rb") as f:
                image_bytes = f.read()
            config = types.GenerateContentConfig(system_instruction=system, response_mime_type="application/json")
            _limiter.wait_for_slot()
            resp = _client.models.generate_content(
                model=MODEL,
                contents=[
                    types.Part.from_bytes(data=image_bytes, mime_type="image/png"),
                    f"event_id: {event_id}",
                ],
                config=config,
            )
            in_tok, out_tok = _usage(resp)
            text = _strip_code_fence(resp.text)
            with _cache_lock:
                _cache[key] = {"raw": text, "in_tok": in_tok, "out_tok": out_tok}
                _save_cache()
            LAST_CALL_SOURCE = "live"
            return text, in_tok, out_tok
        except Exception as e:
            last_err = e
            if _is_quota_error(e) and attempt == MAX_RETRIES:
                _quota_exhausted = True
                print("[llm_client] quota appears exhausted for today (daily RPD, not per-minute "
                      "RPM) — falling back to mock for all remaining calls this run. Re-run after "
                      "the quota resets (midnight Pacific) and the on-disk cache will skip every "
                      "call that already succeeded.")
            else:
                time.sleep(BASE_BACKOFF_SECONDS * (attempt + 1))
    print(f"[llm_client] call_vlm failed after retries: {last_err}")
    LAST_CALL_SOURCE = "mock"
    return _mock_image_response(event_id)
