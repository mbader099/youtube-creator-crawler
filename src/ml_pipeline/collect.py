"""
Adapter: crawler outputs → raw.jsonl.

The crawler persists *pointers* to content, not the content itself:
  - Brands CSV (brand_sourcer export)  → brand homepage URLs
  - channel_cache.json (finder)         → channel IDs + pass/fail verdicts
  - seed_queue.json (rabbit-hole)       → seed channel links

This module follows those pointers and stores what they point at, one raw record
per line, without cleaning anything (that is clean.py's job):

    {"id", "source": "brand_site", "kind": "html", "url", "raw": "<html…>", ...}
    {"id", "source": "youtube_description", "kind": "text", "url", "raw": "title\\n\\ndescription", ...}

Politeness: real browser User-Agent, robots.txt respected for websites, per-site
sequential requests with a delay, explicit timeouts, 429 backoff honouring
retry-after. Every failure is counted and printed — nothing fails silently.
"""

import csv
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html.parser import HTMLParser

try:
    import envfile  # noqa: F401  — loads .env (YOUTUBE_API_KEY) when run from src/
except ImportError:
    pass

USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36")
TIMEOUT = 15
MAX_BYTES = 2_000_000
SITE_DELAY = 1.0                 # seconds between requests to the same site
SKIP_EXT = re.compile(r"\.(pdf|jpe?g|png|gif|svg|webp|mp4|zip|dmg|exe|css|js|xml|ico)(\?|$)", re.I)
# Internal links worth more for a text corpus than e.g. /login or /cart.
PREFERRED_PATH = re.compile(r"about|blog|product|feature|pricing|faq|doc|guide|"
                            r"security|privacy-?guide|how|learn|story|mission", re.I)
AVOID_PATH = re.compile(r"login|signin|sign-in|signup|register|cart|checkout|"
                        r"account|careers/|jobs/|/cdn-cgi/|mailto:|tel:|javascript:", re.I)

ERRORS = Counter()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _warn(msg):
    print(f"  ⚠ {msg}", file=sys.stderr)


# ── HTTP ─────────────────────────────────────────────────────────────────────
def http_get(url, accept_html=True, retries=3):
    """GET with UA + timeout + 429 backoff. Falls back to curl_cffi (Chrome TLS
    fingerprint) on 403/503 if it is installed. Returns (text, final_url) or (None, None)."""
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                   "Accept-Language": "en-US,en;q=0.8"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                ctype = r.headers.get("Content-Type", "")
                if accept_html and "html" not in ctype:
                    ERRORS["not_html"] += 1
                    return None, None
                body = r.read(MAX_BYTES)
                charset = r.headers.get_content_charset() or "utf-8"
                return body.decode(charset, errors="replace"), r.geturl()
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries:
                ra = e.headers.get("retry-after") if e.headers else None
                wait = float(ra) if ra and ra.replace(".", "", 1).isdigit() else 5 * (attempt + 1)
                time.sleep(min(wait, 60))
                continue
            if e.code in (403, 503):
                return _get_impersonate(url, accept_html)
            ERRORS[f"http_{e.code}"] += 1
            return None, None
        except Exception as e:
            ERRORS[type(e).__name__] += 1
            return None, None
    ERRORS["http_429_exhausted"] += 1
    return None, None


def _get_impersonate(url, accept_html):
    try:
        from curl_cffi import requests as creq
    except ImportError:
        ERRORS["blocked_no_curl_cffi"] += 1
        return None, None
    try:
        r = creq.get(url, impersonate="chrome", timeout=TIMEOUT)
        if r.status_code >= 400 or (accept_html and "html" not in r.headers.get("content-type", "")):
            ERRORS[f"blocked_{r.status_code}"] += 1
            return None, None
        return r.text[:MAX_BYTES], str(r.url)
    except Exception as e:
        ERRORS[f"curl_cffi_{type(e).__name__}"] += 1
        return None, None


