"""
Rabbit-Hole Crawler
===================
Automates the manual "rabbit-hole" sourcing method: reads
YouTube's related-videos sidebar and tunnels creator-to-creator, catching the
association-only creators the keyword script structurally can't reach.

See docs/architecture.md for the full design.

────────────────────────────────────────────────────────────────────────────
PHASE 1 — SCRAPER CORE (this file so far)
Launch a clean-profile browser, clear the consent wall, prefer the "Related"
chip, and scrape ONE video's sidebar into a clean list of neighbors.

Run it standalone to prove it reads the graph:
    python3 rabbit_hole_crawler.py https://www.youtube.com/watch?v=VIDEO_ID
    python3 rabbit_hole_crawler.py VIDEO_ID            # bare id works too
    python3 rabbit_hole_crawler.py VIDEO_ID --headless

NOTE: selectors target the live YouTube desktop DOM, which changes without
notice. Every scrape step
is defensive (per-item try/except, multiple selector fallbacks) so one layout
change never kills the whole run.
────────────────────────────────────────────────────────────────────────────
"""

import envfile  # noqa: F401  — loads .env before any os.environ reads
import argparse
import json
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter, deque
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# Optional stealth — hides the automation fingerprint. Supports BOTH the legacy
# v1 function (stealth_sync) and the v2 rewrite (Stealth class). Safe without it.
_STEALTH_MODE = None       # "v1" | "v2" | None
_stealth_v1 = None
_Stealth = None
try:
    from playwright_stealth import stealth_sync as _stealth_v1   # v1.x
    _STEALTH_MODE = "v1"
except Exception:
    try:
        from playwright_stealth import Stealth as _Stealth       # v2.x
        _STEALTH_MODE = "v2"
    except Exception:
        _STEALTH_MODE = None


# ── CONFIG ────────────────────────────────────────────────────────────────
# A dedicated, throwaway sourcing profile — NEVER your main Google account.
# Persisted so cookies/consent survive between runs (looks human, fewer walls).
PROFILE_DIR   = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             ".rabbithole_profile")
SIDEBAR_WIDTH = 20          # how many sidebar items to read (free)
PACE_RANGE    = (3, 8)      # random seconds between navigations (human pacing)
NAV_TIMEOUT   = 30_000      # ms

# ── Flagging circuit breakers — stop hitting YouTube the moment it pushes back
MAX_CONSEC_EMPTY  = 4       # consecutive empty sidebars -> likely soft-blocked
MAX_CONSEC_ERRORS = 3       # consecutive nav errors/timeouts -> bail
API_FAIL_LIMIT    = 6       # consecutive API failures -> stop the API step
LAST_RUN_FLAGGED  = False   # set True when a run bails due to flagging
MAX_SUBS          = 900_000 # skip crawling channels bigger than this (matches your filter)


# ── SMALL HELPERS ─────────────────────────────────────────────────────────
def pace():
    """Random human-like pause. The deliberate 'slowness' that keeps us safe."""
    time.sleep(random.uniform(*PACE_RANGE))


def parse_view_count(text):
    """'1.2M views' / '345K views' / '1,234 views' / 'No views' -> int.

    Returns None when it can't parse (live streams, missing counts, etc.),
    which callers treat as 'unknown' rather than zero.
    """
    if not text:
        return None
    t = text.lower().replace("views", "").replace("watching", "").strip()
    t = t.replace(",", "")
    m = re.match(r"([\d.]+)\s*([kmb]?)", t)
    if not m:
        return None
    try:
        num = float(m.group(1))
    except ValueError:
        return None
    mult = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}.get(m.group(2), 1)
    return int(num * mult)


def extract_views_from_text(blob):
    """Pull 'N views' out of a metadata blob like 'Channel · 1.2M views · 3d ago'."""
    if not blob:
        return None
    m = re.search(r"([\d.,]+\s*[KMBkmb]?)\s*views", blob)
    return parse_view_count(m.group(1) + " views") if m else None


def video_id_from(arg):
    """Accept a full watch URL, a youtu.be link, or a bare 11-char id."""
    arg = arg.strip()
    m = re.search(r"[?&]v=([A-Za-z0-9_-]{11})", arg)
    if m:
        return m.group(1)
    m = re.search(r"youtu\.be/([A-Za-z0-9_-]{11})", arg)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", arg):
        return arg
    return None


def channel_id_from_href(href):
    """Normalise a channel link to '@handle' or 'channel/UC...' for dedup keys."""
    if not href:
        return ""
    href = href.split("?")[0]
    # @handle or /channel/UC... — matched anywhere, so a trailing /videos etc.
    # never throws it off.
    m = re.search(r"/(@[\w.\-]+)", href)
    if m:
        return m.group(1)
    m = re.search(r"/channel/([\w\-]+)", href)
    if m:
        return "channel/" + m.group(1)
    return href.rstrip("/")


# ── BROWSER LIFECYCLE ─────────────────────────────────────────────────────
def make_playwright_cm():
    """Context manager yielding a playwright instance, stealth-patched when the
    v2 library is present (v2 applies stealth by wrapping the whole instance)."""
    if _STEALTH_MODE == "v2":
        try:
            return _Stealth().use_sync(sync_playwright())
        except Exception:
            pass
    return sync_playwright()


