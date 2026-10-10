"""
YouTube Creator Sourcing v3
Niche: AI & Machine Learning
Filters: <900K subs | 80K+ avg views (last 13 vids) | >=2% engagement | >=2 uploads in last 30 days
Auto-updates Google Sheet | Skips existing creators | Pulls emails from descriptions

Every PASSING channel also gets a free sponsor-brand scan: the video data this
script already fetches is fed to brand_sourcer.py, and detected sponsors land
on the Brands tab (then auto-enriched) — 0 extra API units. Off: --no-brands
"""

import envfile  # noqa: F401  — loads .env before any os.environ reads
import argparse
import os
import urllib.parse
import urllib.request
import json
import time
import re
from datetime import datetime, timezone, timedelta

import gspread
from google.oauth2.service_account import Credentials

# ── CONFIG ──────────────────────────────────────────────────────────────────
YOUTUBE_API_KEY     = os.environ.get("YOUTUBE_API_KEY", "")   # set in env / .env
CREDS_FILE          = os.environ.get("GOOGLE_CREDS_FILE")      # service-account JSON path
SHEET_NAME          = os.environ.get("SHEET_NAME", "")         # Google Sheet (workbook) name
WORKSHEET_NAME      = os.environ.get("WORKSHEET_NAME", "Youtube")   # exact tab name
ACTIVITY_WINDOW_DAYS = 30   # look-back window for the activity check
MIN_UPLOADS_IN_WINDOW = 2   # require at least this many uploads inside that window
RESULTS_PER_KEYWORD = 50
PAGES_PER_KEYWORD   = 1   # page 2 is mostly duplicate channels — not worth the quota

# Sheet column order (matches your existing columns)
# Name | Link | Email | Tag | Outreach Status | Subs | Avg Views | Engagement %
# ("Tag" = the channel's official YouTube @handle from the API's customUrl.
#  Column F was renamed from Instagram on 2026-07-08.)
COL_NAME       = 0
COL_LINK       = 1
COL_EMAIL      = 2
COL_TAG        = 3
COL_STATUS     = 4
COL_SUBS       = 5
COL_AVG_VIEWS  = 6
COL_ENGAGEMENT = 7

# ── KEYWORDS — PRIVACY / VPN / CYBERSECURITY CLUSTER (90 total) ───────────────
# Derived from the Brands tab (Proton, Surfshark, Incogni, NordVPN, PIA, Aura,
# DeleteMe, Flare, Saily, TryHackMe...). All 90 confirmed NOT-yet-searched vs
# channel_cache.json at swap time. Previous 90 AI-vertical terms are archived in
# influncers_yt_finderScript.py.bak (2026-07-20 swap).
KEYWORDS = [
    "digital privacy guide",
    "how to be anonymous online",
    "delete your data online",
    "data broker removal",
    "degoogle your life",
    "personal cybersecurity",
    "online privacy 2025",
    "smartphone privacy",
    "OPSEC guide",
    "secure your accounts",
    "online safety tips",
    "privacy tips 2025",
    "privacy for beginners",
    "protect your privacy online",
    "internet privacy guide",
    "best privacy apps",
    "privacy focused apps",
    "stop being tracked online",
    "online tracking explained",
    "reduce digital footprint",
    "minimize digital footprint",
    "digital footprint explained",
    "remove personal info from internet",
    "opt out data brokers",
    "google privacy settings",
    "facebook privacy settings",
    "disable ad tracking",
    "location tracking privacy",
    "private search engine",
    "browser privacy guide",
    "private browsing explained",
    "metadata privacy",
    "delete google data",
    "GrapheneOS setup",
    "de-google android",
    "iphone privacy settings",
    "android privacy settings",
    "privacy phone setup",
    "Tails OS tutorial",
    "VeraCrypt tutorial",
    "full disk encryption",
    "encrypt your files",
    "Linux privacy",
    "best VPN 2025",
    "VPN explained",
    "do you need a VPN",
    "VPN for beginners",
    "how VPN works",
    "VPN setup guide",
    "free VPN dangers",
    "VPN vs proxy",
    "VPN for privacy",
    "are VPNs worth it",
    "VPN myths",
    "WireGuard VPN setup",
    "router VPN setup",
    "mobile VPN setup",
    "Mullvad VPN review",
    "VPN comparison 2025",
    "password manager setup",
    "best password manager 2025",
    "2FA setup guide",
    "passkeys explained",
    "hardware security key",
    "YubiKey setup",
    "secure passwords guide",
    "passwordless login",
    "Bitwarden tutorial",
    "KeePass tutorial",
    "secure your google account",
    "secure your email",
    "stop getting hacked",
    "account takeover prevention",
    "online scams explained",
    "avoid online scams",
    "phishing scams explained",
    "how to spot phishing",
    "crypto scams explained",
    "investment scam exposed",
    "tech support scam",
    "phone scam exposed",
    "text scam explained",
    "AI scam explained",
    "deepfake scam",
    "identity theft explained",
    "credit card fraud protection",
    "latest data breach",
    "cyberattack explained",
    "how hackers hack",
    "social engineering explained",
]

