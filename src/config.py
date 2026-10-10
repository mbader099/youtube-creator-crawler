"""
Central config + shared HTTP client.

The ONE place for API keys, endpoints, models, rate limits, and a hardened HTTP
helper. Every script imports from here so a fix (like the Groq User-Agent) is
made once and inherited everywhere.

    from config import groq_chat, GROQ_MODEL
    text = groq_chat([{"role": "user", "content": "..."}])

Lessons baked in (so they never bite again):
  - Cloudflare-fronted APIs (Groq) 403 the default Python User-Agent -> send a
    real browser UA on every request.
  - Free tiers rate-limit -> retry on HTTP 429, honoring retry-after.
  - Never let an API failure be silent -> callers should surface it.
"""

import envfile  # noqa: F401  — loads .env before any os.environ reads
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

# A normal browser UA — required for Cloudflare-fronted APIs, harmless elsewhere.
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36")


def _read_key(*env_names, file_name=None):
    """First matching env var, else a local key file, else ''."""
    for n in env_names:
        v = os.environ.get(n)
        if v:
            return v.strip()
    if file_name:
        p = os.path.join(HERE, file_name)
        if os.path.exists(p):
            try:
                return open(p).read().strip()
            except Exception:
                pass
    return ""


# ── API REGISTRY ───────────────────────────────────────────────────────────
# key = human name; store creds source, endpoint, model, and rate limits so
# they're never rediscovered by hitting 429s.
APIS = {
    "youtube": {
        "key": _read_key("YOUTUBE_API_KEY"),        # env only
        "base": "https://www.googleapis.com/youtube/v3/",
        "rate": "~10,000 units/day (search=100 units each)",
    },
    "groq": {
        "key": _read_key("GROQ_API_KEY", "GROQ_KEY", "GROQ_API_TOKEN",
                         "GROQ_TOKEN", file_name=".groq_key"),
        "base": "https://api.groq.com/openai/v1/chat/completions",
        "model": "llama-3.1-8b-instant",
        "rate": "free tier ~30 req/min; pace ~2s, retry on 429",
    },
    "gemini": {
        "key": _read_key("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        "base": "https://generativelanguage.googleapis.com/v1beta/models/",
        "model": "gemini-2.0-flash",
        "rate": "free tier ~15 req/min, ~1500/day",
    },
    # ── Email verifiers (GET APIs; used as a cascade by email_verifier.py) ──
    # Primary → fallback → fallback. Local MX/syntax pre-filter runs first so
    # these credits are only spent on emails that already passed cheap checks.
    "reoon": {
        "key": _read_key("REOON_API_KEY", "REOON_KEY"),
        "base": "https://emailverifier.reoon.com/api/v1/verify",
        "rate": "free: 600/mo + ~20 renewing daily credits, full API on free tier",
    },
    "hunter": {
        "key": _read_key("HUNTER_API_KEY", "HUNTER_KEY"),
        "base": "https://api.hunter.io/v2/email-verifier",
        "rate": "free: 50 req/mo (finder + verifier share the pool)",
    },
    "zerobounce": {
        "key": _read_key("ZEROBOUNCE_API_KEY", "ZEROBOUNCE_KEY"),
        "base": "https://api.zerobounce.net/v2/validate",
        "rate": "free: 100 credits/mo",
    },
    # ── Email FINDERS (database lookups; used for risky/catch-all corporate
    # contacts the SMTP verifiers can't confirm). Reputation-safe: no send. ──
    "apollo": {
        "key": _read_key("APOLLO_API_KEY", "APOLLO_KEY"),
        "base": "https://api.apollo.io/api/v1/people/match",
        # NOTE: people/match API is PAID-ONLY — the free plan 403s it. 85 web-app
        # reveals still work for manual break-glass. Finder self-disables Apollo.
        "rate": "API paid-only; free plan = 85 manual web-app reveals/mo",
    },
    "prospeo": {
        "key": _read_key("PROSPEO_API_KEY", "PROSPEO_KEY"),
        "base": "https://api.prospeo.io/enrich-person",
        "rate": "free: 75 finds/mo, ~98% accuracy",
    },
    "snov": {
        "key": _read_key("SNOV_CLIENT_ID", "SNOV_API_KEY"),
        "secret": _read_key("SNOV_CLIENT_SECRET"),
        "base": "https://api.snov.io",
        "rate": "free: 50 credits/mo (OAuth client_credentials)",
    },
    # ── SEARCH fallback (real Google results; used when DuckDuckGo throttles or
    #    returns nothing on the LinkedIn X-ray). Search-only → reputation-safe. ──
    "serper": {
        "key": _read_key("SERPER_API_KEY", "SERPER_KEY"),
        "base": "https://google.serper.dev/search",
        "rate": "free: 2,500 one-time credits, then ~$0.30/1k",
    },
}

GROQ_MODEL = APIS["groq"]["model"]
HAS_GROQ = bool(APIS["groq"]["key"])
HAS_YOUTUBE = bool(APIS["youtube"]["key"])
GEMINI_MODEL = APIS["gemini"]["model"]
HAS_GEMINI = bool(APIS["gemini"]["key"])
HAS_REOON = bool(APIS["reoon"]["key"])
HAS_HUNTER = bool(APIS["hunter"]["key"])
HAS_ZEROBOUNCE = bool(APIS["zerobounce"]["key"])
HAS_APOLLO = bool(APIS["apollo"]["key"])
HAS_PROSPEO = bool(APIS["prospeo"]["key"])
HAS_SNOV = bool(APIS["snov"]["key"] and APIS["snov"]["secret"])
HAS_SERPER = bool(APIS["serper"]["key"])


# ── HARDENED HTTP ──────────────────────────────────────────────────────────
def _send(req, retries=4, timeout=20):
    """Open a urllib Request with 429 backoff. Returns parsed JSON.
    Raises on non-429 HTTP errors (so failures are LOUD, not silent).
    Shared by post_json + get_json so the UA/retry fix lives in one place."""
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            txt = ""
            try:
                txt = e.read().decode()
            except Exception:
                pass
            if e.code == 429 and attempt < retries:
                ra = e.headers.get("retry-after") if e.headers else None
                wait = None
                if ra:
                    try:
                        wait = float(ra)
                    except Exception:
                        wait = None
                if wait is None:
                    m = re.search(r"try again in ([\d.]+)s", txt)
                    wait = float(m.group(1)) if m else 6.0
                time.sleep(wait + 0.5)
                continue
            raise RuntimeError(f"HTTP {e.code}: {txt[:300]}")
    raise RuntimeError("rate-limited after retries")


def post_json(url, payload, headers=None, retries=4, timeout=20):
    """POST JSON with a browser UA and 429 backoff. Returns parsed JSON."""
    hdrs = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=hdrs)
    return _send(req, retries, timeout)


