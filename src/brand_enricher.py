"""
Brand Enricher
====================================
The second half of the pipeline. `brand_sourcer.py` finds brands and drops a
clean homepage URL in the "Brand URL" column (S). This script READS each of
those URLs, figures out what the brand sells + its likely marketing objective,
and writes that back into the Brands tab so every row is pitch-ready.

Job 1 (research the BRAND) only. It does NOT find the contact person — that's
LinkedIn + Apollo, by hand. This just fills:
    - Brand Objective (col D)  -> a heuristic guess (Awareness / Lead Gen / etc.)
    - Notes (col R)            -> appends a one-line homepage summary

It is FREE to run: crawl4ai returns clean page text and we parse it with plain
keyword rules — no LLM tokens, ours or yours.

Three crawl backends, auto-selected in order:
    1. crawl4ai   (preferred) — renders JavaScript, best on modern brand sites.
    2. Scrapling  (anti-bot)  — stealth browser; kicks in when a site serves a
       Cloudflare/anti-bot challenge page instead of real content.
    3. urllib     (fallback)  — no install, no browser; works on simple sites.

Every backend's output is sanity-checked: a "Just a moment..." challenge shell
is treated as BLOCKED, not as content, so junk never gets written to the sheet.

------------------------------------------------------------------------------
ONE-TIME SETUP (on your Mac):
    python3 -m pip install crawl4ai --break-system-packages --user
    python3 -m crawl4ai-setup          # installs the headless browser it needs
    python3 -m pip install scrapling --break-system-packages --user
    scrapling install                  # installs its stealth browser (camoufox)
(Skip either install and the script still runs on whatever is available.)

RUN:
    python3 brand_enricher.py                 # enrich every un-enriched brand
    python3 brand_enricher.py --limit 5       # do the first 5 only (test run)
    python3 brand_enricher.py --dry-run       # show what it found, write nothing
    python3 brand_enricher.py --force         # re-do brands even if already enriched
------------------------------------------------------------------------------
"""

import envfile  # noqa: F401  — loads .env before any os.environ reads
import argparse
import json
import os
import re
import sys
import threading
import time
import html as _html
import urllib.request
import urllib.error
from datetime import datetime

# Reuse ALL config + the Sheets connection from the sourcer (one source of truth).
import brand_sourcer as bs

# Column positions in the new two-tab Brands layout (BRAND_HEADERS):
OBJ_COL = 3     # C  Brand Objective
NOTES_COL = 10  # J  Notes
URL_COL = 11    # K  Brand URL

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


# ── OBJECTIVE HEURISTICS (note 39's five objectives, guessed from copy) ─────────
# Ordered: first match wins, so put the most specific signals first.
OBJECTIVE_RULES = [
    ("Product Launch", re.compile(
        r"\b(introducing|now available|coming soon|pre-?order|just launched|"
        r"new (?:product|collection|release)|the all-new|meet the new)\b", re.I)),
    ("Sales Conversion", re.compile(
        r"\b(shop now|buy now|add to cart|% ?off|discount|coupon|promo code|"
        r"sale ends|order today|free shipping|checkout)\b", re.I)),
    ("Lead Generation", re.compile(
        r"\b(free trial|start (?:for )?free|sign ?up (?:free|today)|get started free|"
        r"book a demo|request a demo|download (?:the )?app|free account|"
        r"create (?:a |your )?(?:free )?account)\b", re.I)),
    ("Market Testing", re.compile(
        r"\b(join the waitlist|early access|beta|give us feedback|help us shape)\b", re.I)),
]
DEFAULT_OBJECTIVE = "Brand Awareness"


def guess_objective(text):
    for label, rx in OBJECTIVE_RULES:
        if rx.search(text):
            return label
    return DEFAULT_OBJECTIVE


# ── NICHE-FIT GATE ──────────────────────────────────────────────────────────────
# A brand we pitch tech/AI creators to should *smell* like tech. If none of these
# signals appear in its homepage copy, the row gets a review flag in Notes —
# flagged, not skipped, because some legit sponsors (VPNs aside) write vague copy.
NICHE_SIGNALS = re.compile(
    r"\b(ai|artificial intelligence|machine learning|llm|software|app|api|saas|"
    r"developer|dev tool|code|coding|cloud|hosting|server|data|automation|"
    r"cyber|security|privacy|vpn|encrypt|password|tech|platform|productivity|"
    r"workflow|open.?source|self.?host|linux|docker|gpu|neural|model)\b", re.I)