def launch(pw, headless=False):
    """Open a persistent-profile Chromium. Persistent = cookies/consent stick,
    which looks far more human than a fresh session every time."""
    ctx = pw.chromium.launch_persistent_context(
        PROFILE_DIR,
        headless=headless,
        viewport={"width": 1360, "height": 900},
        args=["--disable-blink-features=AutomationControlled"],
        locale="en-US",
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    if _STEALTH_MODE == "v1":
        try:
            _stealth_v1(page)      # v2 is applied via make_playwright_cm() wrapper
        except Exception:
            pass
    page.set_default_timeout(NAV_TIMEOUT)
    return ctx, page


def clear_consent(page):
    """Best-effort dismissal of YouTube's cookie/consent wall.
    Consent lives either inline or on consent.youtube.com — try common buttons."""
    labels = ["Accept all", "Accept the use of cookies and other data",
              "I agree", "Reject all", "Accept"]
    # Consent is sometimes inside a form on consent.youtube.com
    for label in labels:
        try:
            btn = page.get_by_role("button", name=re.compile(label, re.I))
            if btn.count() > 0:
                btn.first.click(timeout=3000)
                page.wait_for_timeout(1200)
                return True
        except Exception:
            continue
    return False


def is_flagged(page):
    """Detect a bot-flag: /sorry redirect, CAPTCHA, or 'unusual traffic' page.
    This is the trip-wire that tells us to STOP hitting YouTube."""
    try:
        url = (page.url or "").lower()
        if "/sorry" in url:
            return True
        txt = page.evaluate(
            "() => document.body ? document.body.innerText.slice(0, 2500) : ''") or ""
        low = txt.lower()
        for sig in ("unusual traffic", "not a robot", "verify it's you",
                    "verify you're", "recaptcha", "i'm not a robot",
                    "before you continue to youtube"):
            if sig in low:
                return True
    except Exception:
        pass
    return False


# ── CAPTCHA HANDLER — pause, alert, let you solve it, resume ────────────────
def _say(text):
    try:
        subprocess.Popen(["say", text])
    except Exception:
        pass


def _notify(title, msg):
    # Pass title/msg as argv, never interpolated into the AppleScript source:
    # untrusted text (e.g. a scraped page title) can't break out of the string.
    script = ("on run argv\n"
              "display notification (item 2 of argv) with title (item 1 of argv)\n"
              "end run")
    try:
        subprocess.Popen(["osascript", "-e", script, "--", str(title), str(msg)])
    except Exception:
        pass


def _sound_loop(stop_event):
    """Loop Blow.aiff until the stop event is set (macOS)."""
    while not stop_event.is_set():
        try:
            subprocess.run(["afplay", "/System/Library/Sounds/Blow.aiff"], timeout=8)
        except Exception:
            time.sleep(1)
        if stop_event.wait(0.3):
            break


def handle_captcha(page, url=None):
    """Pause on a CAPTCHA: bring the window forward, loop the alert sound, speak,
    banner, and wait for you to solve it + press Enter. Returns True if cleared."""
    try:
        page.bring_to_front()
    except Exception:
        pass
    stop = threading.Event()
    threading.Thread(target=_sound_loop, args=(stop,), daemon=True).start()
    _say("CAPTCHA detected")
    _notify("Rabbit-Hole Crawler", "Paused — solve the CAPTCHA in the browser")
    try:
        input("\n  ⚠ CAPTCHA detected — solve it in the browser window, "
              "then press Enter... ")
    except EOFError:
        pass
    finally:
        stop.set()                                     # stop the sound
    if url:                                             # reload the intended video
        try:
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(1000)
        except Exception:
            pass
    else:
        page.wait_for_timeout(500)
    return not is_flagged(page)


# ── THE "RELATED" CHIP ────────────────────────────────────────────────────
def click_related_chip(page):
    """Prefer the 'Related' filter chip (topical neighbours) over the default
    'All' (personalised mix). Returns the mode we ended up on. Robust to the
    legacy chip renderer and the newer chip-shape component."""
    try:
        clicked = page.evaluate(r"""
        () => {
          const cands = [...document.querySelectorAll(
            'yt-chip-cloud-chip-renderer, #secondary [role=tab], '
            + '.ytChipShapeChip, #secondary button, tp-yt-paper-chip')];
          for (const c of cands) {
            if ((c.textContent || '').trim().toLowerCase() === 'related') {
              c.click(); return true;
            }
          }
          return false;
        }
        """)
        if clicked:
            page.wait_for_timeout(1500)   # let the list reload
            return "Related"
    except Exception:
        pass
    return "All"   # chip absent (YouTube A/B) — fall back to the default list


# ── SIDEBAR SCRAPE ────────────────────────────────────────────────────────
# YouTube is mid-migration from the legacy `ytd-compact-video-renderer` to the
# new `yt-lockup-view-model` component. We scrape via one JS pass so we catch
# BOTH and don't depend on any single component's internal structure.
_SCRAPE_JS = r"""
() => {
  const root = document.querySelector('#secondary')
            || document.querySelector('ytd-watch-next-secondary-results-renderer')
            || document.body;
  const nodes = [...root.querySelectorAll(
    'ytd-compact-video-renderer, yt-lockup-view-model')];
  const out = [];
  for (const it of nodes) {
    const rec = {video_id:'', title:'', channel_name:'', channel_href:'', rows:[]};
    const vlink = it.querySelector('a[href*="/watch"]');
    if (vlink) {
      const m = (vlink.getAttribute('href')||'').match(/[?&]v=([A-Za-z0-9_-]{11})/);
      if (m) rec.video_id = m[1];
    }
    const t = it.querySelector('h3[title], #video-title, a[title]');
    if (t) rec.title = (t.getAttribute('title') || t.textContent || '').trim();
    // Channel name: avatar aria-label 'Go to channel X' is cleanest (no badge text).
    const av = it.querySelector('[aria-label^="Go to channel"]');
    if (av) rec.channel_name =
      (av.getAttribute('aria-label')||'').replace(/^Go to channel\s*/, '').trim();
    // Channel link only exists in the legacy DOM; keep it when present.
    const chl = [...it.querySelectorAll('a[href]')]
      .find(a => /(\/@|\/channel\/)/.test(a.getAttribute('href')||''));
    if (chl) { rec.channel_href = chl.getAttribute('href')||'';
               if (!rec.channel_name) rec.channel_name = (chl.textContent||'').trim(); }
    // Metadata rows — new camelCase class + legacy fallbacks.
    rec.rows = [...it.querySelectorAll(
      '.ytContentMetadataViewModelMetadataRow, '
      + '.yt-content-metadata-view-model__metadata-row, #metadata-line')]
      .map(r => (r.textContent||'').trim()).filter(Boolean);
    if (rec.video_id || rec.channel_name || rec.rows.length) out.push(rec);
  }
  return out;
}
"""

_DEBUG_JS = r"""
() => {
  const root = document.querySelector('#secondary')
            || document.querySelector('ytd-watch-next-secondary-results-renderer')
            || document.body;
  const counts = {};
  for (const el of root.querySelectorAll('*')) {
    const tag = el.tagName.toLowerCase();
    if (/(lockup|compact|renderer|video)/.test(tag))
      counts[tag] = (counts[tag]||0) + 1;
  }
  return counts;
}
"""


def scrape_sidebar(page, limit=SIDEBAR_WIDTH):
    """Read the related sidebar into neighbour dicts:
        {video_id, title, channel_name, channel_id, views}
    Free — one JS scrape, no API, no extra pages. Handles old + new DOM."""
    try:
        page.wait_for_selector(
            "ytd-compact-video-renderer, yt-lockup-view-model", timeout=12_000)
    except PWTimeout:
        pass
    # A couple of light scrolls nudge lazy-loading so we reliably see ~20 items.
    for _ in range(2):
        try:
            page.mouse.wheel(0, 1800)
            page.wait_for_timeout(700)
        except Exception:
            break
    try:
        raw = page.evaluate(_SCRAPE_JS)
    except Exception as e:
        print(f"  scrape error: {e}")
        raw = []
    out = []
    for r in raw[:limit]:
        rows = r.get("rows", [])
        # views = first metadata row that actually mentions "views"
        views = None
        for row in rows:
            v = extract_views_from_text(row)
            if v is not None:
                views = v
                break
        # channel fallback: first row that isn't the views/date line
        cname = r.get("channel_name", "")
        if not cname:
            for row in rows:
                low = row.lower()
                if "views" not in low and "ago" not in low:
                    cname = row
                    break
        out.append({
            "video_id":     r.get("video_id", ""),
            "title":        r.get("title", ""),
            "channel_name": cname,
            "channel_id":   channel_id_from_href(r.get("channel_href", "")),
            "views":        views,
        })
    return out


_SUBS_JS = """
() => {
  const el = document.querySelector('#owner-sub-count');
  return el ? el.textContent.trim() : '';
}
"""


def scrape_sub_count(page):
    """Read the current video's channel subscriber count off the watch page
    (free — no API). Returns an int (e.g. '1.2M subscribers' -> 1200000), or None."""
    try:
        txt = page.evaluate(_SUBS_JS)
        return parse_view_count(txt) if txt else None
    except Exception:
        return None


def debug_dump(page):
    """When a scrape comes back empty, show which components ARE in the sidebar
    so we can tune selectors precisely."""
    try:
        counts = page.evaluate(_DEBUG_JS)
        print("  [debug] sidebar component tags found:", counts or "none")
    except Exception as e:
        print(f"  [debug] dump failed: {e}")


def dump_first_item(page):
    """Print the first sidebar item's HTML so selectors can be tuned exactly."""
    js = r"""
    () => {
      const root = document.querySelector('#secondary') || document.body;
      const it = root.querySelector('yt-lockup-view-model, ytd-compact-video-renderer');
      if (!it) return '(no sidebar item found)';
      const meta = it.querySelector('.ytLockupViewModelMetadata, '
        + 'yt-lockup-metadata-view-model, #meta, #metadata');
      return (meta ? meta.outerHTML : it.outerHTML).slice(0, 3500);
    }
    """
    try:
        print("\n  [dump] first sidebar item HTML:\n")
        print(page.evaluate(js))
    except Exception as e:
        print(f"  [dump] failed: {e}")


_CHIPS_JS = r"""
() => {
  const out = []; const seen = new Set();
  for (const c of document.querySelectorAll(
      'yt-chip-cloud-chip-renderer, .ytChipShapeChip, [role="tab"], tp-yt-paper-chip')) {
    const t = (c.textContent||'').trim();
    if (t && t.length < 25 && !seen.has(t)) { seen.add(t); out.push(t); }
  }
  return out;
}
"""


def dump_chips(page):
    """Print the filter chips available above the sidebar (All / Related / ...)."""
    try:
        chips = page.evaluate(_CHIPS_JS)
        print(f"  [chips] filter chips available: {chips or 'none found'}")
    except Exception as e:
        print(f"  [chips] failed: {e}")


def setup_profile():
    """One-time: open the crawler's OWN profile so you can sign in + configure it.
    Whatever you set here (login, history off) persists for every future run."""
    with make_playwright_cm() as pw:
        ctx, page = launch(pw, headless=False)   # always headed for setup
        try:
            page.goto("https://www.youtube.com")
            print("\n  A browser window is open on YouTube — this is the crawler's")
            print("  OWN profile (separate from your normal Chrome).")
            print("   1) Sign in with your dedicated crawler Google account.")
            print("   2) Turn OFF watch + search history and ad personalization.")
            print("   3) Leave it on youtube.com when done.")
            input("\n  Press Enter HERE when finished — saves the profile and closes... ")
        finally:
            ctx.close()
    print("  ✅ Profile saved. Future runs reuse this signed-in, configured profile.")


# ── NICHE GATE (Tier-0 glance) ────────────────────────────────────────────
# PLACEHOLDER matcher for Phase 2. Phase 3 replaces this with real scoring
# against the script's KEYWORDS + each creator's stored niche context.
_DEFAULT_NICHE_HINTS = [
    "ai", "gpt", "llm", "claude", "chatgpt", "gemini", "agent", "agentic",
    "machine learning", "automation", "mcp", "prompt", "cursor", "copilot",
    "coding", "developer", "software", "saas", "api", "no-code", "workflow",
]

# Over-generic words that cause drift if they leak into the niche gate
# (e.g. "hacker caught" -> "caught" matches every true-crime title).
_NICHE_STOPWORDS = {
    "caught", "internet", "online", "new", "best", "video", "videos", "tips",
    "explained", "tutorial", "review", "reviews", "guide", "top", "vs", "real",
    "life", "story", "stories", "exposed", "secret", "truth", "world", "day",
    "time", "day", "you", "your", "how", "why", "what", "this", "that", "get",
}

def is_on_niche(title, keywords=None):
    """Cheap title-based niche check (the human's pre-click glance).
    Returns True if the title looks in-niche. Permissive by design — the full
    channel filter still runs later before anything is logged."""
    if not title:
        return False
    t = title.lower()
    hints = keywords or _DEFAULT_NICHE_HINTS
    # Word-boundary match so short hints ('ai', 'api') don't hit inside words
    # like 'pAInting' or 'chAPIter'.
    return any(re.search(r"\b" + re.escape(h) + r"\b", t) for h in hints)


# ── GROQ NICHE CLASSIFIER (optional — smarter than keyword matching) ────────
# Fixes the "good creator with a clickbait title slips through" gap. Reads the
# channel name + title with an LLM instead of literal word matching. Free/fast.
# Key: set env GROQ_API_KEY, or drop it in a `.groq_key` file next to this script.
GROQ_API_KEY = (os.environ.get("GROQ_API_KEY") or os.environ.get("GROQ_KEY")
                or os.environ.get("GROQ_API_TOKEN") or os.environ.get("GROQ_TOKEN")
                or "")
if not GROQ_API_KEY:
    _kf = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".groq_key")
    if os.path.exists(_kf):
        try:
            GROQ_API_KEY = open(_kf).read().strip()
        except Exception:
            pass
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
_HAS_GROQ = bool(GROQ_API_KEY)
_groq_cache = {}   # (name|title) -> bool, dedups calls within a run


