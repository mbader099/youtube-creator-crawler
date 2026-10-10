"""
Brand Sourcer
=============
Find the BRANDS that already sponsor a YouTube creator, straight from the
YouTube Data API — no agent credits, no browser. Pull recent video
descriptions, detect sponsor links + promo codes, flag recurring sponsors,
and log them to your Google Sheet (a "Brands" tab in your existing workbook).

This is the cheap, scriptable version of the brand-outreach skill's "Mode A —
Sourcing". Run it on as many creators as you want for basically free (the
YouTube API gives ~10,000 free units/day; each creator costs ~3-5 units).

Auth (env vars — see .env.example):
    - YouTube:  YOUTUBE_API_KEY
    - Sheets:   GOOGLE_CREDS_FILE (service-account JSON path) + SHEET_NAME

Install (one time, if not already): the same libraries your finder uses —
    pip3 install gspread google-auth --break-system-packages --user
    pip3 install openpyxl --break-system-packages --user   # only for --import-xlsx / --local

Run (sourcing NOW also auto-enriches — one command does it all):
    python3 brand_sourcer.py @SomeCreator                        # source + enrich one creator
    python3 brand_sourcer.py @creator1 @creator2 @creator3      # many at once
    python3 brand_sourcer.py @creator --videos 40               # scan more videos
    python3 brand_sourcer.py @creator --no-enrich               # source only, skip research
    python3 brand_sourcer.py @creator --dry-run                 # show, don't write
    python3 brand_sourcer.py --clean                            # sweep junk OUT of the Brands tab
    python3 brand_sourcer.py --clean --dry-run                  # preview the sweep first
    python3 brand_sourcer.py --fix-url 'Pikzels' pikzels.com    # correct a dead/wrong brand URL
    python3 brand_sourcer.py --import-xlsx brand-tracking.xlsx   # one-time migrate
    python3 brand_sourcer.py @creator --local backup.xlsx       # write to a local file instead

Junk filter (always on): affiliate/shortener domains, known non-sponsor sites
(news, wikis, socials), gov/edu TLDs, and DEAD domains (DNS check) are skipped
before they ever hit the sheet. --no-dns skips the liveness check.

Writes one row per brand to the "Brands" tab, deduped. Recurring sponsors
(seen in 2+ videos) are tagged GOLDEN NUGGET — pitch those first. Straight after
writing, it researches each new brand (objective + what they sell) via
brand_enricher, so every row lands pitch-ready. Turn that off with --no-enrich.

NOTE: the finder (influncers_yt_finderScript.py) now runs this automatically on
every channel that passes its filters, at zero API cost (it reuses its own
fetches via rows_from_descs + save_rows). Run this standalone only for creators
that are ALREADY in your sheet, or ones the finder didn't discover.
"""

import envfile  # noqa: F401  — loads .env before any os.environ reads
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime

try:
    import gspread
    from google.oauth2.service_account import Credentials
except ImportError:
    gspread = None

try:
    from openpyxl import load_workbook
    from openpyxl import Workbook
except ImportError:
    load_workbook = None

# ── CONFIG (env only — see .env.example; shared with the finder script) ────────
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")
CREDS_FILE = os.environ.get("GOOGLE_CREDS_FILE")
SHEET_NAME = os.environ.get("SHEET_NAME", "")
BRANDS_WS = "Brands"            # the tab brands are written to (created if missing)
DEFAULT_VIDEOS = 30             # how many recent videos to scan per channel
NICHE = "AI / Tech"            # default niche label written to the sheet

SIGHT_WS = "Sightings"          # the log: one row every time a brand is caught sponsoring

# Two-tab model. BRANDS = deduped master (one row per brand). SIGHTINGS = the log.
# Removed from the old layout: Tie-in Influencer + Best-fit Creator (moved to Sightings),
# Brand Size, Follow-up #, Next Action Date, Deal Type, Est. Deal Value, Est. Margin.
BRAND_HEADERS = ["Brand", "Niche", "Brand Objective", "Decision-Maker", "Title",
                 "Contact Email", "Alt Contact 1", "Alt Contact 2", "Status",
                 "Last Contact", "Notes", "Brand URL", "Times Spotted", "# Influencers"]
SIGHT_HEADERS = ["Brand", "Influencer", "Subs", "Date Spotted",
                 "Sponsor Link / Source", "Confidence", "Recurring?", "Notes"]

# ── APOLLO CONTACT POLICY (read by Claude/humans — this script doesn't call Apollo)
# - Reveal ONE contact per brand by default (1 lead credit each; free plan = 85/mo).
# - Qualify the brand BEFORE revealing — searching names/titles is free.
# - Priority: Influencer/Creator Partnerships > Head of Marketing > founder (small brands).
# - Log backup contact NAMES (unrevealed = free) in Notes; reveal #2 only after
#   the 6-step follow-up sequence exhausts on contact #1.
# - Credits CAN be increased (paid plan approved if qualified brands outpace 85/mo) —
#   check with the account owner before treating the cap as a blocker.

# The live sheet uses a European locale, where the formula argument separator is
# ';' (not ','). Formulas written via gspread USER_ENTERED are parsed with the
# sheet's locale, so count formulas must use this. If the sheet is ever switched
# to a US locale, change this back to ",".
FORMULA_SEP = ";"

# Affiliate/redirect networks + shorteners: never a real payable brand, always skip.
AFFILIATE_DOMAINS = {"sjv.io", "pxf.io", "itl.ink", "sbird.co", "tinyurl.com",
                     "a.co", "app.link", "bit.ly", "lnk.to", "t.co", "goo.gl",
                     "tiny.cc", "rb.gy", "cutt.ly", "buff.ly", "ow.ly",
                     "rebrand.ly", "smarturl.it", "shorturl.at", "linkfly.to",
                     "hyperfollow.com", "fanlink.tv", "snip.ly", "amzn.to",
                     # sponsor-link aggregators — always wrap a real destination
                     "sponsr.is", "geni.us", "lmg.gg"}

# Canonical display names — dedupe variants + fix casing so counts match.
CANON = {"joindeleteme": "DeleteMe", "nordvpn": "NordVPN", "buyraycon": "Raycon",
         "ground": "Ground News", "groundnews": "Ground News", "seowriting": "SEO Writing",
         "seo writing": "SEO Writing", "lemonslice": "LemonSlice", "lemon slice": "LemonSlice",
         "jogg": "JoggAI", "joggai": "JoggAI", "tempo": "Tempo", "tempo labs": "Tempo",
         "seeedstudio": "Seeed Studio", "buildwithparallel": "Parallel", "piavpn": "PIA VPN",
         "epidemicsound": "Epidemic Sound",
         "controlai": "ControlAI", "higgsfield ai": "Higgsfield AI", "heygen": "HeyGen"}