# ── BRAND WEBSITES ───────────────────────────────────────────────────────────
def read_brand_urls(csv_path):
    """Brand homepage URLs from a brand_sourcer CSV export. The export may have
    title rows above the header, so we locate the row containing 'Brand URL'."""
    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    hdr_i = next((i for i, r in enumerate(rows) if "Brand URL" in r), None)
    if hdr_i is None:
        raise ValueError(f"{csv_path}: no 'Brand URL' column found")
    hdr = rows[hdr_i]
    ui, bi = hdr.index("Brand URL"), (hdr.index("Brand") if "Brand" in hdr else None)
    out, seen = [], set()
    for r in rows[hdr_i + 1:]:
        if len(r) > ui and r[ui].strip().startswith("http"):
            u = r[ui].strip()
            host = urllib.parse.urlparse(u).netloc.lower()
            if host and host not in seen:
                seen.add(host)
                out.append((r[bi].strip() if bi is not None else host, u))
    return out


class _LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)


def internal_links(html, base_url):
    """Same-site links, preferred paths first, assets/auth pages dropped."""
    p = _LinkParser()
    try:
        p.feed(html)
    except Exception:
        return []
    host = urllib.parse.urlparse(base_url).netloc.lower().removeprefix("www.")
    seen, good, other = set(), [], []
    for href in p.links:
        u = urllib.parse.urljoin(base_url, href).split("#")[0]
        pu = urllib.parse.urlparse(u)
        if pu.scheme not in ("http", "https"):
            continue
        if pu.netloc.lower().removeprefix("www.") != host:
            continue
        if SKIP_EXT.search(pu.path) or AVOID_PATH.search(u) or u in seen:
            continue
        if pu.path in ("", "/"):
            continue
        seen.add(u)
        (good if PREFERRED_PATH.search(pu.path) else other).append(u)
    return good + other


def _robots(base_url):
    rp = urllib.robotparser.RobotFileParser()
    robots_url = urllib.parse.urljoin(base_url, "/robots.txt")
    txt, _ = http_get(robots_url, accept_html=False)
    if txt is None:
        return None                       # unreachable robots.txt → allowed (RFC 9309)
    rp.parse(txt.splitlines())
    return rp


def crawl_site(brand, url, pages_per_site):
    """Homepage + up to pages_per_site-1 internal pages, sequential with a delay."""
    rp = _robots(url)
    allowed = (lambda u: rp.can_fetch(USER_AGENT, u)) if rp else (lambda u: True)
    records, queue, done = [], [url], set()
    while queue and len(records) < pages_per_site:
        u = queue.pop(0)
        if u in done:
            continue
        done.add(u)
        if not allowed(u):
            ERRORS["robots_disallowed"] += 1
            continue
        html, final = http_get(u)
        time.sleep(SITE_DELAY)
        if not html:
            continue
        records.append({
            "id": hashlib.sha1((final or u).encode()).hexdigest()[:16],
            "source": "brand_site", "kind": "html", "brand": brand,
            "url": final or u, "raw": html, "fetched_at": _now(),
        })
        if len(records) == 1:                      # expand links from the homepage only
            queue.extend(internal_links(html, final or u))
    return records


def collect_brand_sites(csv_path, pages_per_site=6, max_sites=None, workers=8):
    sites = read_brand_urls(csv_path)[:max_sites]
    print(f"  Brand sites: {len(sites)} from {csv_path} (≤{pages_per_site} pages each)")
    out = []
    with ThreadPoolExecutor(max_workers=workers) as ex:    # parallel across sites,
        for i, recs in enumerate(ex.map(lambda s: crawl_site(s[0], s[1], pages_per_site), sites), 1):
            out.extend(recs)                                # sequential within a site
            if i % 10 == 0 or i == len(sites):
                print(f"    {i}/{len(sites)} sites · {len(out)} pages")
    return out