def groq_niche_ok(name, title, niche_label):
    """Ask Groq (yes/no) whether a creator fits the niche, from free sidebar
    data. Returns True/False, or None on any error so the caller can fall back."""
    if not _HAS_GROQ:
        return None
    ck = f"{name}|{title}"
    if ck in _groq_cache:
        return _groq_cache[ck]
    body = json.dumps({
        "model": GROQ_MODEL,
        "temperature": 0, "max_tokens": 4,
        "messages": [
            {"role": "system",
             "content": (
                 "You screen YouTube creators for an agency that places them "
                 "with tech companies — software, SaaS, cybersecurity, VPN, "
                 "privacy, antivirus, AI, hardware, and developer-tool brands. "
                 "These brands sponsor a WIDE range of creators, so be inclusive. "
                 "Answer YES if there is ANY plausible chance a tech company would "
                 "sponsor this creator — because of their CONTENT (tech, security, "
                 "privacy, hacking, exploits, scams/fraud, AI, software, Linux, "
                 "gaming) OR their AUDIENCE (developers, gamers, students, "
                 "professionals, commentary, education, science, productivity, "
                 "tech-curious). When in doubt, answer YES. Answer NO only for "
                 "pure kids' content, religious sermons, ASMR/screensaver loops, "
                 "non-English regional music, or brand-unsafe content. "
                 "Reply with only YES or NO.")},
            {"role": "user",
             "content": f"Channel: {name!r}. Recent title: {title!r}. "
                        f"Found via keyword: {niche_label!r}. "
                        f"Would a tech company plausibly sponsor this creator? "
                        f"YES or NO."},
        ],
    }).encode()
    req = urllib.request.Request(GROQ_URL, data=body, headers={
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
        # Cloudflare 403s the default 'Python-urllib' agent — send a normal one.
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read())
            ans = data["choices"][0]["message"]["content"].strip().lower()
            verdict = ans.startswith("y")
            _groq_cache[ck] = verdict
            return verdict
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 3:          # rate-limited — back off + retry
                body = ""
                try:
                    body = e.read().decode()
                except Exception:
                    pass
                ra = e.headers.get("retry-after") if e.headers else None
                wait = None
                if ra:
                    try:
                        wait = float(ra)
                    except Exception:
                        wait = None
                if wait is None:
                    m = re.search(r"try again in ([\d.]+)s", body)
                    wait = float(m.group(1)) if m else 5.0
                time.sleep(wait + 0.3)
                continue
            return None                                # other HTTP error -> fall back
        except Exception:
            return None                                # fall back to keywords
    return None


def niche_gate(name, title, keywords=None, niche_label=None):
    """Smart niche gate: use Groq when available, else keyword matching."""
    label = niche_label or "technology, AI, software, developer, or cybersecurity creators"
    if _HAS_GROQ:
        v = groq_niche_ok(name, title, label)
        if v is not None:
            return v
    return is_on_niche(title, keywords)


_groq_brand_cache = {}

def groq_is_tech_brand(brand):
    """True if the brand is a TECH company (keep). False = drop it (clothing,
    food, wellness, etc.). None on error/no-key so the caller keeps it."""
    if not _HAS_GROQ or not brand:
        return None
    b = brand.strip().lower()
    if b in _groq_brand_cache:
        return _groq_brand_cache[b]
    body = json.dumps({
        "model": GROQ_MODEL, "temperature": 0, "max_tokens": 4,
        "messages": [
            {"role": "system", "content": (
                "You classify whether a brand is a TECHNOLOGY company — software, "
                "SaaS, apps, AI, hardware, electronics, cybersecurity, VPN, privacy, "
                "developer tools, or tech platforms. Answer NO for clothing, apparel, "
                "food, drinks, supplements, wellness, beauty, or other non-tech "
                "consumer brands. Reply only YES or NO.")},
            {"role": "user",
             "content": f"Brand: {brand!r}. Is it a technology company? YES or NO."},
        ],
    }).encode()
    req = urllib.request.Request(GROQ_URL, data=body, headers={
        "Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        v = data["choices"][0]["message"]["content"].strip().lower().startswith("y")
        _groq_brand_cache[b] = v
        return v
    except Exception:
        return None


# ── PHASE 2: 3-HOP RECURSION ──────────────────────────────────────────────
def crawl(seed_ids, hops=3, min_views=50_000, expand_cap=10,
          max_pages=None, target=None, headless=False, keywords=None,
          max_views=None):
    """Rabbit-hole from seed videos, up to `hops` deep.

    - Seeds are always opened (they're your chosen passers).
    - Neighbours are expanded only if they pass the Tier-0 glance:
      on-niche + >= min_views + not already visited/expanded + within expand_cap.
    - One video per channel (a channel is only expanded once per run).
    - Stops at: empty frontier, page ceiling, or target discovered.

    Returns {channel_key: {channel, video_id, views, title, hop}}.
    (Phase 3 wires in the processed-cache skip, full filter, and sheet/queue.)
    """
    global LAST_RUN_FLAGGED
    LAST_RUN_FLAGGED = False
    visited = set()             # video ids already opened this run
    expanded_channels = set()   # channels already tunnelled from (1 vid/channel)
    discovered = {}             # channel_key -> record
    frontier = deque((vid, 0) for vid in seed_ids)
    pages = 0
    first = True
    consec_empty = consec_err = 0

    with make_playwright_cm() as pw:
        ctx, page = launch(pw, headless=headless)
        try:
            while frontier:
                if max_pages and pages >= max_pages:
                    print(f"\n  ⛔ page ceiling ({max_pages}) reached")
                    break
                if target and len(discovered) >= target:
                    print(f"\n  ✅ target ({target}) reached")
                    break

                vid, hop = frontier.popleft()
                if vid in visited:
                    continue
                visited.add(vid)

                url = f"https://www.youtube.com/watch?v={vid}"
                try:
                    page.goto(url, wait_until="domcontentloaded")
                except Exception as e:
                    print(f"  nav error {vid}: {e}")
                    consec_err += 1
                    if consec_err >= MAX_CONSEC_ERRORS:
                        print("  ⛔ too many navigation errors in a row — stopping "
                              "(possible block). Progress saved.")
                        LAST_RUN_FLAGGED = True
                        break
                    continue
                consec_err = 0
                if first:
                    clear_consent(page)
                    first = False
                page.wait_for_timeout(1000)

                # Trip-wire: YouTube is challenging us.
                if is_flagged(page):
                    if headless:
                        print("\n  🚨 FLAGGED in headless (no window to solve in) — "
                              "stopping. Progress saved.")
                        LAST_RUN_FLAGGED = True
                        break
                    print("\n  🚨 CAPTCHA / challenge detected — pausing for you.")
                    if handle_captcha(page, url):
                        print("  ✅ Cleared — resuming.")
                        consec_empty = 0
                        # fall through and scrape the now-loaded video
                    else:
                        print("  🚨 Still blocked after solving — stopping. Progress saved.")
                        LAST_RUN_FLAGGED = True
                        break

                click_related_chip(page)

                # Skip crawling FROM channels over the sub cap — stops the crawl
                # tunnelling deeper into mega-channel territory. Free (reads the page).
                subs = scrape_sub_count(page)
                if subs and subs > MAX_SUBS:
                    pages += 1
                    print(f"  [page {pages} · hop {hop}] {vid} → skipped "
                          f"({subs:,} subs > {MAX_SUBS:,} cap)")
                    pace()
                    continue

                neighbours = scrape_sidebar(page)
                pages += 1
                print(f"  [page {pages} · hop {hop}] {vid} → {len(neighbours)} neighbours")

                if not neighbours:
                    consec_empty += 1
                    if consec_empty >= MAX_CONSEC_EMPTY:
                        print("  ⛔ several empty sidebars in a row — stopping "
                              "(possible soft-block). Progress saved.")
                        LAST_RUN_FLAGGED = True
                        break
                else:
                    consec_empty = 0

                expanded = 0
                qualified = 0
                for n in neighbours:
                    ckey = n["channel_id"] or n["channel_name"]
                    if ckey and ckey not in discovered:
                        discovered[ckey] = {
                            "channel":  n["channel_name"],
                            "video_id": n["video_id"],
                            "views":    n["views"],
                            "title":    n["title"],
                            "hop":      hop + 1,
                        }
                    # Quality gates = what "qualifies" a neighbour for expansion
                    # (views floor/ceiling + niche), before dedup and the cap.
                    v = n["views"] or 0
                    qualifies = bool(
                        n["video_id"] and v >= min_views
                        and (not max_views or v <= max_views)
                        and is_on_niche(n["title"], keywords))
                    if qualifies:
                        qualified += 1
                    # Expand it — subject to depth, the cap, and per-run dedup.
                    if hop >= hops or expanded >= expand_cap or not qualifies:
                        continue
                    if n["video_id"] in visited or ckey in expanded_channels:
                        continue                       # one video per channel
                    frontier.append((n["video_id"], hop + 1))
                    expanded_channels.add(ckey)
                    expanded += 1

                print(f"        ↳ {qualified}/{len(neighbours)} qualified · "
                      f"{expanded} expanded")
                pace()   # human delay before the next page
        finally:
            ctx.close()
    return discovered


# ── PHASE 3: PIPELINE INTEGRATION ─────────────────────────────────────────
# Reuse the keyword script's helpers (stats, enrichment, filters, sheet, cache)
# so the crawler runs discoveries through the EXACT same pipeline.
try:
    import influncers_yt_finderScript as ks
    _HAS_KS = True
    _KS_ERR = ""
except Exception as e:                       # gspread/creds missing, etc.
    ks = None
    _HAS_KS = False
    _KS_ERR = str(e)

SEED_QUEUE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "seed_queue.json")

# Sheet column indexes (0-based from column A). Matches the keyword script's
# "Columns C-O: Name|Link|Email|Tag|Outreach Status|Subs|Avg|Eng|Niche|Keyword|..."
COL_NAME, COL_LINK, COL_TAG, COL_STATUS, COL_NICHE, COL_KEYWORD = 2, 3, 5, 6, 10, 11


def load_queue():
    if not os.path.exists(SEED_QUEUE_FILE):
        return {}
    try:
        with open(SEED_QUEUE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_queue(q):
    with open(SEED_QUEUE_FILE, "w") as f:
        json.dump(q, f, indent=2)


def resolve_channel_id(video_id):
    """video_id -> channelId via one videos.list snippet call (reuses ks.yt_get).
    The new sidebar DOM has no channel link, so we resolve the id from the video."""
    data = ks.yt_get("videos", {"part": "snippet", "id": video_id})
    items = data.get("items", [])
    return items[0]["snippet"].get("channelId") if items else None


def confirm_channel(cid):
    """Tier-1: run the FULL keyword-script filter on a channel id.
    Returns (passer_dict, "pass") or (None, "<reason>") so callers can report WHY."""
    info = ks.get_base_stats([cid]).get(cid)
    if not info:
        return None, "no_data"
    if info["subs"] >= ks.FILTERS["max_subs"]:
        return None, "subs_too_big"
    if info["subs"] <= 1000:
        return None, "subs_too_small"
    if getattr(ks, "FILTER_OUT_FEMALE", False) and \
       ks.is_likely_female(info["name"], info.get("description", "")):
        return None, "gender_filter"
    vids, latest_date, titles, publish_dates = \
        ks.get_recent_video_ids(info["uploads_playlist"], 13)
    if not vids:
        return None, "no_videos"
    if getattr(ks, "FILTER_NON_ENGLISH", False) and not ks.is_likely_english(
            info.get("language", ""), info.get("description", ""), titles[:13]):
        return None, "non_english"
    now = datetime.now(timezone.utc)
    stats, videos_meta, longform_dates = ks.get_video_stats(vids, stat_limit=13)
    if not stats:
        return None, "no_stats"
    longform_in_window = sum(
        1 for d in longform_dates if (now - d).days <= ks.ACTIVITY_WINDOW_DAYS)
    if longform_in_window < ks.MIN_UPLOADS_IN_WINDOW:
        return None, "inactive"
    tv = sum(s["v"] for s in stats)
    avg = tv / len(stats)
    eng = (sum(s["l"] for s in stats) + sum(s["c"] for s in stats)) / tv if tv else 0
    if avg < ks.FILTERS["min_avg_views"]:
        return None, "low_avg_views"
    if eng < ks.FILTERS["min_engagement"]:
        return None, "low_engagement"
    return {
        "name": info["name"], "url": f"https://youtube.com/channel/{cid}",
        "email": info["email"], "tag": info["tag"], "subs": info["subs"],
        "avg_views": round(avg), "engagement": round(eng * 100, 2), "cid": cid,
        # descriptions we already fetched — fed to the free sponsor scan.
        "_videos_meta": videos_meta,
    }, "pass"


def source_discovered(discovered, seed_handle="", keywords=None):
    """Tier-1 confirm each niche discovery, then write passers to the sheet AND
    the local seed queue. Skips anything already cached or already in the sheet."""
    if not _HAS_KS:
        print(f"  ⚠ can't import keyword script ({_KS_ERR}) — run from the src/ "
              f"folder with gspread installed.")
        return
    ws = ks.connect_sheet()
    existing_names, existing_urls = ks.get_existing_data(ws)
    cache = ks.load_cache()
    cache.setdefault("processed", {})
    queue = load_queue()

    # Build a human niche label for Groq from the seeds' own context words.
    label = None
    if keywords:
        terms = [k for k in keywords if k not in _DEFAULT_NICHE_HINTS][:8]
        if terms:
            label = "creators making content about: " + ", ".join(terms)

    passers = []
    brand_pending = []          # (record, sighting_key) tuples for the Brands tab
    checked = 0
    api_fail = 0
    skip = Counter()            # why a discovery never reached the full filter
    fail = Counter()            # why a checked candidate failed the full filter
    for rec in discovered.values():
        if not niche_gate(rec["channel"], rec["title"], keywords, label):
            skip["off-niche"] += 1; rec["status"] = "off-niche"
            continue                                   # smart niche gate (Groq or keywords)
        vid = rec["video_id"]
        if not vid:
            skip["no-video-id"] += 1; rec["status"] = "no-video"
            continue
        cid = resolve_channel_id(vid)
        if not cid:
            skip["resolve-failed"] += 1; rec["status"] = "resolve-fail"
            api_fail += 1
            if api_fail >= API_FAIL_LIMIT:
                print("  ⛔ repeated API failures — stopping the source step "
                      "(quota / rate-limit?). What passed so far is saved.")
                break
            continue
        api_fail = 0
        url = f"https://youtube.com/channel/{cid}"
        if cid in cache["processed"]:
            skip["already-judged"] += 1
            rec["status"] = "judged:" + cache["processed"][cid].get("verdict", "?")
            continue                                   # never re-evaluate
        if url in existing_urls or (rec["channel"] or "").lower() in existing_names:
            skip["already-in-sheet"] += 1; rec["status"] = "in-sheet"
            continue                                   # already in the sheet
        checked += 1
        passer, reason = confirm_channel(cid)
        rec["status"] = "NEW" if passer else reason
        cache["processed"][cid] = {"verdict": reason,
                                   "ts": datetime.now(timezone.utc).isoformat()}
        if passer:
            passer["keyword"] = f"rabbithole:{seed_handle}" if seed_handle else "rabbithole"
            # Free sponsor scan on the descriptions we already fetched (0 API cost).
            vm = passer.pop("_videos_meta", [])
            try:
                import brand_sourcer
                new_rows = brand_sourcer.rows_from_descs(vm, passer["name"], passer["subs"])
                # Keep only TECH brands — drop clothing/food/wellness/etc.
                # (Groq False = confirmed non-tech; True/None = keep.)
                new_rows = [(r, k) for (r, k) in new_rows
                            if groq_is_tech_brand(r["brand"]) is not False]
                brand_pending += new_rows
                passer["sponsors"] = ", ".join(r["brand"] for r, _ in new_rows)
            except Exception as be:
                passer["sponsors"] = ""
                print(f"    (brand scan skipped for {passer['name']}: {be})")
            passers.append(passer)
            queue[cid] = {"status": "pending", "seed_video": vid,
                          "niche_context": passer["keyword"], "name": passer["name"]}
        else:
            fail[reason] += 1

    if passers:
        ks.write_to_sheet(ws, passers)
    # Brands by-product: write sponsor brands found on the passing channels.
    if brand_pending:
        try:
            import brand_sourcer
            n_new = brand_sourcer.save_rows(brand_pending)
            print(f"  [brands] {len(brand_pending)} sponsor sighting(s) scanned → "
                  f"{n_new} new brand(s) added to the Brands tab.")
        except Exception as e:
            print(f"  [brands] write skipped ({e}).")
    ks.save_cache(cache)
    save_queue(queue)
    print(f"\n  Tier-1 checked {checked} candidates → {len(passers)} new passers "
          f"written to the sheet + queue.")
    if skip:
        print(f"  Not checked (why): {dict(skip)}")
    if fail:
        print(f"  Checked but failed (why): {dict(fail)}")


def _cell_url(cell):
    """Pull the real URL from a sheet cell — handles =HYPERLINK("url","label")
    and plain URLs. Returns '' for display-text-only or header cells."""
    if not cell:
        return ""
    m = re.search(r'HYPERLINK\("([^"]+)"', cell, re.IGNORECASE)
    if m:
        return m.group(1)
    return cell if cell.lower().startswith("http") else ""


def backfill_queue():
    """One-time: load existing NON-'Untouched' sheet passers into the local queue
    as `pending`, so your engaged back-catalogue becomes crawl anchors. Reads the
    sheet, writes only the local file — the sheet is never modified."""
    if not _HAS_KS:
        print(f"  ⚠ can't import keyword script ({_KS_ERR}).")
        return
    ws = ks.connect_sheet()
    # FORMULA render exposes the real URL behind hyperlinked cells; plain
    # get_all_values would only give display text like 'Foo on YouTube'.
    try:
        rows = ws.get_all_values(value_render_option="FORMULA")
    except Exception:
        rows = ws.get_all_values()
    queue = load_queue()
    added = skipped_untouched = skipped_nolink = 0
    for row in rows[1:]:
        if len(row) <= COL_STATUS:
            continue
        name = str(row[COL_NAME]).strip()
        url = _cell_url(str(row[COL_LINK]).strip())
        # Fallback: build the URL from the @handle in the Tag column.
        tag = str(row[COL_TAG]).strip() if len(row) > COL_TAG else ""
        if not url and tag.startswith("@"):
            url = f"https://youtube.com/{tag}"
        status = str(row[COL_STATUS]).strip()
        if not name or not url:
            skipped_nolink += 1
            continue                                   # header rows / no real link
        if status.lower() == "untouched":
            skipped_untouched += 1
            continue                                   # keep the backlog out
        if url in queue:
            continue
        niche_ctx = (str(row[COL_KEYWORD]).strip() if len(row) > COL_KEYWORD else "") \
            or (str(row[COL_NICHE]).strip() if len(row) > COL_NICHE else "")
        queue[url] = {"status": "pending", "seed_video": "",    # derived lazily later
                      "niche_context": niche_ctx, "name": name, "link": url}
        added += 1
    save_queue(queue)
    print(f"  Back-fill: {added} engaged creators added as pending "
          f"({skipped_untouched} 'Untouched', {skipped_nolink} no-link skipped).")
    print(f"  Queue: {SEED_QUEUE_FILE}")


# ── PHASE 4: SEED FROM THE QUEUE ──────────────────────────────────────────
def channel_id_from_url(url):
    """Extract a UC... channel id from a /channel/ URL (None if it's a @handle)."""
    m = re.search(r"/channel/(UC[\w-]+)", url or "")
    return m.group(1) if m else None


def resolve_handle(handle):
    """@handle -> channel id via channels.list?forHandle (one API call)."""
    h = handle.lstrip("@")
    data = ks.yt_get("channels", {"part": "id", "forHandle": h})
    items = data.get("items", [])
    return items[0]["id"] if items else None


def derive_seed_video(cid, niche_context=""):
    """Pick a channel's most on-niche recent video as its crawl anchor.
    Scores against the creator's STORED niche context first (per the spec),
    then general niche, then falls back to the newest upload."""
    info = ks.get_base_stats([cid]).get(cid)
    if not info:
        return None
    vids, _latest, titles, _dates = ks.get_recent_video_ids(info["uploads_playlist"], 13)
    if not vids:
        return None
    ctx_words = [w for w in re.split(r"[^a-z0-9]+", (niche_context or "").lower())
                 if len(w) > 2]
    # 1) newest title matching the creator's own stored niche context
    for v, t in zip(vids, titles):
        tl = (t or "").lower()
        if ctx_words and any(re.search(r"\b" + re.escape(w) + r"\b", tl) for w in ctx_words):
            return v
    # 2) newest generally on-niche title
    for v, t in zip(vids, titles):
        if is_on_niche(t):
            return v
    # 3) fall back to the newest upload
    return vids[0]


def _resolve_cid(key, entry):
    """Best-effort channel id for a queue entry (UC key, /channel/ url, or @handle)."""
    if key.startswith("UC"):
        return key
    link = entry.get("link") or key
    cid = channel_id_from_url(link)
    if cid:
        return cid
    if "/@" in link:
        return resolve_handle(link.split("/@")[-1])
    if link.startswith("@"):
        return resolve_handle(link)
    return None


def _manual_anchor(raw):
    """Turn a --seed anchor (video URL/id, @handle, or channel URL) into a
    (key, entry) pair with a seed_video, or None if it can't be resolved."""
    vid = video_id_from(raw)
    if vid:                                            # a video URL/id → use directly
        return ("manual:" + vid,
                {"seed_video": vid, "niche_context": "", "name": raw})
    cid = channel_id_from_url(raw)                      # /channel/UC... URL
    if not cid and "/@" in raw:
        cid = resolve_handle(raw.split("/@")[-1])
    if not cid and raw.lstrip("@"):
        cid = resolve_handle(raw)                       # bare @handle
    if not cid:
        print(f"    - {raw}: can't resolve channel, skipping")
        return None
    vid = derive_seed_video(cid, "")
    if not vid:
        print(f"    - {raw}: no video found, skipping")
        return None
    return (cid, {"seed_video": vid, "niche_context": "", "name": raw})


def crawl_from_queue(n_seeds=5, hops=3, min_views=50_000, expand_cap=10,
                     max_pages=None, target=None, headless=False, source=False,
                     max_views=None, consume=True, manual_seeds=None, random_pick=False):
    """Crawl from pending queue seeds AND/OR manual --seed anchors. Queue seeds
    can be taken in order or at random (`random_pick`). Only queue seeds get
    consumed (flipped to 'crawled') — manual anchors don't touch the queue."""
    if not _HAS_KS:
        print(f"  ⚠ can't import keyword script ({_KS_ERR}).")
        return
    queue = load_queue()

    batch = []                                         # (key, entry, in_queue)
    for raw in (manual_seeds or []):                   # 1) manual --seed anchors
        anc = _manual_anchor(raw)
        if anc:
            batch.append((anc[0], anc[1], False))

    pending = [(k, v) for k, v in queue.items() if v.get("status") == "pending"]
    if random_pick:
        pending = random.sample(pending, len(pending))  # shuffle, then take n_seeds
    for key, entry in pending[:max(0, n_seeds)]:       # 2) queue seeds
        batch.append((key, entry, True))

    if not batch:
        print("  Nothing to seed — no --seed anchors and no pending queue seeds.")
        return
    print(f"  Preparing {len(batch)} seed(s)...\n")

    seed_videos, seeded_keys, seeded_entries = [], [], []
    for key, entry, in_queue in batch:
        vid = entry.get("seed_video")
        if not vid:                                    # back-filled — derive it
            cid = _resolve_cid(key, entry)
            if not cid:
                print(f"    - {entry.get('name','?')}: can't resolve channel, skipping")
                continue
            vid = derive_seed_video(cid, entry.get("niche_context", ""))
            if vid:
                entry["seed_video"] = vid              # cache it for next time
        if vid:
            seed_videos.append(vid)
            seeded_entries.append(entry)
            if in_queue:
                seeded_keys.append(key)                # only queue seeds get consumed
            tag = "" if in_queue else " (manual)"
            print(f"    - {entry.get('name','?')}{tag}: anchor video {vid}")

    save_queue(queue)                                  # persist any derived videos
    if not seed_videos:
        print("  No usable seed videos derived.")
        return

    # Build the niche gate from the seeds' OWN stored contexts + general hints,
    # so a cybersecurity seed crawls cybersecurity neighbours (not just AI ones).
    niche_words = set()
    for entry in seeded_entries:
        for w in re.split(r"[^a-z0-9]+", (entry.get("niche_context", "") or "").lower()):
            if len(w) > 2 and not w.startswith("rabbithole") and w not in _NICHE_STOPWORDS:
                niche_words.add(w)
    gate_keywords = sorted(niche_words) + _DEFAULT_NICHE_HINTS

    print(f"\n  Crawling {len(seed_videos)} seeds — hops={hops}, "
          f"min_views={min_views:,}\n  niche gate: {sorted(niche_words) or 'AI defaults'}\n")
    disc = crawl(seed_videos, hops=hops, min_views=min_views, expand_cap=expand_cap,
                 max_pages=max_pages, target=target, headless=headless,
                 keywords=gate_keywords, max_views=max_views)

    if LAST_RUN_FLAGGED:
        # Bailed due to flagging: keep seeds 'pending' to retry, skip the write.
        save_queue(queue)                              # persist any derived videos
        print("\n  ⚠ Stopped early (flagged). Seeds kept as 'pending' to retry "
              "later; skipping the sheet write to avoid more hits.")
        return disc

    # Flip the seeds we crawled from to 'crawled' so they don't re-seed next run.
    if consume:
        for key in seeded_keys:
            queue[key]["status"] = "crawled"
        save_queue(queue)
    else:
        print("  (--no-consume: seeds kept 'pending', not marked crawled)")

    # Source FIRST so each discovery gets a status, then print the list annotated.
    if source:
        source_discovered(disc, seed_handle="queue", keywords=gate_keywords)

    YEL, GRN, RST = "\033[33m", "\033[32m", "\033[0m"    # already-judged=yellow, NEW=green
    print(f"\n  === {len(disc)} channels discovered ===")
    print(f"  {'hop':<4}{'views':>12}  {'status':<16} channel / title")
    for rec in sorted(disc.values(), key=lambda r: -(r["views"] or 0))[:40]:
        vv = f"{rec['views']:,}" if rec["views"] is not None else "—"
        st = rec.get("status", "")
        tag = "✅ NEW" if st == "NEW" else st           # already-judged/off-niche/too-big/…
        line = (f"  h{rec['hop']:<3}{vv:>12}  {tag:<16} {rec['channel'][:22]:<23} "
                f"{rec['title'][:28]}")
        if st.startswith("judged"):
            line = YEL + line + RST                     # already checked -> yellow
        elif st == "NEW":
            line = GRN + line + RST                     # fresh passer -> green
        print(line)
    return disc


# ── PHASE 1 TEST HARNESS ──────────────────────────────────────────────────
def scrape_one(video_arg, headless=False, dump=False):
    vid = video_id_from(video_arg)
    if not vid:
        print(f"  Could not read a video id from: {video_arg!r}")
        return []
    url = f"https://www.youtube.com/watch?v={vid}"

    with make_playwright_cm() as pw:
        ctx, page = launch(pw, headless=headless)
        try:
            print(f"  Opening {url}")
            page.goto(url, wait_until="domcontentloaded")
            clear_consent(page)
            page.wait_for_timeout(1500)
            dump_chips(page)                       # show what filter chips exist
            mode = click_related_chip(page)
            print(f"  Sidebar mode: {mode}")
            rows = scrape_sidebar(page)
            if dump:
                dump_first_item(page)
            if not rows:
                debug_dump(page)
            print(f"\n  {len(rows)} sidebar neighbours:\n")
            print(f"  {'#':<3} {'views':>10}  {'channel':<28} title")
            print("  " + "-" * 88)
            for i, r in enumerate(rows, 1):
                v = f"{r['views']:,}" if r["views"] is not None else "—"
                print(f"  {i:<3} {v:>10}  {r['channel_name'][:27]:<28} "
                      f"{r['title'][:44]}")
            return rows
        finally:
            ctx.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Rabbit-hole crawler (Phase 1: scrape one sidebar)")
    ap.add_argument("video", nargs="?",
                    help="watch URL, youtu.be link, or bare 11-char video id")
    ap.add_argument("--setup", action="store_true",
                    help="one-time: open the crawler profile to sign in / configure it")
    ap.add_argument("--fresh", action="store_true",
                    help="wipe the crawler profile before running (clean slate)")
    ap.add_argument("--headless", action="store_true", help="run without a visible window")
    ap.add_argument("--dump", action="store_true",
                    help="print the first sidebar item's HTML (for selector tuning)")
    # Phase 2 — multi-hop crawl
    ap.add_argument("--crawl", action="store_true",
                    help="multi-hop rabbit-hole crawl from the seed (Phase 2)")
    ap.add_argument("--hops", type=int, default=3, help="crawl depth (default 3)")
    ap.add_argument("--min-views", type=int, default=50_000,
                    help="expansion view FLOOR (default 50000)")
    ap.add_argument("--max-views", type=int, default=None,
                    help="expansion view CEILING — skip mega-viral videos to avoid "
                         "drifting into big channels (e.g. 2000000)")
    ap.add_argument("--expand-cap", type=int, default=10,
                    help="max neighbours expanded per video (default 10)")
    ap.add_argument("--max-pages", type=int, default=None,
                    help="hard page ceiling (default: unlimited)")
    ap.add_argument("--target", type=int, default=None,
                    help="stop after N channels discovered (default: unlimited)")
    # Phase 3 — pipeline integration
    ap.add_argument("--source", action="store_true",
                    help="confirm discoveries + write passers to the sheet & queue")
    ap.add_argument("--backfill", action="store_true",
                    help="one-time: load engaged (non-Untouched) sheet passers into the queue")
    # Phase 4 — seed from the queue
    ap.add_argument("--from-queue", action="store_true",
                    help="crawl from pending seeds in the queue (the daily-run mode)")
    ap.add_argument("--seed", nargs="+", metavar="ANCHOR",
                    help="manual anchor(s): @handle, channel URL, or video URL "
                         "(mix with queue seeds via --seeds)")
    ap.add_argument("--seeds", type=int, default=None,
                    help="how many queue seeds to crawl (default 5 with --from-queue, "
                         "0 when only --seed anchors are given)")
    ap.add_argument("--random", action="store_true",
                    help="pick queue seeds at random instead of in order")
    ap.add_argument("--no-consume", action="store_true",
                    help="don't mark seeds as crawled — test without burning seeds")
    args = ap.parse_args()

    if _STEALTH_MODE:
        print(f"  (stealth: {_STEALTH_MODE} active)")
    else:
        print("  (note: playwright-stealth not installed — running without stealth patches)")
    if _HAS_GROQ:
        print(f"  (groq niche classifier: on — {GROQ_MODEL})")

    if args.setup:
        setup_profile()
        sys.exit(0)
    if args.backfill:
        backfill_queue()
        sys.exit(0)
    if args.fresh and os.path.isdir(PROFILE_DIR):
        shutil.rmtree(PROFILE_DIR)
        print(f"  Fresh profile: wiped {PROFILE_DIR}")
    if args.from_queue or args.seed:
        n = args.seeds if args.seeds is not None else (5 if args.from_queue else 0)
        crawl_from_queue(n_seeds=n, hops=args.hops, min_views=args.min_views,
                         expand_cap=args.expand_cap, max_pages=args.max_pages,
                         target=args.target, headless=args.headless, source=args.source,
                         max_views=args.max_views, consume=not args.no_consume,
                         manual_seeds=args.seed, random_pick=args.random)
        sys.exit(0)
    if not args.video:
        ap.error("a video URL or id is required "
                 "(unless using --setup / --backfill / --from-queue)")

    if args.crawl:
        seed = video_id_from(args.video)
        if not seed:
            print(f"  Could not read a video id from: {args.video!r}")
            sys.exit(1)
        print(f"  Crawling from seed {seed} — hops={args.hops}, "
              f"min_views={args.min_views:,}, expand_cap={args.expand_cap}\n")
        disc = crawl([seed], hops=args.hops, min_views=args.min_views,
                     expand_cap=args.expand_cap, max_pages=args.max_pages,
                     target=args.target, headless=args.headless,
                     max_views=args.max_views)
        print(f"\n  === {len(disc)} channels discovered ===")
        print(f"  {'hop':<4}{'views':>12}  {'channel':<28} title")
        print("  " + "-" * 84)
        for rec in sorted(disc.values(), key=lambda r: -(r["views"] or 0)):
            vv = f"{rec['views']:,}" if rec["views"] is not None else "—"
            print(f"  h{rec['hop']:<3}{vv:>12}  {rec['channel'][:27]:<28} "
                  f"{rec['title'][:38]}")
        if args.source and LAST_RUN_FLAGGED:
            print("  ⚠ Skipping --source (flagged this run — not hitting the API).")
        elif args.source:
            source_discovered(disc, seed_handle=seed)
    else:
        scrape_one(args.video, headless=args.headless, dump=args.dump)