def canon_name(name):
    """Trim, collapse spaces, and map known variants to one canonical brand name."""
    clean = re.sub(r"\s+", " ", str(name)).strip()
    return CANON.get(clean.lower(), clean)

# Domains that are NEVER a brand sponsor — creator's own links, socials, music,
# paste/source dumps, payment + community tools. Anything else is a candidate.
NOISE_DOMAINS = {
    "youtube.com", "youtu.be", "m.youtube.com", "www.youtube.com",
    "instagram.com", "twitter.com", "x.com", "tiktok.com", "facebook.com",
    "threads.net", "linkedin.com", "reddit.com", "t.me", "telegram.me",
    "discord.gg", "discord.com", "twitch.tv", "patreon.com", "ko-fi.com",
    "buymeacoffee.com", "paypal.com", "paypal.me", "cash.app", "venmo.com",
    "linktr.ee", "beacons.ai", "bio.link", "carrd.co", "gumroad.com",
    "skool.com", "substack.com", "medium.com", "github.com", "gitlab.com",
    "pastebin.com", "docs.google.com", "drive.google.com", "forms.gle",
    "open.spotify.com", "spotify.com", "soundcloud.com", "music.apple.com",
    "apple.com", "amazon.com", "wikipedia.org",
    # epidemicsound.com deliberately NOT here — it's one of the most common real
    # YouTube sponsors; the sponsor-language detector judges it like any brand.
    # — News & media (citation links, never sponsors) — edit freely —
    "nbcnews.com", "washingtonpost.com", "wsj.com", "nytimes.com", "inquirer.com",
    "inquirer.net", "harpers.org", "semafor.com", "business-standard.com",
    "axios.com", "theintercept.com", "icij.org", "coindesk.com", "reuters.com",
    "apnews.com", "bbc.com", "theguardian.com", "bloomberg.com", "cnbc.com",
    "forbes.com", "politico.com", "cnn.com", "ft.com", "economist.com",
    "propublica.org", "vox.com", "npr.org", "aljazeera.com",
    # — Tech press & reference (citation links) —
    "vice.com", "theverge.com", "techcrunch.com", "wired.com", "arstechnica.com",
    "engadget.com", "gizmodo.com", "businessinsider.com", "fortune.com",
    "digitaltrends.com", "extremetech.com", "9to5mac.com", "9to5google.com",
    "krebsonsecurity.com", "hackread.com", "bleepingcomputer.com", "mashable.com",
    "macrumors.com", "androidpolice.com", "bostonglobe.com", "archive.org",
    "investopedia.com", "thurrott.com", "tomshardware.com",
    # (sponsor-link aggregators sponsr.is/geni.us/lmg.gg moved to AFFILIATE_DOMAINS
    #  so their redirects get UNWRAPPED to the real brand instead of dropped)
    # — Government / NGO / reference (never sponsors) —
    "europa.eu", "cbp.gov", "nih.gov", "house.gov", "senate.gov", "coe.int",
    "victimsofcommunism.org", "gatewayfoundation.org", "psychologytoday.com",
    "who.int", "un.org", "gov.uk", "irs.gov", "fbi.gov", "justice.gov",
    "state.gov", "archives.gov", "loc.gov",
    # — Generic platforms (bare domain is never a brand deal) —
    "google.com", "google.org", "blogspot.com", "wordpress.com", "wixsite.com",
    "imdb.com", "goodreads.com", "quora.com",
    # — Reference / encyclopedia (citation links, never sponsors) —
    "ballotpedia.org", "britannica.com", "snopes.com", "wikimedia.org",
    "wiktionary.org", "fandom.com", "stackexchange.com", "stackoverflow.com",
}

# TLDs that are never a payable sponsor (reference/government/military).
JUNK_TLD_RE = re.compile(r"\.(gov|mil|edu|int)$", re.I)


# ── JUNK FILTER (blocklist + DNS) ──────────────────────────────────────────────
def is_junk_domain(dom):
    """Return a human-readable reason if this domain can never be a payable
    sponsor, else ''. Cheap string checks only — no network."""
    if not dom or dom.startswith("name:"):
        return ""
    if dom in AFFILIATE_DOMAINS:
        return "affiliate/redirect/shortener"
    if dom in NOISE_DOMAINS:
        return "known non-sponsor (noise list)"
    if JUNK_TLD_RE.search(dom):
        return "gov/edu/reference TLD"
    return ""


_dns_cache = {}


def dns_alive(dom, timeout=4):
    """True if the domain resolves. Dead domain = dead brand = never write it.
    Cached per run so repeat sponsors cost one lookup."""
    if not dom or dom.startswith("name:") or "." not in dom:
        return True   # name-only sponsors have no domain to check
    if dom in _dns_cache:
        return _dns_cache[dom]
    import socket
    old = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout)
    try:
        socket.getaddrinfo(dom, 443)
        alive = True
    except OSError:
        # A bare root domain sometimes only resolves with www.
        try:
            socket.getaddrinfo("www." + dom, 443)
            alive = True
        except OSError:
            alive = False
    finally:
        socket.setdefaulttimeout(old)
    _dns_cache[dom] = alive
    return alive


def junk_reason(dom, check_dns=True):
    """Combined verdict: blocklist reason, 'dead domain (DNS)', or ''."""
    reason = is_junk_domain(dom)
    if reason:
        return reason
    if check_dns and not dns_alive(dom):
        return "dead domain (DNS does not resolve)"
    return ""


# ── REDIRECT RESOLVER (unwrap shorteners to the brand hiding behind them) ──────
# lmg.gg, bit.ly, geni.us etc. are never the brand — but their DESTINATION often
# is a real sponsor. Follow the redirect once and judge the final domain instead.
_redirect_cache = {}