def niche_fit(text):
    """True if the page copy shows any tech/AI/software signal."""
    return bool(text) and bool(NICHE_SIGNALS.search(text))


# ── CHALLENGE-PAGE DETECTION ───────────────────────────────────────────────────
# Anti-bot walls (Cloudflare & friends) return a shell page. If we mistake that
# for content, the enricher writes garbage ("Brand Awareness", no tagline).
CHALLENGE_RE = re.compile(
    r"(just a moment|checking your browser|verify you are human|"
    r"enable javascript and cookies|attention required|cloudflare|"
    r"access denied|request blocked|are you a robot|captcha)", re.I)


def looks_blocked(text):
    """True if the fetched 'content' is actually an anti-bot challenge shell."""
    if not text or len(text.strip()) < 80:
        return True
    head = text[:2500]
    return bool(CHALLENGE_RE.search(head))


# ── CRAWLERS ────────────────────────────────────────────────────────────────────
class _FilteredFDs:
    """FD-level filter that KEEPS COLOR. crawl4ai binds the terminal at import, so
    a Python-level redirect misses it. This routes real stdout/stderr (fd 1 & 2)
    through a pseudo-terminal (pty) — so crawl4ai still sees a TTY and emits its
    colors — then drops the 'Call log:' / 'Code context:' blocks and passes
    everything else through LIVE, in color, to the real terminal.
    Falls back to a plain pipe (no color) if a pty can't be opened."""
    _tag  = re.compile(rb"\[[A-Z]{2,}\]")
    _ansi = re.compile(rb"\x1b\[[0-9;]*m")           # strip color codes for matching only

    def __enter__(self):
        sys.stdout.flush(); sys.stderr.flush()
        self._saved_out = os.dup(1)
        self._saved_err = os.dup(2)
        try:
            import pty, termios
            self._reader, wfd = pty.openpty()        # a real TTY -> crawl4ai keeps color
            try:                                     # stop NL -> CR-NL translation
                a = termios.tcgetattr(wfd)
                a[1] &= ~termios.ONLCR
                termios.tcsetattr(wfd, termios.TCSANOW, a)
            except Exception:
                pass
        except Exception:
            self._reader, wfd = os.pipe()            # fallback: filtered but no color
        os.dup2(wfd, 1); os.dup2(wfd, 2)
        os.close(wfd)
        self._skip = False
        self._buf = b""
        self._th = threading.Thread(target=self._pump, daemon=True)
        self._th.start()
        return self

    def _pump(self):
        while True:
            try:
                chunk = os.read(self._reader, 4096)
            except OSError:
                break
            if not chunk:
                break
            self._buf += chunk
            while b"\n" in self._buf:
                line, self._buf = self._buf.split(b"\n", 1)
                self._emit(line + b"\n")
        if self._buf:
            self._emit(self._buf)
            self._buf = b""

    def _emit(self, bline):
        plain = self._ansi.sub(b"", bline)           # match on the un-colored text
        if self._tag.search(plain):                  # tagged line ends a skip block
            self._skip = False
            os.write(self._saved_out, bline)
            return
        low = plain.strip().lower()
        if low.startswith(b"call log:") or low.startswith(b"code context:"):
            self._skip = True                        # drop from here to the next tag
            return
        if not self._skip:
            os.write(self._saved_out, bline)         # original bytes -> color preserved

    def __exit__(self, *a):
        sys.stdout.flush(); sys.stderr.flush()
        os.dup2(self._saved_out, 1)                  # restore real fds (EOFs the pty/pipe)
        os.dup2(self._saved_err, 2)
        self._th.join(timeout=3)
        for fd in (self._reader, self._saved_out, self._saved_err):
            try:
                os.close(fd)
            except OSError:
                pass
        return False