def get_json(url, params=None, headers=None, retries=4, timeout=20):
    """GET JSON with a browser UA and 429 backoff. Returns parsed JSON.
    Same hardening as post_json — for GET-based APIs like the email
    verifiers (Reoon / Hunter / ZeroBounce). Secrets go in `params`, never
    logged here."""
    if params:
        sep = "&" if "?" in url else "?"
        url = url + sep + urllib.parse.urlencode(params)
    hdrs = {"User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    return _send(req, retries, timeout)


# ── PROACTIVE RATE PACING ────────────────────────────────────────────────────
# Stay UNDER each provider's per-minute cap so we rarely hit a 429 in the first
# place (Gemini free = ~15 req/min, Groq free = ~30 req/min). _send still backs
# off on 429 as the safety net; this just spaces calls so we don't get there.
_LAST_CALL = {}
_MIN_INTERVAL = {"groq": 2.0, "gemini": 4.1}     # seconds between calls (per provider)


def _pace(provider):
    interval = _MIN_INTERVAL.get(provider, 0)
    if interval <= 0:
        return
    wait = interval - (time.monotonic() - _LAST_CALL.get(provider, 0))
    if wait > 0:
        time.sleep(wait)
    _LAST_CALL[provider] = time.monotonic()


def groq_chat(messages, model=None, max_tokens=20, temperature=0):
    """One Groq chat call -> assistant text. Raises on failure (caller decides
    whether to fall back — and should say so out loud if it does)."""
    if not HAS_GROQ:
        raise RuntimeError("no Groq key (env GROQ_API_KEY or .groq_key file)")
    _pace("groq")
    data = post_json(
        APIS["groq"]["base"],
        {"model": model or GROQ_MODEL, "temperature": temperature,
         "max_tokens": max_tokens, "messages": messages},
        headers={"Authorization": f"Bearer {APIS['groq']['key']}"})
    return data["choices"][0]["message"]["content"].strip()


def gemini_chat(prompt, model=None, max_tokens=20, temperature=0):
    """One Gemini call -> text. Gemini uses a different shape than Groq
    (contents/parts, key in an x-goog-api-key header). Raises on failure."""
    if not HAS_GEMINI:
        raise RuntimeError("no Gemini key (env GEMINI_API_KEY)")
    _pace("gemini")
    model = model or GEMINI_MODEL
    url = APIS["gemini"]["base"] + f"{model}:generateContent"
    data = post_json(
        url,
        {"contents": [{"parts": [{"text": prompt}]}],
         "generationConfig": {"temperature": temperature,
                              "maxOutputTokens": max_tokens}},
        headers={"x-goog-api-key": APIS["gemini"]["key"]})
    return data["candidates"][0]["content"]["parts"][0]["text"].strip()


def status():
    """Quick health line for a preflight check."""
    print("  Config APIs:")
    for name, a in APIS.items():
        have = "✅" if a.get("key") else "— (no key)"
        print(f"    {name:<10} {have}   rate: {a.get('rate', '?')}")


if __name__ == "__main__":
    status()