def resolve_redirect(url, timeout=6):
    """Follow a shortener/affiliate link and return the final ROOT DOMAIN
    (e.g. lmg.gg/xyz -> nordvpn.com), or '' if unresolvable. Cached per run."""
    if url in _redirect_cache:
        return _redirect_cache[url]
    final = ""
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
    for method in ("HEAD", "GET"):        # some shorteners refuse HEAD
        try:
            req = urllib.request.Request(url, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                host = urllib.parse.urlparse(r.geturl()).netloc
                final = root_domain(host) if host else ""
            break
        except Exception:
            continue
    _redirect_cache[url] = final
    return final


# ── SKIP LOG ────────────────────────────────────────────────────────────────────
# Every lead the run passes on is logged here and printed ONCE in a final
# SKIP REPORT (deduped — a brand skipped on 5 channels shows one line, ×5).
# Purpose: you eyeball the report every run so no real lead dies silently.
SKIPPED = {}   # (name_lower, reason) -> {name, reason, count, where}


def log_skip(name, reason, where=""):
    """Record a skipped lead. Returns True only on FIRST sighting, so callers
    can print immediately without duplicate spam."""
    key = (str(name).strip().lower(), reason)
    e = SKIPPED.setdefault(key, {"name": str(name).strip(), "reason": reason,
                                 "count": 0, "where": set()})
    e["count"] += 1
    if where:
        e["where"].add(str(where))
    return e["count"] == 1


def print_skip_report():
    """The end-of-run audit: everything skipped, grouped by reason, deduped."""
    if not SKIPPED:
        print("\n── SKIP REPORT ── nothing was skipped this run.")
        return
    print(f"\n── SKIP REPORT ── {len(SKIPPED)} unique skip(s) this run. "
          f"Eyeball this list — rescue anything that's a real lead:")
    by_reason = defaultdict(list)
    for e in SKIPPED.values():
        by_reason[e["reason"]].append(e)
    for reason, entries in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
        print(f"\n  [{reason}] — {len(entries)} brand(s)")
        for e in sorted(entries, key=lambda x: x["name"].lower()):
            times = f"  ×{e['count']}" if e["count"] > 1 else ""
            where = f"  (via: {', '.join(sorted(e['where'])[:3])})" if e["where"] else ""
            print(f"    • {e['name']}{times}{where}")
    print("\n  Rescue a wrongly-skipped brand with: "
          "python3 brand_sourcer.py --add <domain> --creator '<channel>'")
    SKIPPED.clear()

SPONSOR_WORDS = re.compile(
    r"\b(sponsor|sponsored|sponsoring|thanks to|brought to you|use code|promo code|"
    r"discount code|coupon|% ?off|free trial|sign ?up|get started(?: with)?|"
    r"check (?:it |them )?out|try .* (free|today)|affiliate|partner(ed)? with|"
    r"in partnership with|in collaboration with|collab(?:orat(?:ed|ing|ion))? with|"
    r"get the best out of|powered by|our friends at|special thanks|shout ?out to|"
    r"supported by|start your free trial)\b",
    re.I,
)
URL_RE = re.compile(r"https?://([^/\s)\]]+)(/[^\s)\]]*)?", re.I)
CODE_RE = re.compile(r"\b(?:use\s+code|promo\s*code|code)[:\s\"']*([A-Z0-9]{3,15})\b")

# Capture a brand NAMED in sponsor copy even when it has no link, e.g.
# "Sponsored by Surfshark" / "Thanks to Notion". Brand must start capitalised.
NAMED_SPONSOR = re.compile(
    r"\b(?P<trig>[Ss]ponsored by|[Tt]hanks (?:again )?to|[Bb]rought to you by|"
    r"[Ii]n partnership with|[Pp]artnered with)\s+"
    r"(?:our sponsors?[,:]?\s+)?"
    r"(?P<name>[A-Z][A-Za-z0-9.&'\-]*(?:\s+[A-Z0-9][A-Za-z0-9.&'\-]+){0,1})"
)
# First word here is never a brand — stops "thanks to You/everyone/my Patreon".
NAME_STOP = {
    "you", "your", "our", "my", "the", "all", "everyone", "everybody", "this",
    "that", "today", "them", "they", "we", "i", "watching", "god", "mom", "dad",
    "family", "fans", "community", "viewers", "patreon", "patron", "patrons",
    "supporters", "members", "subscribers", "team", "channel", "guys", "friend",
    "friends", "folks", "people", "everyone's", "y'all",
}
# Trailing words to trim off a captured name (CTA verbs / connectors).
TRIM_TAIL = {
    "use", "get", "go", "sign", "visit", "check", "head", "click", "download",
    "try", "for", "at", "and", "the", "to", "by", "is", "are", "now", "here",
    "today", "just", "also", "who", "which", "links", "link", "code", "in",
    "this", "that", "it", "we", "they", "my", "on", "with", "of",
}


# ── YOUTUBE API ───────────────────────────────────────────────────────────────
def yt_get(endpoint, params):
    params["key"] = YOUTUBE_API_KEY
    url = f"https://www.googleapis.com/youtube/v3/{endpoint}?" + urllib.parse.urlencode(params)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
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
            print(f"  Error: {e}")
            return {}
    return {}


def resolve_channel(arg):
    """Return (channel_id, title, uploads_playlist, subs) from a URL/handle/id."""
    arg = arg.strip()
    handle = cid = None
    m = re.search(r"/channel/(UC[\w-]{20,})", arg)
    if m:
        cid = m.group(1)
    elif "/@" in arg or arg.startswith("@"):
        handle = re.sub(r".*/@", "@", arg) if "/@" in arg else arg
        handle = handle.split("/")[0].lstrip("@")
    elif arg.startswith("UC") and len(arg) >= 22:
        cid = arg
    else:
        handle = arg.lstrip("@")

    params = {"part": "snippet,statistics,contentDetails"}
    if cid:
        params["id"] = cid
    else:
        params["forHandle"] = handle
    data = yt_get("channels", params)
    items = data.get("items") or []
    if not items:
        return None
    it = items[0]
    return (
        it["id"],
        it["snippet"]["title"],
        it["contentDetails"]["relatedPlaylists"]["uploads"],
        int(it["statistics"].get("subscriberCount", 0)),
    )


def recent_videos(uploads_playlist, n):
    ids, token = [], None
    while len(ids) < n:
        p = {"part": "contentDetails", "playlistId": uploads_playlist,
             "maxResults": min(50, n - len(ids))}
        if token:
            p["pageToken"] = token
        data = yt_get("playlistItems", p)
        ids += [i["contentDetails"]["videoId"] for i in data.get("items", [])]
        token = data.get("nextPageToken")
        if not token:
            break
    return ids[:n]


def video_descriptions(video_ids):
    out = []
    for i in range(0, len(video_ids), 50):
        chunk = video_ids[i:i + 50]
        data = yt_get("videos", {"part": "snippet", "id": ",".join(chunk)})
        for it in data.get("items", []):
            sn = it["snippet"]
            out.append((sn.get("title", ""), sn.get("description", "")))
    return out


# ── SPONSOR DETECTION ─────────────────────────────────────────────────────────
def root_domain(host):
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def brand_name(domain):
    return domain.split(".")[0].replace("-", " ").title()


def brand_url_from_domain(dom):
    """Clean, crawlable homepage URL from a detected root domain.

    Sponsor links in descriptions are usually campaign/tracking links
    (proton.me/somecreator, go.brand.com/creator, nordvpn.com/ref?a=123).
    For research we only want the ROOT homepage, so we drop the path entirely.
    Name-only sponsors (dict key 'name:...') have no domain -> return "".
    """
    if not dom or dom.startswith("name:") or "." not in dom:
        return ""
    return "https://" + dom


def _norm_name(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def names_match(a, b):
    a, b = _norm_name(a), _norm_name(b)
    return bool(a) and bool(b) and (a in b or b in a)


def clean_named(raw):
    """Tidy a captured sponsor name: keep up to 2 words, drop trailing CTA words."""
    words = raw.split()
    out = []
    for i, w in enumerate(words[:2]):
        if i > 0 and w.lower().strip(".,:;!") in TRIM_TAIL:
            break
        out.append(w)
    name = " ".join(out).strip(" .,:;!&'\"-")
    if not name or name.split()[0].lower() in NAME_STOP:
        return None
    if not re.search(r"[A-Za-z]", name):
        return None
    return name


_unwrap_announced = set()   # print each unwrapped short link once per run

# Tracking/affiliate link shapes — strong sponsor evidence even with no words.
_AFF_QUERY = re.compile(r"[?&](ref|via|aff|affiliate|utm_[a-z]+|coupon|discount|promo|code|a)=", re.I)
_AFF_PATH  = re.compile(r"/(r|ref|aff|affiliate|promo|partner|go)/", re.I)
_PAGE_WORDS = {
    "about", "pricing", "price", "blog", "product", "products", "features", "docs",
    "doc", "download", "downloads", "home", "index", "contact", "support", "help",
    "login", "signup", "sign-up", "register", "app", "apps", "store", "shop",
    "careers", "news", "watch", "video", "videos", "channel", "playlist",
    "privacy", "terms", "faq", "pages", "en", "us",
}


def affiliate_shaped(raw):
    """True if a link looks like a creator tracking/affiliate link — a ref/utm
    param, an /r/ style path, or a single short vanity segment like /ivy. Those
    are sponsor signals even when no sponsor words sit nearby."""
    if "://" not in raw:
        return False
    after = raw.split("://", 1)[1]
    path = "/" + after.split("/", 1)[1] if "/" in after else ""
    if _AFF_QUERY.search(raw) or _AFF_PATH.search(path):
        return True
    segs = [s for s in path.split("?")[0].split("/") if s]
    if len(segs) == 1:
        seg = segs[0].lower()
        if (2 <= len(seg) <= 20 and re.fullmatch(r"[a-z0-9._-]+", seg)
                and seg not in _PAGE_WORDS
                and not seg.endswith((".html", ".htm", ".php"))):
            return True
    return False


def detect_sponsors(videos, own_title):
    """videos: list of (title, description) -> dict domain -> {count,confidence,codes,sample,name}"""
    own_tokens = set(re.findall(r"[a-z0-9]+", own_title.lower()))
    found = defaultdict(lambda: {"count": 0, "high": 0, "codes": set(),
                                 "sample": "", "name": ""})
    for title, desc in videos:
        # All candidate links in this video (skip the creator's own + known noise).
        links = []  # (start, end, dom, raw)
        for m in URL_RE.finditer(desc):
            dom = root_domain(m.group(1))
            if dom in AFFILIATE_DOMAINS:
                # Shortener: the deal (if any) lives at the DESTINATION.
                final = resolve_redirect(m.group(0))
                if final and final not in AFFILIATE_DOMAINS and final not in NOISE_DOMAINS:
                    if m.group(0) not in _unwrap_announced:
                        _unwrap_announced.add(m.group(0))
                        print(f"    ↪ unwrapped {dom} -> {final}")
                    dom = final               # judge the real brand from here on
                elif final:
                    log_skip(f"{dom} -> {final}",
                             "shortener resolves to a noise/blocked destination",
                             title or own_title)
                    continue
                else:
                    log_skip(dom, "shortener/affiliate link (destination unresolvable)",
                             title or own_title)
                    continue
            if dom in NOISE_DOMAINS:
                # Noise domains skip silently EXCEPT when sponsor language sits
                # right next to the link — that could be a real lead a blocklist
                # entry is eating (apple.com is on the list; Apple runs sponsorships).
                ctx = desc[max(0, m.start() - 150):m.end() + 150]
                if SPONSOR_WORDS.search(ctx):
                    log_skip(dom, "on noise blocklist BUT sponsor language nearby — verify",
                             title or own_title)
                continue
            if brand_name(dom).lower() in own_tokens:
                continue
            links.append((m.start(), m.end(), dom, m.group(0)[:90]))

        seen_here = set()
        for s, e, dom, raw in links:
            if dom not in seen_here:
                found[dom]["count"] += 1
                seen_here.add(dom)
            if not found[dom]["sample"]:
                found[dom]["sample"] = raw
                found[dom]["name"] = brand_name(dom)
            if affiliate_shaped(raw):
                found[dom]["high"] += 1        # tracking/affiliate link = sponsor signal

        # Attribution: a sponsor phrase belongs to the link that FOLLOWS it
        # ("Thanks to X ... <link>"). If none follows within range, fall back to a
        # link just BEFORE it (handles a trailing "<link> use code XYZ").
        def owner_link(pos):
            after = [l for l in links if l[0] >= pos and l[0] - pos <= 200]
            if after:
                return min(after, key=lambda l: l[0] - pos)
            before = [l for l in links if l[1] <= pos and pos - l[1] <= 120]
            if before:
                return max(before, key=lambda l: l[1])
            return None

        # 1) NAMED sponsors ("Sponsored by Brand"): credit the matching link if one
        #    is nearby, otherwise log the brand by NAME even with no link.
        named_spans = []
        for nm in NAMED_SPONSOR.finditer(desc):
            name = clean_named(nm.group("name"))
            if not name:
                continue
            owner = owner_link(nm.start())
            # Brand also has a link right here -> just credit the link (works for any
            # trigger, e.g. "thanks to Incogni https://incogni.com").
            if owner and names_match(name, brand_name(owner[2])):
                named_spans.append(nm.span())
                found[owner[2]]["high"] += 1
                continue
            # No matching link. "thanks to ___" is the noisy trigger (it also credits
            # collaborators/people), so only trust it as a brand when sponsorship is
            # stated right after ("thanks to Notion for sponsoring").
            weak = nm.group("trig").lower().startswith("thanks")
            if weak and "sponsor" not in desc[nm.end():nm.end() + 45].lower():
                named_spans.append(nm.span())   # suppress: don't let it mis-credit a link
                continue
            named_spans.append(nm.span())
            key = "name:" + name.lower()                # name-only sponsor
            if key not in seen_here:
                found[key]["count"] += 1
                seen_here.add(key)
            found[key]["high"] += 1
            if not found[key]["name"]:
                found[key]["name"] = name
                found[key]["sample"] = "(named as sponsor in description; no link)"

        def in_named(pos):
            return any(a <= pos < b for a, b in named_spans)

        # 2) Generic sponsor language -> credit the owning link (skip phrases already
        #    consumed by a named-sponsor capture so we don't double-handle them).
        for sm in SPONSOR_WORDS.finditer(desc):
            if in_named(sm.start()):
                continue
            owner = owner_link(sm.start())
            if owner:
                found[owner[2]]["high"] += 1
        for cm in CODE_RE.finditer(desc):
            owner = owner_link(cm.start())
            if owner:
                found[owner[2]]["codes"].add(cm.group(1))
                found[owner[2]]["high"] += 1   # a promo code by a link = sponsor
    results, dropped = {}, []
    for dom, d in found.items():
        # Keep only domains with real sponsor language nearby. Bare mentions and
        # source/citation links are dropped.
        if d["high"]:
            d["confidence"] = "High" if (d["count"] >= 2 or d["codes"]) else "Medium"
            results[dom] = d
        else:
            dropped.append(d["name"] or dom)
    return results, dropped


def build_brand_row(name, niche, note, brand_url="", status="Sourced"):
    """12 base cols for the Brands master (matches BRAND_HEADERS up to Brand URL:
    Brand, Niche, Objective, Decision-Maker, Title, Contact Email, Alt Contact 1,
    Alt Contact 2, Status, Last Contact, Notes, Brand URL).

    Brand Objective + contact columns are left blank for the enricher/outreach
    pipeline to fill. The two trailing formula columns (Times Spotted /
    # Influencers) are added at append time, once the target row is known.
    """
    return [name, niche, "", "", "", "", "", "", status, "", note, brand_url]


def build_sight_row(brand, influencer, subs, link, confidence, recurring, note, date=None):
    """8 cols for the Sightings log (matches SIGHT_HEADERS)."""
    d = date or f"{datetime.now():%Y-%m-%d}"
    return [brand, influencer, subs, d, link, confidence,
            "Yes" if recurring else "", note]


# ── GOOGLE SHEETS ──────────────────────────────────────────────────────────────
def _col_letter(n):
    """1 -> A, 19 -> S, 27 -> AA."""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def find_header_row(grid, max_scan=10):
    """Return the 1-based row that holds the column headers.

    The Brands tab has a title band + subtitle above the real headers (headers
    live on row 3, data from row 4). We locate the header row by content instead
    of assuming row 1, so decoration rows above it are never clobbered.
    Falls back to row 1 if nothing matches (fresh/plain sheet).
    """
    for i, row in enumerate(grid[:max_scan]):
        cells = [c.strip().lower() for c in row]
        if "brand" in cells and ("status" in cells or "notes" in cells
                                 or "tie-in influencer" in cells):
            return i + 1
    return 1


def _open_sheet():
    """Authorize once and return the open spreadsheet."""
    if gspread is None:
        sys.exit("gspread not installed. Run: pip3 install gspread google-auth "
                 "--break-system-packages --user")
    if not CREDS_FILE or not SHEET_NAME:
        sys.exit("Set GOOGLE_CREDS_FILE and SHEET_NAME (see .env.example).")
    scopes = ["https://www.googleapis.com/auth/spreadsheets",
              "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_file(CREDS_FILE, scopes=scopes)
    gc = gspread.authorize(creds)
    return gc.open(SHEET_NAME)


def connect_brands_ws(sh=None):
    """Return (worksheet, header_row) for the Brands master. Data starts below it."""
    sh = sh or _open_sheet()
    try:
        ws = sh.worksheet(BRANDS_WS)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=BRANDS_WS, rows=300, cols=len(BRAND_HEADERS))
        ws.update(values=[BRAND_HEADERS], range_name="A1")
        print(f"  Created new '{BRANDS_WS}' tab in '{SHEET_NAME}'.")
        return ws, 1

    grid = ws.get_all_values()
    hrow = find_header_row(grid)
    header = grid[hrow - 1] if hrow - 1 < len(grid) else []
    header_nonempty = [c for c in header if c.strip()]
    # Add any new trailing headers to the END of the real header row only.
    if len(header_nonempty) < len(BRAND_HEADERS):
        missing = BRAND_HEADERS[len(header_nonempty):]
        start_col = _col_letter(len(header_nonempty) + 1)
        ws.update(values=[missing], range_name=f"{start_col}{hrow}")
        print(f"  Added new column header(s) to '{BRANDS_WS}' row {hrow}: {', '.join(missing)}")
    return ws, hrow


def connect_sightings_ws(sh=None):
    """Return (worksheet, header_row) for the Sightings log. Creates the tab if missing."""
    sh = sh or _open_sheet()
    try:
        ws = sh.worksheet(SIGHT_WS)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=SIGHT_WS, rows=1000, cols=len(SIGHT_HEADERS))
        ws.update(values=[SIGHT_HEADERS], range_name="A1")
        print(f"  Created new '{SIGHT_WS}' tab in '{SHEET_NAME}'.")
        return ws, 1

    grid = ws.get_all_values()
    hrow = 1
    for i, row in enumerate(grid[:5]):
        cells = [c.strip().lower() for c in row]
        if "brand" in cells and "influencer" in cells:
            hrow = i + 1
            break
    return ws, hrow


def sheet_existing_brands(ws, header_row):
    """Set of lowercased brand names already in the Brands master (dedupe by name)."""
    names = set()
    for r in ws.get_all_values()[header_row:]:   # rows BELOW the header row
        if r and r[0].strip():
            names.add(r[0].strip().lower())
    return names


def sheet_existing_sightings(sws, header_row):
    """Set of (brand, influencer, link) keys already logged, to skip exact repeats."""
    keys = set()
    for r in sws.get_all_values()[header_row:]:
        if r and r[0].strip():
            inf = r[1].strip().lower() if len(r) > 1 else ""
            link = r[4].strip().lower() if len(r) > 4 else ""
            keys.add((r[0].strip().lower(), inf, link))
    return keys


def append_brand_rows(ws, base_rows, header_row):
    """Append deduped brand rows, adding the two live count formulas per row."""
    if not base_rows:
        return 0
    col_a = ws.col_values(1)
    start = max(len(col_a), header_row) + 1
    block = []
    for i, r in enumerate(base_rows):
        n = start + i
        s = FORMULA_SEP
        block.append(r + [f"=COUNTIF({SIGHT_WS}!$A:$A{s}$A{n})",
                          f"=COUNTUNIQUEIFS({SIGHT_WS}!$B:$B{s}{SIGHT_WS}!$A:$A{s}$A{n})"])
    ws.update(values=block, range_name=f"A{start}", value_input_option="USER_ENTERED")
    return len(block)


def append_sight_rows(sws, rows, header_row):
    """Append sighting rows to the log."""
    if not rows:
        return 0
    col_a = sws.col_values(1)
    start = max(len(col_a), header_row) + 1
    sws.update(values=rows, range_name=f"A{start}", value_input_option="USER_ENTERED")
    return len(rows)


# ── LOCAL XLSX (fallback + migration source) ───────────────────────────────────
def read_local_brands(path):
    """Read existing brand rows (A:R, row 5+) from the old local tracking xlsx."""
    if load_workbook is None:
        sys.exit("openpyxl not installed. Run: pip3 install openpyxl --break-system-packages --user")
    wb = load_workbook(path)
    ws = wb["Pipeline"]
    rows = []
    for r in range(5, ws.max_row + 1):  # row 4 is the example; start at 5
        brand = ws.cell(row=r, column=1).value
        if not brand or "EXAMPLE ROW" in str(ws.cell(row=r, column=18).value or ""):
            continue
        row = [ws.cell(row=r, column=c).value if ws.cell(row=r, column=c).value is not None
               else "" for c in range(1, 19)]
        row.append("")   # pad Brand URL (col S) for migrated legacy rows
        rows.append(row)
    return rows


def write_local(path, rows):
    if load_workbook is None:
        sys.exit("openpyxl not installed. Run: pip3 install openpyxl --break-system-packages --user")
    if os.path.exists(path):
        wb = load_workbook(path)
        ws = wb.active
    else:
        wb = Workbook()
        ws = wb.active
        ws.title = "Brands"
        ws.append(BRAND_HEADERS)
    for row in rows:
        ws.append(row)
    wb.save(path)


# ── CLEAN (sweep junk already sitting in the Brands tab) ───────────────────────
def clean_brands(dry_run=False, check_dns=True):
    """One pass over the existing Brands master: any row whose URL is on the
    blocklist or whose domain is dead gets Status -> 'Skip' with the reason
    appended to Notes. Never deletes rows, so nothing is lost.

    Run it any time with:  python3 brand_sourcer.py --clean
    """
    STATUS_COL, NOTES_COL, URL_COL = 8, 10, 11
    ws, hrow = connect_brands_ws()
    values = ws.get_all_values()
    data = values[hrow:]
    if not data:
        print("Brands tab has no data rows.")
        return 0

    def cell(row, idx):
        return row[idx - 1].strip() if len(row) >= idx else ""

    flagged, updates = [], []
    for i, row in enumerate(data):
        sheet_row = hrow + 1 + i
        name = cell(row, 1)
        if not name:
            continue
        if cell(row, STATUS_COL).lower() in ("skip", "review"):
            continue                        # already flagged
        url = cell(row, URL_COL)
        dom = root_domain(re.sub(r"^https?://", "", url).split("/")[0]) if url else ""
        if not dom:
            continue
        # Certain junk (blocklist) -> Skip. Dead DNS -> Review, because sponsor
        # links die while brands live on (Pikzels/ClearSERP lesson).
        blocked = is_junk_domain(dom)
        if blocked:
            new_status, reason = "Skip", blocked
        elif check_dns and not dns_alive(dom):
            new_status = "Review"
            reason = f"dead domain: {dom} does not resolve — verify the brand, then --fix-url"
        else:
            continue
        flagged.append((name, dom, new_status, reason))
        print(f"  {'✂' if new_status == 'Skip' else '⚠'} {name:<20} ({dom})  -> {new_status}  ({reason})")
        note = cell(row, NOTES_COL)
        stamp = f" | AUTO-CLEAN {datetime.now():%Y-%m-%d}: {reason}."
        updates.append({"range": f"{_col_letter(STATUS_COL)}{sheet_row}",
                        "values": [[new_status]]})
        updates.append({"range": f"{_col_letter(NOTES_COL)}{sheet_row}",
                        "values": [[note + stamp]]})

    if not flagged:
        print("Clean sweep done — no junk found. The tab is pitch-ready.")
        return 0
    n_skip = sum(1 for f in flagged if f[2] == "Skip")
    n_rev = len(flagged) - n_skip
    if dry_run:
        print(f"\n[dry-run] Would mark {n_skip} as Skip and {n_rev} as Review. Nothing saved.")
        return 0
    ws.batch_update(updates, value_input_option="USER_ENTERED")
    print(f"\nMarked {n_skip} brand(s) Skip and {n_rev} Review in '{BRANDS_WS}'. "
          f"Rows kept (audit trail in Notes). Verify the Review rows, then fix with:\n"
          f"  python3 brand_sourcer.py --fix-url '<Brand>' <real-homepage-url>")
    return len(flagged)


def fix_url(brand, url, dry_run=False):
    """Point a brand at its REAL homepage: update Brand URL, reset Status to
    Sourced, clear the objective so the enricher redoes it, and stamp Notes.

    python3 brand_sourcer.py --fix-url 'Pikzels' https://pikzels.com
    """
    STATUS_COL, NOTES_COL, URL_COL, OBJ_COL = 8, 10, 11, 3
    if not url.startswith("http"):
        url = "https://" + url
    ws, hrow = connect_brands_ws()
    data = ws.get_all_values()[hrow:]
    target = brand.strip().lower()
    for i, row in enumerate(data):
        if row and row[0].strip().lower() == target:
            sheet_row = hrow + 1 + i
            note = (row[NOTES_COL - 1] if len(row) >= NOTES_COL else "")
            stamp = f" | URL corrected to {url} on {datetime.now():%Y-%m-%d}."
            if dry_run:
                print(f"[dry-run] Would set {row[0].strip()}: URL={url}, Status=Sourced, "
                      f"Objective cleared for re-enrichment.")
                return True
            ws.batch_update([
                {"range": f"{_col_letter(URL_COL)}{sheet_row}", "values": [[url]]},
                {"range": f"{_col_letter(STATUS_COL)}{sheet_row}", "values": [["Sourced"]]},
                {"range": f"{_col_letter(OBJ_COL)}{sheet_row}", "values": [[""]]},
                {"range": f"{_col_letter(NOTES_COL)}{sheet_row}", "values": [[note + stamp]]},
            ], value_input_option="USER_ENTERED")
            print(f"Fixed {row[0].strip()}: URL -> {url}, Status -> Sourced. "
                  f"Run 'python3 brand_enricher.py' to re-enrich it.")
            return True
    print(f"[!] Brand '{brand}' not found in the '{BRANDS_WS}' tab (exact name match, "
          f"case-insensitive).")
    return False


# ── MAIN ───────────────────────────────────────────────────────────────────────
def source_channel(ch, n_videos, niche, min_conf, check_dns=True):
    """Return (printed_ok, list_of_rows) for one channel."""
    info = resolve_channel(ch)
    if not info:
        print(f"\n[!] Could not resolve: {ch}")
        return []
    cid, title, uploads, subs = info
    print(f"\n=== {title}  ({subs:,} subs) ===")
    descs = video_descriptions(recent_videos(uploads, n_videos))
    return rows_from_descs(descs, title, subs, niche, min_conf, check_dns=check_dns)


def rows_from_descs(descs, title, subs, niche=NICHE, min_conf="Medium", check_dns=True):
    """Build brand rows from PRE-FETCHED (title, description) pairs — zero API calls.

    This is the sourcing core with the YouTube fetch stripped out, so the finder
    script can feed it video data it already paid for (its stats call) and get
    brand rows for free. Returns the same (row, dedupe_key) tuples as
    source_channel().
    """
    sponsors, dropped = detect_sponsors(descs, title)
    if dropped:
        uniq = sorted(set(dropped))
        shown = ", ".join(uniq[:10]) + ("..." if len(uniq) > 10 else "")
        print(f"  (filtered {len(uniq)} non-sponsor link(s): {shown})")
        for d in uniq:   # full deduped list lands in the end-of-run SKIP REPORT
            log_skip(d, "no sponsor language near link", title)
    ranked = sorted(sponsors.items(),
                    key=lambda kv: (kv[1]["count"] >= 2, kv[1]["confidence"] == "High",
                                    kv[1]["count"]), reverse=True)
    keep = [(d, x) for d, x in ranked
            if not (min_conf == "High" and x["confidence"] != "High")]
    if not keep:
        print("  No sponsor brands detected in recent descriptions.")
        return []
    sub_label = f"{round(subs/1000)}K" if subs else "?"
    records = []
    for dom, x in keep:
        # Certain junk (blocklist) -> skip. Dead DNS -> KEEP but flag Review:
        # sponsor links are campaign/tracking domains that often die while the
        # brand lives on (Pikzels, ClearSERP). A human or Claude verifies.
        blocked = is_junk_domain(dom)
        if blocked:
            label = f"{x['name'] or dom} ({dom})"
            if log_skip(label, blocked, title):   # print once per run
                print(f"  ✂ {label:<28} skipped: {blocked}")
            continue
        dead = check_dns and not dns_alive(dom)
        if dead:
            print(f"  ⚠ {x['name'] or dom:<16} ({dom}) — sponsor-link domain is DEAD; "
                  f"keeping with Status=Review (find the real homepage)")
        name = canon_name(x["name"])
        recur = x["count"] >= 2
        tag = "  <-- GOLDEN NUGGET (recurring)" if recur else ""
        codes = (" codes:" + ",".join(sorted(x["codes"]))) if x["codes"] else ""
        print(f"  • {name:<16} {x['confidence']:<6} seen in {x['count']} vid(s)"
              f"  {x['sample']}{codes}{tag}")
        sight_note = (f"{'RECURRING - GOLDEN NUGGET. ' if recur else ''}"
                      f"Seen in {x['count']} of {len(descs)} recent videos. "
                      f"Confidence {x['confidence']}."
                      f"{' Codes: ' + ','.join(sorted(x['codes'])) if x['codes'] else ''} "
                      f"Auto-sourced {datetime.now():%Y-%m-%d}.")
        note_brand = "Recurring sponsor (proven budget)." if recur else ""
        status = "Sourced"
        if dead:
            status = "Review"
            note_brand = (note_brand + f" DEAD LINK: {dom} does not resolve — the brand "
                          f"may still be live under another domain. Verify before pitching "
                          f"(fix with: --fix-url '{name}' <real-homepage>).").strip()
        rec = {"brand": name, "niche": niche,
               "note_brand": note_brand,
               "brand_url": brand_url_from_domain(dom), "status": status,
               "influencer": title, "subs": sub_label, "link": x["sample"],
               "confidence": x["confidence"], "recurring": recur, "note_sight": sight_note}
        key = (name.strip().lower(), title.strip().lower(), str(x["sample"]).strip().lower())
        records.append((rec, key))
    return records


def manual_brands(items, creator, subs, niche):
    """Build rows from a hand-supplied brand list (e.g. a creator's media kit)."""
    sub_label = f"{round(subs/1000)}K" if subs else "?"
    creator_label = creator or "Manual entry"
    print(f"\n=== Manual add: {len(items)} brand(s) -> {creator_label} ({sub_label}) ===")
    records = []
    for raw in items:
        raw = raw.strip().strip(",")
        if not raw:
            continue
        # Accept either a domain (nordvpn.com) or a plain name (NordVPN).
        # Manual adds are TRUSTED (this is also the rescue path for wrongly
        # auto-skipped brands) — the junk filter only warns here, never blocks.
        dom = root_domain(raw) if "." in raw else ""
        reason = junk_reason(dom, check_dns=False)
        if reason:
            print(f"  ⚠ {raw:<18} heads-up: normally skipped ({reason}) — "
                  f"adding anyway because you asked for it")
        name = canon_name(brand_name(dom) if dom else raw)
        url = brand_url_from_domain(dom)
        link = raw if "." in raw else url
        note = (f"From {creator_label}'s media kit / partnered-with list. "
                f"{'Link: ' + link + '. ' if link else ''}Added {datetime.now():%Y-%m-%d}.")
        print(f"  • {name:<18} {url or '(no url - name only)'}")
        rec = {"brand": name, "niche": niche, "note_brand": "",
               "brand_url": url, "status": "Sourced",
               "influencer": creator_label, "subs": sub_label, "link": link,
               "confidence": "Manual", "recurring": False, "note_sight": note}
        key = (name.strip().lower(), creator_label.strip().lower(), str(link).strip().lower())
        records.append((rec, key))
    return records


def main():
    ap = argparse.ArgumentParser(description="Source sponsor brands from YouTube creators into Google Sheets.")
    ap.add_argument("channels", nargs="*", help="Channel URL(s), @handle(s), or UC id(s)")
    ap.add_argument("--videos", type=int, default=DEFAULT_VIDEOS, help="recent videos to scan")
    ap.add_argument("--niche", default=NICHE, help="niche label to write")
    ap.add_argument("--min-confidence", choices=["High", "Medium"], default="Medium")
    ap.add_argument("--import-xlsx", metavar="PATH",
                    help="one-time: copy existing brands from the old local xlsx into the Brands tab")
    ap.add_argument("--local", metavar="PATH",
                    help="write to a local xlsx instead of Google Sheets")
    ap.add_argument("--add", nargs="+", metavar="BRAND",
                    help="manually add brands (domains or names) instead of scanning, "
                         "e.g. --add nordvpn.com brilliant.org --creator 'Some Creator' --subs 490000")
    ap.add_argument("--creator", default="", help="creator label for --add (the tie-in)")
    ap.add_argument("--subs", type=int, default=0, help="creator sub count for --add")
    ap.add_argument("--dry-run", action="store_true", help="print only, don't write")
    ap.add_argument("--no-enrich", action="store_true",
                    help="skip the auto-enrichment pass (by default, sourcing also "
                         "researches each new brand's objective + what they sell)")
    ap.add_argument("--clean", action="store_true",
                    help="sweep the EXISTING Brands tab: blocklisted junk -> Skip, "
                         "dead domains -> Review (reasons stamped in Notes)")
    ap.add_argument("--no-dns", action="store_true",
                    help="skip the DNS liveness check (faster, but dead domains get through)")
    ap.add_argument("--fix-url", nargs=2, metavar=("BRAND", "URL"),
                    help="point a brand at its real homepage and reset it for "
                         "re-enrichment, e.g. --fix-url 'Pikzels' https://pikzels.com")
    args = ap.parse_args()

    if args.fix_url:
        fix_url(args.fix_url[0], args.fix_url[1], dry_run=args.dry_run)
        return

    if args.clean:
        clean_brands(dry_run=args.dry_run, check_dns=not args.no_dns)
        return

    # Collect (record, dedupe_key) tuples from all sources
    pending = []

    if args.import_xlsx:
        print("--import-xlsx is retired: the one-time migration to the two-tab "
              "(Brands + Sightings) model is already done. Ignoring.")

    if args.add:
        pending += manual_brands(args.add, args.creator, args.subs, args.niche)

    for ch in args.channels:
        pending += source_channel(ch, args.videos, args.niche, args.min_confidence,
                                  check_dns=not args.no_dns)

    if not pending:
        print("\nNothing to do. Pass a channel, --add brands, or --import-xlsx to migrate.")
        print_skip_report()
        return

    if args.dry_run:
        print(f"\n[dry-run] Would write {len(pending)} row(s). Nothing saved.")
        print_skip_report()
        return

    # ── WRITE ──
    if args.local:
        # Local dump = Brands master only, deduped by brand name within this batch.
        seen, rows = set(), []
        for rec, _ in pending:
            b = rec["brand"].strip().lower()
            if b not in seen:
                rows.append(build_brand_row(rec["brand"], rec["niche"],
                                            rec["note_brand"], rec["brand_url"], rec["status"]))
                seen.add(b)
        write_local(args.local, rows)
        print(f"\nSaved {len(rows)} brand(s) to {args.local}")
        print_skip_report()
        return

    # Manual --add is a trusted, deliberate action → bypass the tech-only filter
    # so you can add non-tech real sponsors on purpose. Channel scans stay tech-only.
    save_rows(pending, enrich=not args.no_enrich, tech_filter=not bool(args.add))


# ── GROQ TECH-BRAND FILTER — keep only tech brands, drop clothing/food/etc. ────
GROQ_API_KEY = (os.environ.get("GROQ_API_KEY") or os.environ.get("GROQ_KEY")
                or os.environ.get("GROQ_API_TOKEN") or os.environ.get("GROQ_TOKEN") or "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
_HAS_GROQ = bool(GROQ_API_KEY)
_groq_brand_cache = {}


def groq_is_tech_brand(brand):
    """True = tech brand (keep), False = non-tech (drop), None = error/no-key.
    Sends a browser User-Agent (Cloudflare 403s bare urllib) and backs off on
    429, per the Groq gotchas."""
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
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}",
               "Content-Type": "application/json",
               "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
    for attempt in range(3):
        try:
            req = urllib.request.Request(GROQ_URL, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read())
            v = data["choices"][0]["message"]["content"].strip().lower().startswith("y")
            _groq_brand_cache[b] = v
            return v
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(3 * (attempt + 1))
                continue
            print(f"  ⚠ Groq brand check failed for {brand!r}: HTTP {e.code}")
            return None
        except Exception as e:
            print(f"  ⚠ Groq brand check failed for {brand!r}: {e}")
            return None
    return None


def filter_tech_brands(pending):
    """Drop confirmed non-tech brands from a pending list (Groq NO). Keeps
    tech + anything Groq can't reach. No-op when Groq isn't configured."""
    if not _HAS_GROQ or not pending:
        return pending
    kept = []
    for rec, skey in pending:
        if groq_is_tech_brand(rec["brand"]) is False:
            log_skip(rec["brand"], "non-tech brand (Groq filter)",
                     rec.get("influencer", ""))
            continue
        kept.append((rec, skey))
    dropped = len(pending) - len(kept)
    if dropped:
        print(f"  [tech-filter] dropped {dropped} non-tech brand(s).")
    return kept


def save_rows(pending, enrich=True, tech_filter=True):
    """Write records to the two-tab model and optionally enrich.

    Each pending item is (record, sighting_key). Every sighting is logged (unless
    the exact brand+influencer+link already exists); each brand lands once in the
    Brands master (deduped by name). Shared by main() and the finder's brand pass.
    Returns the number of NEW brands added.
    """
    if tech_filter:
        pending = filter_tech_brands(pending)  # keep only tech brands (auto-sourced runs)
    sh = _open_sheet()
    bws, bhrow = connect_brands_ws(sh)
    sws, shrow = connect_sightings_ws(sh)
    existing_brands = sheet_existing_brands(bws, bhrow)
    existing_sights = sheet_existing_sightings(sws, shrow)

    new_brand_rows, new_sight_rows = [], []
    seen_brands, seen_sights = set(), set()
    for rec, skey in pending:
        b = rec["brand"].strip().lower()
        if skey not in existing_sights and skey not in seen_sights:
            new_sight_rows.append(build_sight_row(
                rec["brand"], rec["influencer"], rec["subs"], rec["link"],
                rec["confidence"], rec["recurring"], rec["note_sight"]))
            seen_sights.add(skey)
        if b not in existing_brands and b not in seen_brands:
            new_brand_rows.append(build_brand_row(
                rec["brand"], rec["niche"], rec["note_brand"],
                rec["brand_url"], rec["status"]))
            seen_brands.add(b)
        elif b in existing_brands:
            # Not lost — the brand already has a row; the new sighting still logs.
            log_skip(rec["brand"], "already in Brands tab (dedupe — sighting still logged)",
                     rec["influencer"])

    n_s = append_sight_rows(sws, new_sight_rows, shrow)   # log first so counts have data
    n_b = append_brand_rows(bws, new_brand_rows, bhrow)
    print(f"\nSaved {n_b} new brand(s) to '{BRANDS_WS}' and {n_s} sighting(s) to "
          f"'{SIGHT_WS}' in '{SHEET_NAME}'." if (n_b or n_s) else
          "\nNothing new to add (all brands + sightings already logged).")

    if enrich:
        print("\n── Enrichment pass ──")
        try:
            import brand_enricher   # lazy import avoids a circular import at load time
            brand_enricher.enrich_sheet(bws, bhrow)
        except Exception as e:
            print(f"  Enrichment skipped ({e}). Run 'python3 brand_enricher.py' separately.")

    print_skip_report()   # the audit: every skipped lead, once, with the reason
    return n_b


if __name__ == "__main__":
    main()