def crawl_with_crawl4ai(url):
    """Return page markdown via crawl4ai, or None. Output is identical to before —
    live and colored — except the 'Call log:' / 'Code context:' blocks are stripped."""
    try:
        import asyncio
        from crawl4ai import AsyncWebCrawler
    except ImportError:
        return None

    async def _run():
        async with AsyncWebCrawler() as crawler:
            res = await crawler.arun(url=url)
            if not res.success:
                return None
            md = getattr(res, "markdown", None)
            if hasattr(md, "raw_markdown"):
                md = md.raw_markdown
            return md or ""

    try:
        with _FilteredFDs():
            return asyncio.run(_run())
    except Exception:
        return None


def crawl_with_scrapling(url):
    """Anti-bot backend: fetch via Scrapling's stealth browser (camoufox).
    Returns (title, description, text) like the urllib fallback, or None if
    Scrapling isn't installed or the fetch failed.

    Install once:  python3 -m pip install scrapling --break-system-packages --user
                   scrapling install
    """
    try:
        try:
            from scrapling.fetchers import StealthyFetcher
        except ImportError:
            from scrapling import StealthyFetcher   # older versions
    except ImportError:
        return None
    try:
        page = StealthyFetcher.fetch(url, headless=True, network_idle=True)
        if getattr(page, "status", 200) >= 400:
            return None
        title = desc = ""
        try:
            t = page.css_first("title::text")
            title = str(t).strip() if t else ""
            d = (page.css_first('meta[name="description"]::attr(content)')
                 or page.css_first('meta[property="og:description"]::attr(content)'))
            desc = str(d).strip() if d else ""
        except Exception:
            pass
        try:
            text = page.get_all_text(ignore_tags=("script", "style"))
        except Exception:
            text = ""
        text = re.sub(r"\s+", " ", text or "")
        return title, desc, text
    except Exception as e:
        print(f"    scrapling error ({type(e).__name__}: {e}); trying next backend.")
        return None


TAG_RE = re.compile(r"<[^>]+>")
META_DESC_RE = re.compile(
    r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']', re.I)