FILTERS = {
    "max_subs":       900_000,
    "min_avg_views":   70_000,
    "min_engagement":    0.02,
}

# Niche label written to each row's "Niche" column (col K) and shown in the run
# header. Un-hardcoded 2026-07-20 so it can track whichever keyword cluster is
# loaded. Set this to match the current KEYWORDS cluster before a run.
NICHE_LABEL = "Cybersecurity & Privacy"

# When True, skip channels that look female-presenting based on name/description.
# Heuristic only — see is_likely_female(). No external gender API is called.
FILTER_OUT_FEMALE = True

# When True, skip channels that don't appear to post in English.
# Uses 3 signals: YouTube language code, description character set, video titles.
# No extra API calls — all data is already fetched.
FILTER_NON_ENGLISH = True

# When True, every channel that PASSES the filters also gets its recent video
# descriptions scanned for sponsor brands (via brand_sourcer.py) and any hits
# written to the Brands tab — using the SAME playlistItems/videos calls this
# script already makes, so it costs 0 extra API units. Disable with --no-brands.
SOURCE_BRANDS = True
BRAND_VIDEOS  = 50   # videos to scan per passing channel (50 = still 1 API call)

# ── CACHE ───────────────────────────────────────────────────────────────────
# Caches both (a) which keywords were searched + the channel IDs they returned,
# and (b) channels already fully evaluated, with their pass/fail verdict.
#
# Two different expiry rules:
#   • keywords  → expire after KEYWORD_TTL_DAYS so we re-discover NEW creators.
#                 A keyword searched within this window is NOT searched again.
#   • processed → NEVER expire. Every channel we've evaluated/reached out to is
#                 remembered forever, so we never re-surface or re-contact them.
# Wipe the whole cache on demand:  python <script>.py --clear-cache
CACHE_FILE       = os.path.join(os.path.dirname(os.path.abspath(__file__)), "channel_cache.json")
KEYWORD_TTL_DAYS = 60      # don't re-run a keyword searched within this many days

# ── RABBIT-HOLE SEED QUEUE ────────────────────────────────────────────────────
# Every fresh passer is enqueued here as `pending` for the rabbit-hole crawler to
# seed from (shared with rabbit_hole_crawler.py, keyed by channel URL). Writing
# this file is cheap JSON — it does NOT import the browser stack, so a normal
# finder run always fills the queue. The crawl itself only runs with --rabbit-hole.
SEED_QUEUE_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seed_queue.json")


def enqueue_passers(pass_list, channel_video):
    """Add this run's fresh passers to seed_queue.json as `pending` seeds.

    Idempotent: a channel already in the queue (from a prior run or the crawler)
    is left untouched, so we never reset a `crawled` seed back to `pending`.
    Each new entry carries the keyword-matched video as its free `seed_video`.
    """
    try:
        q = {}
        if os.path.exists(SEED_QUEUE_FILE):
            with open(SEED_QUEUE_FILE) as f:
                q = json.load(f)
    except Exception as e:
        print(f"  Seed queue unreadable ({e}) — starting a fresh queue")
        q = {}

    added = 0
    for ch in pass_list:
        url = ch["url"]
        if url in q:
            continue   # already queued/crawled — don't disturb its status
        q[url] = {
            "status":        "pending",
            "seed_video":    channel_video.get(ch.get("cid", ""), ""),
            "niche_context": ch.get("keyword", ""),
            "name":          ch["name"],
            "link":          url,
        }
        added += 1

    try:
        with open(SEED_QUEUE_FILE, "w") as f:
            json.dump(q, f, indent=2)
    except Exception as e:
        print(f"  Could not write seed queue: {e}")
        return 0
    return added

# ── CACHE HELPERS ────────────────────────────────────────────────────────────
def load_cache():
    """Load the cache file. Shape: {'keywords': {...}, 'processed': {...}}."""
    if not os.path.exists(CACHE_FILE):
        return {"keywords": {}, "processed": {}}
    try:
        with open(CACHE_FILE, "r") as f:
            data = json.load(f)
        data.setdefault("keywords", {})
        data.setdefault("processed", {})
        return data
    except Exception as e:
        print(f"  Cache unreadable ({e}) — starting fresh")
        return {"keywords": {}, "processed": {}}