# ── YOUTUBE DESCRIPTIONS ─────────────────────────────────────────────────────
def read_channel_ids(channel_cache=None, seed_queue=None, verdicts=("pass",)):
    """Channel IDs the crawler judged worth keeping (finder cache) + rabbit-hole seeds."""
    ids = []
    if channel_cache and os.path.exists(channel_cache):
        with open(channel_cache) as f:
            processed = json.load(f).get("processed", {})
        ids += [cid for cid, v in processed.items() if v.get("verdict") in verdicts]
    if seed_queue and os.path.exists(seed_queue):
        with open(seed_queue) as f:
            for v in json.load(f).values():
                m = re.search(r"/channel/(UC[\w-]{22})", v.get("link", ""))
                if m:
                    ids.append(m.group(1))
    return list(dict.fromkeys(ids))                         # dedupe, keep order


def yt_get(endpoint, params, key):
    q = dict(params, key=key)
    url = f"https://www.googleapis.com/youtube/v3/{endpoint}?" + urllib.parse.urlencode(q)
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 3:
                time.sleep(10 * (attempt + 1))
                continue
            ERRORS[f"youtube_http_{e.code}"] += 1
            if e.code == 403:
                _warn("YouTube API 403 — quota exhausted or key invalid")
            return {}
        except Exception as e:
            ERRORS[f"youtube_{type(e).__name__}"] += 1
            return {}
    return {}


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def collect_youtube(channel_ids, videos_per_channel=10, key=None):
    """~1 unit per 50 channels + 1 unit per channel + 1 unit per 50 videos."""
    key = key or os.environ.get("YOUTUBE_API_KEY", "")
    if not key:
        _warn("YOUTUBE_API_KEY not set — skipping YouTube descriptions")
        ERRORS["youtube_no_key"] += 1
        return []
    print(f"  YouTube: {len(channel_ids)} channels × ≤{videos_per_channel} recent videos")
    uploads = {}
    for batch in _chunks(channel_ids, 50):
        data = yt_get("channels", {"part": "contentDetails", "id": ",".join(batch),
                                   "maxResults": 50}, key)
        for it in data.get("items", []):
            uploads[it["id"]] = it["contentDetails"]["relatedPlaylists"]["uploads"]
    vids = []                                               # (video_id, channel_id)
    for i, (cid, pl) in enumerate(uploads.items(), 1):
        data = yt_get("playlistItems", {"part": "contentDetails", "playlistId": pl,
                                        "maxResults": videos_per_channel}, key)
        vids += [(it["contentDetails"]["videoId"], cid) for it in data.get("items", [])]
        if i % 50 == 0:
            print(f"    {i}/{len(uploads)} channels · {len(vids)} videos")
    chan_of = dict(vids)
    out = []
    for batch in _chunks([v for v, _ in vids], 50):
        data = yt_get("videos", {"part": "snippet", "id": ",".join(batch)}, key)
        for it in data.get("items", []):
            sn = it["snippet"]
            out.append({
                "id": it["id"], "source": "youtube_description", "kind": "text",
                "channel_id": chan_of.get(it["id"], sn.get("channelId")),
                "url": f"https://www.youtube.com/watch?v={it['id']}",
                "raw": f"{sn.get('title', '')}\n\n{sn.get('description', '')}",
                "published_at": sn.get("publishedAt"), "fetched_at": _now(),
            })
    print(f"    {len(out)} video descriptions")
    return out


# ── ENTRY ────────────────────────────────────────────────────────────────────
def collect(out_path, brands_csv=None, channel_cache=None, seed_queue=None,
            pages_per_site=6, max_sites=None, max_channels=300, videos_per_channel=10):
    records = []
    if brands_csv:
        records += collect_brand_sites(brands_csv, pages_per_site, max_sites)
    if channel_cache or seed_queue:
        ids = read_channel_ids(channel_cache, seed_queue)[:max_channels]
        records += collect_youtube(ids, videos_per_channel)
    if not records:
        sys.exit("Nothing collected — pass --brands-csv and/or --channel-cache/--seed-queue.")
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    by_src = Counter(r["source"] for r in records)
    print(f"\n  ✅ {len(records)} raw records → {out_path}  {dict(by_src)}")
    if ERRORS:
        print(f"  ⚠ fetch issues (counted, not silent): {dict(ERRORS.most_common())}")
    return records