OG_DESC_RE = re.compile(
    r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']', re.I)
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def crawl_with_urllib(url):
    """No-dependency fallback: fetch raw HTML and return (title, description, text)."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read(400_000).decode("utf-8", "ignore")
    except Exception as e:
        return None
    desc = ""
    for rx in (META_DESC_RE, OG_DESC_RE):
        m = rx.search(raw)
        if m:
            desc = _html.unescape(m.group(1)).strip()
            break
    tm = TITLE_RE.search(raw)
    title = _html.unescape(TAG_RE.sub("", tm.group(1))).strip() if tm else ""
    # strip scripts/styles then tags -> rough page text for keyword matching
    body = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.I | re.S)
    text = _html.unescape(TAG_RE.sub(" ", body))
    text = re.sub(r"\s+", " ", text)
    return title, desc, text


URL_IN_NOTES = re.compile(r"https?://[^\s)\]]+", re.I)


def url_from_notes(note):
    """Legacy rows (pre-URL-column) stashed the sponsor link in Notes as
    'Link: https://brand.com/...'. Recover a clean homepage URL from it."""
    if not note:
        return ""
    m = URL_IN_NOTES.search(note)
    if not m:
        return ""
    host = re.sub(r"^https?://", "", m.group(0)).split("/")[0]
    dom = bs.root_domain(host)
    return bs.brand_url_from_domain(dom)


def extract_signals(url):
    """Return dict: {sells, objective, source, niche_ok} for a brand URL.
    None = unreachable. Backend order: crawl4ai -> Scrapling (anti-bot) -> urllib.
    Every backend's output goes through looks_blocked() so a Cloudflare shell is
    never mistaken for real homepage copy."""
    # 1) crawl4ai — fast, renders JS, but loses to anti-bot walls
    md = crawl_with_crawl4ai(url)
    if md and not looks_blocked(md):
        lines = [l.strip("# *_-> ").strip() for l in md.splitlines() if l.strip()]
        sells = next((l for l in lines if 20 <= len(l) <= 160), (lines[0] if lines else ""))
        return {"sells": sells[:160], "objective": guess_objective(md),
                "source": "crawl4ai", "niche_ok": niche_fit(md)}
    if md:
        print("    crawl4ai got an anti-bot challenge page; escalating to Scrapling...")

    # 2) Scrapling — stealth browser, built to walk through anti-bot walls
    sc = crawl_with_scrapling(url)
    if sc:
        title, desc, text = sc
        if not looks_blocked(text or desc or title):
            sells = desc or title
            body = text or desc or title
            return {"sells": sells[:160], "objective": guess_objective(body),
                    "source": "scrapling", "niche_ok": niche_fit(body)}
        print("    scrapling also blocked; trying plain fetch...")

    # 3) urllib — last resort, works on simple sites
    fb = crawl_with_urllib(url)
    if not fb:
        return None
    title, desc, text = fb
    if looks_blocked(text or desc or title):
        return None          # blocked everywhere — report unreachable, write nothing
    sells = desc or title
    body = text or desc or title
    return {"sells": sells[:160], "objective": guess_objective(body),
            "source": "urllib", "niche_ok": niche_fit(body)}


# ── GROQ FALLBACK — when the site is unreachable, describe from knowledge ────────
_VALID_OBJ = {"Product Launch", "Sales Conversion", "Lead Generation",
              "Market Testing", "Brand Awareness"}


def groq_enrich(name, url):
    """Fallback for anti-bot/dead sites: ask Groq to describe the brand from its
    name + URL (no fetch). Returns a sig dict, False if Groq says it's NOT a real
    brand (flag for review), or None if Groq is unavailable / errored."""
    if not getattr(bs, "_HAS_GROQ", False):
        return None
    prompt = (
        f"Brand name: {name!r}. URL: {url!r}.\n"
        f"If this is a REAL, recognizable company, reply with ONLY this JSON:\n"
        f'{{"real": true, "sells": "<one line: what they sell>", '
        f'"objective": "<one of: Product Launch, Sales Conversion, Lead Generation, '
        f'Market Testing, Brand Awareness>", "tech": true or false}}\n'
        f'Set "tech" true ONLY for technology companies (software, SaaS, app, AI, '
        f'hardware, electronics, cybersecurity, VPN, developer tools). Set "tech" '
        f'false for clothing, food, drinks, supplements, wellness, beauty, adult '
        f'products, or other non-tech consumer brands.\n'
        f'If it is NOT a recognizable brand (a link shortener, a fragment, or '
        f'unknown), reply with ONLY: {{"real": false}}')
    payload = json.dumps({
        "model": bs.GROQ_MODEL, "temperature": 0, "max_tokens": 160,
        "messages": [
            {"role": "system", "content": "You are a precise brand analyst. "
             "Reply with JSON only — no prose, no markdown."},
            {"role": "user", "content": prompt},
        ],
    }).encode()
    headers = {"Authorization": f"Bearer {bs.GROQ_API_KEY}",
               "Content-Type": "application/json",
               "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
    for attempt in range(3):
        try:
            req = urllib.request.Request(bs.GROQ_URL, data=payload, headers=headers)
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read())
            txt = data["choices"][0]["message"]["content"].strip()
            m = re.search(r"\{.*\}", txt, re.DOTALL)
            if not m:
                return None
            obj = json.loads(m.group(0))
            if not obj.get("real"):
                return False                       # Groq: not a real brand
            objective = (obj.get("objective") or "").strip()
            if objective not in _VALID_OBJ:
                objective = DEFAULT_OBJECTIVE
            return {"sells": (obj.get("sells") or "")[:160],
                    "objective": objective,
                    "source": "Groq (site blocked — verify)",
                    "niche_ok": bool(obj.get("tech", True))}
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(3 * (attempt + 1))
                continue
            print(f"     ⚠ Groq enrich failed: HTTP {e.code}")
            return None
        except Exception as e:
            print(f"     ⚠ Groq enrich failed: {e}")
            return None
    return None


# ── CORE (reusable — called standalone AND by brand_sourcer's --enrich) ──────────
def enrich_sheet(ws, header_row, limit=0, force=False, dry_run=False):
    """Enrich every un-enriched brand row on an already-open Brands worksheet.

    Returns the number of brands enriched. Reused by brand_sourcer.py so a single
    sourcing run can source + enrich in one go on the same connection.
    """
    values = ws.get_all_values()
    data = values[header_row:]   # rows BELOW the detected header row
    if not data:
        print("  Brands tab has no data rows yet.")
        return 0

    def cell(row, idx):   # 1-based col index, safe on short rows
        return row[idx - 1] if len(row) >= idx else ""

    todo, url_backfills = [], []
    for i, row in enumerate(data):
        sheet_row = header_row + 1 + i   # first data row = header_row + 1
        if not cell(row, 1).strip():
            continue
        url = cell(row, URL_COL).strip()
        note = cell(row, NOTES_COL)
        if not url:
            url = url_from_notes(note)      # backfill legacy rows from their Notes link
            if url:
                url_backfills.append({"range": f"{bs._col_letter(URL_COL)}{sheet_row}",
                                      "values": [[url]]})
        if not url:
            continue
        if cell(row, OBJ_COL).strip() and not force:
            continue        # already enriched
        todo.append((sheet_row, cell(row, 1).strip(), url, note))

    if limit:
        todo = todo[:limit]
    if not todo:
        print("  Nothing to enrich — every brand with a URL already has an objective. "
              "(Use --force to redo them.)")
        return 0

    print(f"  Enriching {len(todo)} brand(s)...\n")
    obj_updates, note_updates = [], []
    for sheet_row, name, url, old_note in todo:
        print(f"  {name:<20} {url}")
        sig = extract_signals(url)
        if not sig:
            g = groq_enrich(name, url)               # fallback: describe from knowledge
            if g is False:
                print("     ⚠ Groq: not a recognizable brand — flagged for review/removal")
                bs.log_skip(name, "enricher: unreachable + Groq couldn't verify (review/remove?)", url)
                continue
            if not g:
                print("     unreachable / blocked on all backends — skipped, nothing written")
                bs.log_skip(name, "enricher: unreachable or anti-bot blocked on all backends", url)
                continue
            if not g.get("niche_ok", True):
                print("     ⚠ Groq: non-tech brand — flagged for review/removal (roster is tech-only)")
                bs.log_skip(name, "enricher: non-tech brand (Groq) — review/remove", url)
                continue
            print("     ↳ site blocked — enriched from Groq knowledge (verify before pitching)")
            sig = g
        stamp = f"Auto-enriched {datetime.now():%Y-%m-%d} via {sig['source']}."
        summary = f" | Homepage: {sig['sells']} [{stamp}]" if sig["sells"] else f" | {stamp}"
        if not sig.get("niche_ok", True):
            summary += " | NICHE FIT? no tech/AI signals on homepage — review before pitching."
            print("     ⚠ niche fit: no tech/AI signals found — flagged for review")
        new_note = (old_note or "") + summary
        print(f"     objective guess: {sig['objective']}")
        print(f"     sells: {sig['sells'] or '(no tagline found)'}")
        obj_updates.append({"range": f"{bs._col_letter(OBJ_COL)}{sheet_row}",
                            "values": [[sig["objective"]]]})
        note_updates.append({"range": f"{bs._col_letter(NOTES_COL)}{sheet_row}",
                             "values": [[new_note]]})

    if dry_run:
        print(f"\n  [dry-run] Would enrich {len(obj_updates)} brand(s)"
              f"{f' and backfill {len(url_backfills)} URL(s)' if url_backfills else ''}. "
              f"Nothing saved.")
        return 0
    batch = obj_updates + note_updates + url_backfills
    if batch:
        ws.batch_update(batch, value_input_option="USER_ENTERED")
        msg = f"\n  Enriched {len(obj_updates)} brand(s) in the '{bs.BRANDS_WS}' tab."
        if url_backfills:
            msg += f" Backfilled {len(url_backfills)} legacy URL(s) from Notes."
        print(msg)
    else:
        print("\n  Nothing written.")
    return len(obj_updates)


# ── MAIN (standalone use) ────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Enrich Brands-tab rows from their homepage URL.")
    ap.add_argument("--limit", type=int, default=0, help="only process the first N un-enriched brands")
    ap.add_argument("--force", action="store_true", help="re-enrich even if Objective is already filled")
    ap.add_argument("--dry-run", action="store_true", help="print findings, write nothing")
    args = ap.parse_args()

    ws, header_row = bs.connect_brands_ws()
    enrich_sheet(ws, header_row, limit=args.limit, force=args.force, dry_run=args.dry_run)
    bs.print_skip_report()   # standalone runs get the audit too


if __name__ == "__main__":
    main()