def save_cache(cache):
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump(cache, f)
    except Exception as e:
        print(f"  Could not write cache: {e}")


def clear_cache():
    if os.path.exists(CACHE_FILE):
        os.remove(CACHE_FILE)
        print(f"  Cache cleared: {CACHE_FILE}")
    else:
        print("  No cache file to clear.")


def days_since(ts_iso):
    """How many days ago an ISO timestamp was. Returns None if unparseable."""
    try:
        ts = datetime.fromisoformat(ts_iso)
    except Exception:
        return None
    return (datetime.now(timezone.utc) - ts).days


def keyword_is_fresh(ts_iso):
    """True if a keyword was searched within KEYWORD_TTL_DAYS."""
    # ── EXPIRATION DISABLED (2026-07-08) ──────────────────────────────────
    # Keywords now never expire: once searched, a keyword is treated as fresh
    # forever and is never re-run (max API-quota savings). Channels are NOT
    # affected — the 'processed' cache still remembers every channel forever.
    # To re-enable the 60-day expiry: delete the return line below and
    # uncomment the two original lines.
    return ts_iso not in (None, "")
    # d = days_since(ts_iso)
    # return d is not None and d < KEYWORD_TTL_DAYS


def prune_cache(cache):
    """Drop stale KEYWORD entries so we rediscover new creators.

    Processed channels are intentionally NEVER pruned — we remember every
    channel we've evaluated/contacted forever.
    """
    stale = [k for k, v in cache["keywords"].items()
             if not keyword_is_fresh(v.get("ts", ""))]
    for k in stale:
        del cache["keywords"][k]
    return cache


# ── YOUTUBE HELPERS ─────────────────────────────────────────────────────────
HTTP_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

def yt_get(endpoint, params):
    params["key"] = YOUTUBE_API_KEY
    url = f"https://www.googleapis.com/youtube/v3/{endpoint}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": HTTP_UA})
    for attempt in range(3):
        try:
            # timeout is the fix for the silent multi-hour hang: a stuck socket
            # now raises after 30s and is retried below instead of blocking forever.
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = 15 * (attempt + 1)
                print(f"  Rate limited — waiting {wait}s...")
                time.sleep(wait)
            else:
                print(f"  API error {e.code}")
                return {}
        except Exception as e:
            # Timeouts / transient socket errors: back off and retry rather than
            # hang or bail on the first blip. (Never fail silently — we print it.)
            wait = 5 * (attempt + 1)
            print(f"  Network error ({e}) — retry in {wait}s...")
            time.sleep(wait)
    print(f"  Giving up on {endpoint} after retries")
    return {}


def extract_email(text):
    """Pull first email address found in text."""
    match = re.search(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", text)
    return match.group(0) if match else ""


def channel_tag(snippet):
    """The channel's official YouTube @handle, from the API's customUrl field.
    (Replaced the old Instagram-from-description scraper — it mostly caught junk,
    and the API hands us the real handle for free in the same channels call.)"""
    tag = (snippet.get("customUrl") or "").strip()
    if tag and not tag.startswith("@"):
        tag = "@" + tag
    return tag


# Words/patterns that strongly suggest a female-presenting creator.
FEMALE_PRONOUNS = re.compile(r"\b(she|her|hers|herself)\b", re.IGNORECASE)
FEMALE_NOUNS    = re.compile(
    r"\b(girl|woman|women|female|lady|ladies|mom|mum|mother|wife|"
    r"daughter|sister|queen|miss|mrs|ms\.?|gal)\b",
    re.IGNORECASE,
)

def is_likely_female(name, description=""):
    """Heuristic guess at a female-presenting creator from name + description.

    Returns True only on a reasonably strong signal (pronouns or gendered nouns).
    This is best-effort — it will miss some and occasionally misfire.
    """
    text = f"{name}\n{description}"
    if FEMALE_PRONOUNS.search(text):
        return True
    if FEMALE_NOUNS.search(text):
        return True
    return False


def is_likely_english(language_code, description, video_titles):
    """Three-signal English detection — no extra API calls.

    Signal 1: YouTube language code (strongest — if set, trust it).
    Signal 2: Video titles character set (best real-world signal).
    Signal 3: Description character set (fallback).

    Returns True if the channel appears to be English.
    """
    # Signal 1 — explicit language code set by the creator
    if language_code:
        return language_code.lower().startswith("en")

    # Signal 2 — scan video titles for non-Latin characters
    # Non-English scripts (Arabic, Chinese, Korean, Japanese, Cyrillic, etc.)
    # use character ranges well outside basic Latin. If >20% of title chars
    # are non-Latin, treat as non-English.
    NON_LATIN = re.compile(r"[^\x00-\x7FÀ-ɏḀ-ỿ]")
    titles_text = " ".join(video_titles)
    if titles_text:
        non_latin = len(NON_LATIN.findall(titles_text))
        if non_latin / max(len(titles_text), 1) > 0.20:
            return False
        # If titles are clean Latin — high confidence it's English
        if len(titles_text) > 30:
            return True

    # Signal 3 — fallback to description character check
    if description:
        non_latin = len(NON_LATIN.findall(description))
        if non_latin / max(len(description), 1) > 0.20:
            return False

    # No strong signal either way — assume English to avoid false drops
    return True


def search_channels_for_keyword(kw):
    channels = {}   # channel_id -> channel title
    videos   = {}   # channel_id -> the keyword-matched video id (free crawl seed)
    page_token = None
    for _ in range(PAGES_PER_KEYWORD):
        params = {"part": "snippet", "type": "video", "maxResults": RESULTS_PER_KEYWORD, "q": kw}
        if page_token:
            params["pageToken"] = page_token
        data = yt_get("search", params)
        for item in data.get("items", []):
            ch_id = item["snippet"]["channelId"]
            channels[ch_id] = item["snippet"]["channelTitle"]
            # Keep the first (highest-ranked) matched video per channel — this is
            # the on-niche anchor the rabbit-hole crawler seeds from, for free.
            vid = (item.get("id") or {}).get("videoId", "")
            if vid and ch_id not in videos:
                videos[ch_id] = vid
        page_token = data.get("nextPageToken")
        if not page_token:
            break
        time.sleep(1)
    return channels, videos


def get_base_stats(ids):
    data = yt_get("channels", {
        "part": "statistics,snippet,contentDetails",
        "id": ",".join(ids),
    })
    result = {}
    for i in data.get("items", []):
        desc = i["snippet"].get("description", "")
        result[i["id"]] = {
            "name":             i["snippet"]["title"],
            "subs":             int(i["statistics"].get("subscriberCount", 0)),
            "uploads_playlist": i["contentDetails"]["relatedPlaylists"]["uploads"],
            "email":            extract_email(desc),
            "tag":              channel_tag(i["snippet"]),
            "description":      desc,
            "language":         i["snippet"].get("defaultLanguage", "")
                                or i["snippet"].get("defaultAudioLanguage", ""),
        }
    return result


def get_recent_video_ids(uploads_playlist, n=13):
    data = yt_get("playlistItems", {
        "part": "contentDetails,snippet",
        "playlistId": uploads_playlist,
        "maxResults": n,
    })
    items = data.get("items", [])
    video_ids  = [i["contentDetails"]["videoId"] for i in items]
    video_titles = [i["snippet"].get("title", "") for i in items]
    # Collect the publish date of EVERY upload we pulled, so callers can both
    # find the most recent one AND count how many landed inside a time window.
    publish_dates = []
    for i in items:
        s = i["snippet"].get("publishedAt", "")
        if s:
            publish_dates.append(datetime.fromisoformat(s.replace("Z", "+00:00")))
    latest_date = publish_dates[0] if publish_dates else None
    return video_ids, latest_date, video_titles, publish_dates


def parse_duration_seconds(duration):
    """Convert ISO 8601 duration (PT1M30S) to total seconds."""
    import re
    match = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", duration)
    if not match:
        return 0
    hours   = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    return hours * 3600 + minutes * 60 + seconds

def get_video_stats(vids, stat_limit=13):
    """Fetch stats + snippets in ONE videos.list call (units are per-call, not
    per-part, so adding snippet is free).

    Returns (stats, videos, longform_dates):
      stats  — view/like/comment counts for the `stat_limit` most recent videos,
               Shorts excluded (same evaluation the script has always done)
      videos — (title, description) for EVERY id passed, ready for
               brand_sourcer.detect_sponsors() at zero extra API cost
      longform_dates — publish datetimes of every LONG-FORM video (>180s),
               used by the activity check so Shorts don't count as uploads
    """
    data = yt_get("videos", {"part": "snippet,statistics,contentDetails",
                             "id": ",".join(vids)})
    by_id = {i["id"]: i for i in data.get("items", [])}
    stats, videos, longform_dates = [], [], []
    for vid in vids:                      # request order, not response order
        i = by_id.get(vid)
        if not i:
            continue
        sn = i.get("snippet", {})
        videos.append((sn.get("title", ""), sn.get("description", "")))
        # Record the publish date only for long-form videos (Shorts excluded),
        # so the activity check counts real uploads — not Shorts.
        duration = i.get("contentDetails", {}).get("duration", "PT0S")
        if parse_duration_seconds(duration) > 180:
            s = sn.get("publishedAt", "")
            if s:
                longform_dates.append(datetime.fromisoformat(s.replace("Z", "+00:00")))
    # Stats come only from the `stat_limit` most recent videos (Shorts excluded),
    # so bumping the fetch to 50 for brand-scanning never changes the verdict.
    for vid in vids[:stat_limit]:
        i = by_id.get(vid)
        if not i:
            continue
        duration = i.get("contentDetails", {}).get("duration", "PT0S")
        if parse_duration_seconds(duration) <= 180:
            continue  # skip Shorts
        s = i["statistics"]
        stats.append({
            "v": int(s.get("viewCount", 0)),
            "l": int(s.get("likeCount", 0)),
            "c": int(s.get("commentCount", 0)),
        })
    return stats, videos, longform_dates


# ── GOOGLE SHEETS ────────────────────────────────────────────────────────────
def connect_sheet():
    if not CREDS_FILE or not SHEET_NAME:
        raise RuntimeError("Set GOOGLE_CREDS_FILE and SHEET_NAME (see .env.example).")
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(CREDS_FILE, scopes=scopes)
    gc    = gspread.authorize(creds)
    sh    = gc.open(SHEET_NAME)
    ws    = sh.worksheet(WORKSHEET_NAME)
    return ws


def get_existing_data(ws):
    """Return sets of existing names and URLs (columns C and D, index 2 and 3)."""
    try:
        all_rows = ws.get_all_values()
        existing_names = set()
        existing_urls  = set()
        for row in all_rows[1:]:
            if len(row) > 2 and row[2].strip():
                existing_names.add(row[2].strip().lower())
            if len(row) > 3 and row[3].strip():
                existing_urls.add(row[3].strip())
        return existing_names, existing_urls
    except Exception as e:
        print(f"  Could not read existing sheet rows: {e}")
        return set(), set()


def find_next_empty_row(ws):
    """Find the first row AFTER all existing data — always appends, never overwrites."""
    col_c = ws.col_values(3)  # 1-indexed, col 3 = C
    # Walk from the bottom up to find the last row that actually has a value.
    # This handles gaps in the sheet without accidentally writing into them.
    for i in range(len(col_c) - 1, -1, -1):
        if col_c[i].strip():
            return i + 2  # i is 0-indexed, +1 for 1-indexed, +1 to go one past
    return 4  # sheet is empty — start at row 4 (below headers)


def write_to_sheet(ws, channels):
    """Write new channels starting from column C, first empty row."""
    if not channels:
        return 0
    start_row = find_next_empty_row(ws)
    rows = []
    for ch in channels:
        # Columns C-O: Name|Link|Email|Tag|Outreach Status|Subs|Avg Views|Eng%|
        #              Niche|Keyword|Outreach Support|Notes|Sponsers
        # Outreach Status (col G): mark "ready" the moment we have an email to
        # draft to — that's the flag the drafter filters on. No email yet =
        # "Untouched" (still needs email_finder before it can be drafted).
        has_email = bool(str(ch.get("email", "")).strip())
        rows.append([
            ch["name"],
            ch["url"],
            ch["email"],
            ch["tag"],
            "ready" if has_email else "Untouched",
            ch["subs"],
            ch["avg_views"],
            f"{ch['engagement']}%",
            NICHE_LABEL,
            ch.get("keyword", ""),
            "",                       # M Outreach Support — filled by hand
            "",                       # N Notes — filled by hand
            ch.get("sponsors", ""),   # O Sponsers — from the free brand scan
        ])
    ws.update(values=rows, range_name=f"C{start_row}", value_input_option="USER_ENTERED")
    print(f"  Writing starting at row {start_row}")
    return len(rows)


# ── MAIN ────────────────────────────────────────────────────────────────────
def main(rabbit_hole=False):
    print("=" * 70)
    print("  Creator Sourcing v3")
    print(f"  Niche: {NICHE_LABEL}")
    print("=" * 70)

    # Connect to sheet first so we fail fast if creds are wrong
    print("\nConnecting to Google Sheets...")
    try:
        ws = connect_sheet()
        existing_names, existing_urls = get_existing_data(ws)
        print(f"  Sheet connected — {len(existing_names)} creators already tracked")
    except Exception as e:
        print(f"  ❌ Sheet connection failed: {e}")
        print("  Check GOOGLE_CREDS_FILE / SHEET_NAME and that the sheet is shared with the service account.")
        return

    # Load + prune the cache (only stale KEYWORDS are dropped; processed kept forever)
    cache = prune_cache(load_cache())
    print(f"  Cache: {len(cache['keywords'])} recent keywords (last {KEYWORD_TTL_DAYS}d), "
          f"{len(cache['processed'])} channels remembered (never expire)")

    # Report which keywords will be SKIPPED because they were searched recently
    already_searched = []
    for kw in KEYWORDS:
        cached = cache["keywords"].get(kw)
        if cached and keyword_is_fresh(cached.get("ts", "")):
            d = days_since(cached.get("ts", ""))
            already_searched.append((kw, d, len(cached.get("channels", {}))))
    if already_searched:
        print(f"\n  ⏭  {len(already_searched)} keyword(s) searched within the last "
              f"{KEYWORD_TTL_DAYS} days — these will be SKIPPED (no quota spent):")
        for kw, d, n in already_searched:
            print(f"       • {kw}  —  {d}d ago, {n} channels cached")

    # Step 1: Search — reuse recent keyword results, only pay for new/expired keywords
    print(f"\n[1/4] Searching {len(KEYWORDS)} keywords...")
    all_channels = {}      # channel_id -> name
    channel_keyword = {}   # channel_id -> first keyword that found it
    channel_video = {}     # channel_id -> keyword-matched video id (crawl seed)
    now_iso = datetime.now(timezone.utc).isoformat()
    searched, from_cache = 0, 0
    for i, kw in enumerate(KEYWORDS):
        cached = cache["keywords"].get(kw)
        if cached and keyword_is_fresh(cached.get("ts", "")):
            found = cached.get("channels", {})
            found_videos = cached.get("videos", {})   # absent on pre-2026-07-20 cache
            from_cache += 1
            d = days_since(cached.get("ts", ""))
            print(f"  [{i+1}/{len(KEYWORDS)}] {kw} — skipped ({d}d ago, {len(found)} channels)")
        else:
            print(f"  [{i+1}/{len(KEYWORDS)}] {kw}")
            found, found_videos = search_channels_for_keyword(kw)
            cache["keywords"][kw] = {"ts": now_iso, "channels": found, "videos": found_videos}
            searched += 1
            time.sleep(1)
        for cid, cname in found.items():
            if cid not in all_channels:
                all_channels[cid] = cname
                channel_keyword[cid] = kw
                if found_videos.get(cid):
                    channel_video[cid] = found_videos[cid]
    save_cache(cache)
    print(f"\n  Keywords searched live: {searched} | skipped (searched <{KEYWORD_TTL_DAYS}d ago): {from_cache}")
    print(f"  Unique channels discovered: {len(all_channels)}")

    # Dedup BEFORE spending stats quota — drop channels already in the sheet.
    # We match on name (we have it from search) and on channel URL.
    skipped_existing = 0
    for cid in list(all_channels.keys()):
        name = all_channels[cid]
        url  = f"https://youtube.com/channel/{cid}"
        if url in existing_urls or name.lower() in existing_names:
            del all_channels[cid]
            channel_keyword.pop(cid, None)
            skipped_existing += 1
    print(f"  Skipped {skipped_existing} already-tracked channels (no API spent on them)")

    # Also skip channels we've EVER evaluated (cached verdict — never expires).
    # These may never have reached the sheet (they failed a filter) so the sheet
    # dedup misses them. Remembering them forever means we never re-evaluate or
    # re-contact anyone we've already handled.
    skipped_processed = 0
    for cid in list(all_channels.keys()):
        if cid in cache["processed"]:
            del all_channels[cid]
            channel_keyword.pop(cid, None)
            skipped_processed += 1
    print(f"  Skipped {skipped_processed} previously-evaluated channels (remembered forever)")
    print(f"  Channels to look up: {len(all_channels)}")

    # Step 2: Base stats
    print("\n[2/4] Fetching channel stats...")
    channel_ids = list(all_channels.keys())
    base_info = {}
    for i in range(0, len(channel_ids), 50):
        base_info.update(get_base_stats(channel_ids[i:i+50]))
        time.sleep(0.5)

    sub_filtered = {
        cid: info for cid, info in base_info.items()
        if 1000 < info["subs"] < FILTERS["max_subs"]
    }
    print(f"  After sub filter: {len(sub_filtered)} channels")

    if FILTER_OUT_FEMALE:
        before = len(sub_filtered)
        sub_filtered = {
            cid: info for cid, info in sub_filtered.items()
            if not is_likely_female(info["name"], info.get("description", ""))
        }
        print(f"  After gender filter: {len(sub_filtered)} channels "
              f"({before - len(sub_filtered)} skipped)")

    if FILTER_NON_ENGLISH:
        before = len(sub_filtered)
        sub_filtered = {
            cid: info for cid, info in sub_filtered.items()
            if is_likely_english(
                info.get("language", ""),
                info.get("description", ""),
                [],   # titles not available yet at this stage — checked again in step 3
            )
        }
        print(f"  After language filter: {len(sub_filtered)} channels "
              f"({before - len(sub_filtered)} skipped)")

    # Step 3: Enrich + filter
    print("\n[3/4] Enriching channels...")
    now = datetime.now(timezone.utc)
    pass_list = []
    brand_pending = []   # (row, dedupe_key) tuples for the Brands tab, free by-product
    n_fetch = BRAND_VIDEOS if SOURCE_BRANDS else 13   # 13 vs 50 = same API cost (1 call each)

    for n, (cid, info) in enumerate(sub_filtered.items()):
        url = f"https://youtube.com/channel/{cid}"

        # Persist verdicts periodically so an interruption mid-enrichment doesn't
        # throw away hours of evaluation — the next run resumes via the cache
        # (already-evaluated channels are skipped before spending any quota).
        if n and n % 50 == 0:
            save_cache(cache)
            print(f"  … progress saved ({n}/{len(sub_filtered)} evaluated)")

        # Skip if name OR URL already in sheet
        if url in existing_urls or info["name"].lower() in existing_names:
            print(f"  [{n+1}/{len(sub_filtered)}] {info['name']} — already in sheet, skipping")
            continue

        def remember(verdict):
            cache["processed"][cid] = {"ts": now_iso, "verdict": verdict}

        print(f"  [{n+1}/{len(sub_filtered)}] {info['name']}", end="", flush=True)
        try:
            vids, latest_date, titles, publish_dates = get_recent_video_ids(info["uploads_playlist"], n_fetch)
            if not vids:
                print(" — no videos")
                remember("no_videos")
                continue

            # Title-based language check (strongest signal — runs once we have titles)
            # Only the 13 most recent titles, matching the pre-brands behavior.
            if FILTER_NON_ENGLISH and not is_likely_english(
                info.get("language", ""), info.get("description", ""), titles[:13]
            ):
                print(" — ⛔ non-English")
                remember("non_english")
                continue

            # Fast-fail (cheap): total uploads (incl. Shorts) is an upper bound on
            # long-form uploads, so if even that is below the threshold we skip
            # BEFORE spending the videos.list call. The precise long-form-only
            # check runs right after we have durations, below.
            total_in_window = sum(
                1 for d in publish_dates if (now - d).days <= ACTIVITY_WINDOW_DAYS
            )
            if total_in_window < MIN_UPLOADS_IN_WINDOW:
                print(f" — ⛔ inactive ({total_in_window} upload(s) in {ACTIVITY_WINDOW_DAYS}d)")
                remember("inactive")
                continue

            stats, videos_meta, longform_dates = get_video_stats(vids, stat_limit=13)
            if not stats:
                print(" — no stats")
                remember("no_stats")
                continue

            # Precise activity check: require MIN_UPLOADS_IN_WINDOW LONG-FORM uploads
            # (Shorts excluded) inside the last ACTIVITY_WINDOW_DAYS.
            longform_in_window = sum(
                1 for d in longform_dates if (now - d).days <= ACTIVITY_WINDOW_DAYS
            )
            if longform_in_window < MIN_UPLOADS_IN_WINDOW:
                print(f" — ⛔ inactive ({longform_in_window} long-form in {ACTIVITY_WINDOW_DAYS}d)")
                remember("inactive")
                continue

            tv  = sum(s["v"] for s in stats)
            avg = tv / len(stats)
            eng = (sum(s["l"] for s in stats) + sum(s["c"] for s in stats)) / tv if tv else 0
            last_posted_days = (now - latest_date).days

            print(f" | {avg:,.0f} avg views | {eng*100:.2f}% eng | {last_posted_days}d ago", end="")

            if avg >= FILTERS["min_avg_views"] and eng >= FILTERS["min_engagement"]:
                print(" ✅")
                remember("pass")
                pass_list.append({
                    "name":       info["name"],
                    "url":        url,
                    "cid":        cid,
                    "email":      info["email"],
                    "tag":        info["tag"],
                    "subs":       info["subs"],
                    "avg_views":  round(avg),
                    "engagement": round(eng * 100, 2),
                    "last_posted_days_ago": last_posted_days,
                    "keyword":    channel_keyword.get(cid, ""),
                })
                # Brand sourcing, free: scan the descriptions we ALREADY fetched
                # for sponsor links/codes. Zero extra API calls. Sponsor names
                # also land in the creator's Sponsers column (O) on the Youtube tab.
                if SOURCE_BRANDS:
                    try:
                        import brand_sourcer   # lazy — only needed on a pass
                        new_rows = brand_sourcer.rows_from_descs(
                            videos_meta, info["name"], info["subs"])
                        brand_pending += new_rows
                        pass_list[-1]["sponsors"] = ", ".join(
                            rec["brand"] for rec, _ in new_rows)
                    except Exception as be:
                        print(f"    (brand scan skipped: {be})")
            else:
                print()
                remember("below_threshold")
            time.sleep(0.5)
        except Exception as e:
            print(f" — error: {e}")

    # Persist verdicts so future runs skip these channels until they go stale
    save_cache(cache)

    # Step 4: Write to sheet
    pass_list.sort(key=lambda x: x["engagement"], reverse=True)

    print(f"\n[4/4] Writing {len(pass_list)} new channels to sheet...")
    added = write_to_sheet(ws, pass_list)
    print(f"  ✅ {added} rows added to '{WORKSHEET_NAME}' tab")

    # Brands by-product: write sponsor brands detected on passing channels.
    # A Brands-tab hiccup must never kill a finder run, hence the broad except.
    if SOURCE_BRANDS and brand_pending:
        print(f"\n[brands] Writing {len(brand_pending)} sponsor brand(s) found on passing channels...")
        try:
            import brand_sourcer
            brand_sourcer.save_rows(brand_pending)
        except Exception as e:
            print(f"  Brands write skipped ({e}). Rows are lost for this run — "
                  f"re-run 'python3 brand_sourcer.py <channel>' on the new creators if needed.")

    # Print summary
    print(f"\n{'='*70}")
    print(f"  PASS LIST — {len(pass_list)} new channels added")
    print(f"{'='*70}\n")
    print(f"{'#':<4} {'Channel':<32} {'Subs':>10} {'Avg Views':>12} {'Eng':>8}  Email")
    print("-" * 90)
    for i, ch in enumerate(pass_list, 1):
        print(
            f"{i:<4} {ch['name'][:31]:<32} "
            f"{ch['subs']:>10,} "
            f"{ch['avg_views']:>12,} "
            f"{ch['engagement']:>7.2f}%  "
            f"{ch['email'] or '—'}"
        )
    print("=" * 70)

    # Enqueue this run's fresh passers for the rabbit-hole crawler. This happens
    # AFTER the sheet write + summary above, so batch #1 is fully on the sheet and
    # ready to email before anything else runs.
    n_queued = enqueue_passers(pass_list, channel_video)
    print(f"\n  🐇 {n_queued} new passer(s) added to the rabbit-hole seed queue "
          f"({SEED_QUEUE_FILE}) as 'pending'.")

    # Opt-in crawl. Only now — with batch #1 safely written — do we (optionally)
    # kick off the browser crawl, which is slow and may pause for a CAPTCHA.
    if rabbit_hole:
        print("\n[rabbit-hole] --rabbit-hole set — starting the crawl off the pending queue...")
        try:
            import rabbit_hole_crawler as rh   # lazy: pulls in the browser stack
            rh.crawl_from_queue(source=True)
        except Exception as e:
            print(f"  Rabbit-hole crawl skipped ({e}). The queue is saved — "
                  f"run 'python3 rabbit_hole_crawler.py --from-queue --source' later.")
    else:
        print("  (crawl not run — pass --rabbit-hole to crawl off these seeds now, "
              "or run 'python3 rabbit_hole_crawler.py --from-queue --source' later.)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="YouTube creator sourcing")
    parser.add_argument("--clear-cache", action="store_true",
                        help="wipe the channel cache before running (full re-search)")
    parser.add_argument("--no-brands", action="store_true",
                        help="skip the free sponsor-brand scan on passing channels")
    parser.add_argument("--rabbit-hole", action="store_true",
                        help="after writing batch #1 to the sheet, crawl the rabbit-hole "
                             "seed queue (browser-based, slow, may pause for a CAPTCHA)")
    args = parser.parse_args()
    if args.clear_cache:
        clear_cache()
    if args.no_brands:
        SOURCE_BRANDS = False
    main(rabbit_hole=args.rabbit_hole)