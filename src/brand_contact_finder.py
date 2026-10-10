"""
Brand contact finder (Stage 1, the "find" half).

Given a brand (name + URL), find the right decision-maker to pitch — primary +
2 alternates — and a usable email for each, then hand those to the verifier
(email_verifier.py) so only safe addresses reach a draft.

    per brand:
      site scrape (bs4)      → mailto: inboxes + email pattern + about/team text
      GitHub commit search   → real employee emails (tech brands) + pattern
      DuckDuckGo LinkedIn     → names + titles (partnerships / influencer / brand mktg)
      theHarvester (optional) → bulk names/emails if the CLI is installed
        ↓
      infer the email pattern → generate candidate addresses for named people
        ↓
      Groq/Gemini ranks primary + 2 alternates (prefers partnership-type titles)
        ↓
      verify the top candidates via the Reoon→Hunter→ZeroBounce cascade
        ↓
      route: valid → draft · risky/unknown → draft + ⚠ EMAIL UNVERIFIED · invalid → skip

Free / uncapped OSINT only. Every source degrades gracefully and WARNS on failure
(never silent). Verifications are capped per brand and short-circuit on catch-all
domains so Reoon credits aren't wasted.

CLI:
    python brand_contact_finder.py "Proton" https://proton.me     # one brand
    python brand_contact_finder.py --csv 3                        # first 3 from Brands.csv
    python brand_contact_finder.py "Proton" https://proton.me --no-verify
"""

import envfile  # noqa: F401  — loads .env before any os.environ reads
import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
import urllib.error
from html.parser import HTMLParser

from config import USER_AGENT, HAS_GROQ, HAS_GEMINI, HAS_SERPER, APIS, get_json, post_json
import email_verifier as ev

try:
    from config import groq_chat, gemini_chat
except Exception:                       # pragma: no cover
    groq_chat = gemini_chat = None

HERE = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(HERE, "Brands.csv")

# Contact-priority titles, split by FUNCTION (rank by the job, not seniority):
#   DIRECT  — the person whose literal job is creator/partner deals (rank 5).
#   PARTNER — a marketing generalist who owns the outcome/budget (rank 4).
# A partnerships desk beats a bigger marketing title, so DIRECT is checked first
# (function over seniority). E.g. a "Partnerships Manager" outranks a "Head of Marketing".
DIRECT_PARTNER_TITLE_RE = re.compile(
    r"partnership|influencer|creator|sponsorship|affiliate|"
    r"brand[\s-]*partner|collaborat|brand ambassador|partner marketing",
    re.I)
PARTNER_TITLE_RE = re.compile(
    r"head of marketing|chief marketing|cmo|vp[\s-]*marketing|"
    r"marketing manager|marketing director|director of marketing|"
    r"brand[\s-]*market|brand[\s-]*awareness|brand manager|"
    r"social|community|growth|content marketing",
    re.I)

# Founder/owner — the acceptable target for a SMALL brand with no marketing team
# (Apollo policy: Partnerships > Head of Marketing > founder for small brands).
FOUNDER_TITLE_RE = re.compile(
    r"\bfounder\b|\bceo\b|\bowner\b|co[\s-]?founder|managing director|\bpresident\b",
    re.I)

# Inbox tiers (target decision-makers, AVOID gatekeepers info@/hello@).
#   PARTNER  — creator/partnership inboxes that route to the right team → keep as a
#              strong fallback (e.g. influencers@brand.com, partnerships@).
#   GATEKEEPER — general inboxes that filter/slow you down → never pick as primary.
PARTNER_INBOXES = {"influencers", "influencer", "partnerships", "partnership",
                   "partner", "creators", "creator", "sponsorship", "sponsorships",
                   "collab", "collabs", "collaborations", "brandpartnerships",
                   "brand", "marketing", "media", "pr"}
GATEKEEPER_INBOXES = {"info", "hello", "hi", "contact", "support", "help", "admin",
                      "office", "mail", "enquiries", "inquiries", "general", "team",
                      "noreply", "no-reply", "donotreply", "sales", "billing",
                      "careers", "jobs", "hr", "press"}
# Any shared inbox — used to avoid inferring a personal email pattern from one.
ALL_ROLE_INBOXES = PARTNER_INBOXES | GATEKEEPER_INBOXES | set(ev.ROLE_LOCALPARTS)
ROLE_LOCALPARTS = ALL_ROLE_INBOXES

# Gatekeepers are dropped UNLESS they're a brand's only contact (then something
# beats nothing). No-reply boxes can't receive a pitch, so never use them. Among
# the rest, prefer the most general-purpose for a cold pitch (hello@ over billing@).
_NO_REPLY = {"noreply", "no-reply", "donotreply"}
_GATEKEEPER_PREF = ["hello", "hi", "contact", "info", "general", "enquiries",
                    "inquiries", "team", "office", "mail", "support", "help", "admin"]

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# Pages most likely to carry people + emails.
CONTACT_HINTS = ("contact", "about", "team", "company", "people", "leadership",
                 "our-story", "who-we-are", "press", "management", "staff")


def _warn(msg):
    print(f"  ⚠ {msg}", file=sys.stderr)


# ── FETCH + PARSE (bs4-free: stdlib HTMLParser keeps hrefs + mailto) ──────────
class _LinkTextParser(HTMLParser):
    """Collect <a href> targets and a flat visible-text blob in one pass."""
    def __init__(self):
        super().__init__()
        self.links = []
        self._text = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        if tag == "a":
            for k, v in attrs:
                if k == "href" and v:
                    self.links.append(v)

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            t = data.strip()
            if t:
                self._text.append(t)

    @property
    def text(self):
        return re.sub(r"\s+", " ", " ".join(self._text))


_BOT_BLOCK_CODES = {403, 429, 503}          # "you look like a bot" HTTP codes
_BLOCKED_DOMAINS = set()                     # domains that 403 urllib → skip straight to curl_cffi


def _fetch_impersonate(url, timeout=15):
    """Tier 2: curl_cffi with a real Chrome TLS/JA3 fingerprint. Beats the common
    Cloudflare 403 (which is TLS-fingerprint based) WITHOUT a browser. Silently
    skipped if the lib isn't installed."""
    try:
        from curl_cffi import requests as creq
    except Exception:
        return None
    try:
        r = creq.get(url, impersonate="chrome", timeout=timeout)
        if r.status_code < 400 and r.text:
            print(f"  · curl_cffi rescued {url} (HTTP {r.status_code})")
            return r.text[:500_000]
        _warn(f"curl_cffi {url}: HTTP {r.status_code}")
    except Exception as e:
        _warn(f"curl_cffi failed {url}: {e}")
    return None


def _fetch_browser(url, timeout=15):
    """Tier 3: real stealth browser (reuses the crawler's make_playwright_cm + launch,
    which handle the v1/v2 stealth split and a persistent profile). Passes JS
    'checking your browser' challenges. Skipped if Playwright isn't set up."""
    try:
        from rabbit_hole_crawler import make_playwright_cm, launch
    except Exception as e:
        _warn(f"playwright fallback unavailable: {e}")
        return None
    html = None
    try:
        with make_playwright_cm() as pw:
            ctx, page = launch(pw, headless=True)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
                page.wait_for_timeout(2500)     # let a JS challenge resolve
                html = page.content()
            finally:
                ctx.close()
    except Exception as e:
        _warn(f"playwright failed {url}: {e}")
        return None
    if html:
        print(f"  · playwright rescued {url}")
        return html[:500_000]
    return None


def _fetch(url, timeout=15):
    """Raw HTML (keeps hrefs). Escalation ladder: urllib → curl_cffi (TLS
    impersonation) → Playwright stealth, escalating only on a bot-block (403/429/503).
    Cheap path handles the majority; the browser spins up only for stubborn sites.
    Once a domain 403s, it's remembered so later pages skip the doomed urllib attempt
    (one 403 warning per domain, not per page). None if every tier fails."""
    host = urllib.parse.urlparse(url).netloc.lower()
    if host not in _BLOCKED_DOMAINS:            # known Cloudflare host → straight to curl_cffi
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read(500_000).decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            if e.code not in _BOT_BLOCK_CODES:
                _warn(f"fetch failed {url}: {e}")
                return None
            if e.code == 403:                   # persistent bot-wall (429/503 are transient)
                _BLOCKED_DOMAINS.add(host)
                _warn(f"{host}: HTTP 403 (bot-block?) — escalating; "
                      f"skipping urllib for this domain from now on")
            else:
                _warn(f"{url}: HTTP {e.code} (bot-block?) — escalating fetch")
        except Exception as e:
            _warn(f"fetch failed {url}: {e}")
            return None
    # bot-blocked (or known-blocked host) → tier 2 (curl_cffi), then tier 3 (browser)
    return _fetch_impersonate(url, timeout) or _fetch_browser(url, timeout)


def _emails_from(html, domain):
    """All emails in the HTML (mailto: + inline text) belonging to the domain."""
    found = set()
    for m in re.findall(r'mailto:([^"\'>?]+)', html, re.I):
        found.add(m.strip().lower())
    for m in EMAIL_RE.findall(html):
        found.add(m.lower())
    dom = domain.lower()
    return {e for e in found if e.endswith("@" + dom) or e.endswith("." + dom)}


# ── SOURCE 1: company-site scrape ────────────────────────────────────────────
def scrape_site(url, domain, max_pages=5):
    """Homepage + a few contact/about/team pages → {emails, text}."""
    result = {"emails": set(), "text": ""}
    home = _fetch(url)
    if not home:
        return result
    p = _LinkTextParser()
    try:
        p.feed(home)
    except Exception:
        pass
    result["emails"] |= _emails_from(home, domain)
    blob = [p.text[:4000]]

    # pick contact-ish links on the homepage
    base = url.rstrip("/")
    seen, todo = set(), []
    for href in p.links:
        low = href.lower()
        if any(h in low for h in CONTACT_HINTS):
            full = href if href.startswith("http") else urllib.parse.urljoin(base + "/", href)
            if full.split("#")[0] not in seen and domain in full:
                seen.add(full.split("#")[0])
                todo.append(full)
    for link in todo[:max_pages]:
        html = _fetch(link)
        if not html:
            continue
        result["emails"] |= _emails_from(html, domain)
        pp = _LinkTextParser()
        try:
            pp.feed(html)
            blob.append(f"[{link}] " + pp.text[:3000])
        except Exception:
            pass
        time.sleep(0.4)                 # be polite
    result["text"] = "\n".join(blob)[:9000]
    return result


# ── SOURCE 2: GitHub commit-email search (tech brands) ───────────────────────
def github_contacts(domain, cap=15):
    """Real employee emails via GitHub's commit search (author-email:@domain).
    Uses GITHUB_TOKEN if present (higher rate); works keyless at low rate."""
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    headers = {"Accept": "application/vnd.github.cloak-preview+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        data = get_json("https://api.github.com/search/commits",
                        {"q": f"author-email:@{domain}", "per_page": cap},
                        headers=headers, retries=1)
    except Exception as e:
        _warn(f"github search skipped ({domain}): {e}")
        return []
    people = {}
    for item in (data.get("items") or []):
        author = ((item.get("commit") or {}).get("author") or {})
        name, mail = author.get("name", ""), (author.get("email") or "").lower()
        if mail.endswith("@" + domain) and "@" in mail:
            people.setdefault(mail, name or "")
    return [{"name": n, "email": e, "title": "", "source": "github"}
            for e, n in people.items()]


# ── SOURCE 3: DuckDuckGo LinkedIn X-ray ──────────────────────────────────────
def _parse_linkedin_title(title, brand):
    """'Jane Doe - Head of Partnerships @ Brand | LinkedIn' → (name, title)."""
    t = re.sub(r"\s*\|\s*LinkedIn.*$", "", title, flags=re.I)
    parts = [x.strip() for x in re.split(r"\s[-–]\s", t) if x.strip()]
    if len(parts) < 2:
        return None
    name = parts[0]
    role = re.sub(r"\s*@.*$", "", parts[1]).strip()      # drop '@ Brand'
    if not name or "linkedin" in name.lower():
        return None
    return name, role


def _serper_linkedin(query, cap):
    """Fallback X-ray via Serper (real Google results) for when DuckDuckGo throttles
    or returns nothing. Search-only, so no sender-reputation risk. Returns rows in
    the same {title, body} shape as the DDG paths. Google's LinkedIn index is also
    deeper than DDG's, so this doubles as a coverage win (#16d)."""
    if not HAS_SERPER:
        return []
    data = None
    for attempt in range(2):
        try:
            data = post_json(APIS["serper"]["base"], {"q": query, "num": cap * 2},
                             headers={"X-API-KEY": APIS["serper"]["key"]})
            break
        except Exception as e:
            # Serper free tier rate-limits with a misleading 400 'Query pattern not
            # allowed'. It's intermittent — one short pause + retry usually clears it.
            if attempt == 0 and ("400" in str(e) or "not allowed" in str(e)):
                time.sleep(1.5)
                continue
            _warn(f"serper x-ray failed: {e}")
            return []
    rows = []
    for r in (data.get("organic") or []):
        if "linkedin.com/in" not in (r.get("link") or "").lower():
            continue                            # keep only individual /in profiles
        rows.append({"title": r.get("title", ""), "body": r.get("snippet", "")})
    return rows


def _ddg_linkedin(query, brand, cap, source, serper_query=None):
    """Run one LinkedIn X-ray query (ddgs lib → DDG HTML endpoint → Serper fallback),
    filter to this brand, and parse name + title from each result. serper_query is a
    SIMPLIFIED query for the Serper fallback — its free tier rejects long boolean
    site: queries ('Query pattern not allowed'), so we hand it a short one."""
    rows = []
    try:
        from ddgs import DDGS
        with DDGS() as d:
            rows = list(d.text(query, max_results=cap * 2))
    except Exception as e:
        _warn(f"ddgs x-ray unavailable ({e}); trying html endpoint")
        html = _fetch("https://html.duckduckgo.com/html/?" +
                      urllib.parse.urlencode({"q": query}))
        if html and "result__a" in html:
            for t in re.findall(r'result__a[^>]*>(.*?)</a>', html, re.S):
                rows.append({"title": re.sub(r"<[^>]+>", "", t), "body": ""})

    if not rows:                            # both DDG paths came back empty/throttled
        serp = _serper_linkedin(serper_query or query, cap)
        if serp:
            print(f"  · serper fallback: {len(serp)} result(s)")
            rows = serp

    people, bl = [], brand.lower()
    for r in rows:
        title = re.sub(r"\s+", " ", r.get("title", "")).strip()
        # Brand must be in the profile TITLE (their CURRENT company), not just the
        # snippet — a snippet-only mention drags in people from other companies
        # (e.g. a 'Partnerships Manager at Moët Hennessy' when searching Ranvoo).
        if bl not in title.lower():
            continue
        parsed = _parse_linkedin_title(title, brand)
        if parsed:
            people.append({"name": parsed[0], "title": parsed[1],
                           "email": "", "source": source})
        if len(people) >= cap:
            break
    return people


def ddg_xray(brand, cap=6):
    """LinkedIn profiles for the brand's partnership/marketing decision-makers.
    Leads with FUNCTION terms (partnerships/influencer/creator/sponsorship) so the
    partnerships desk actually surfaces into the candidate pool — ranking can only
    reorder who's found, not conjure who isn't (see _tier_rank). One generalist
    term ('head of marketing') is kept as a fallback for brands with no partnerships
    person. Kept to ONE query on purpose — DuckDuckGo rate-limits, so a second call
    would just double the throttle risk."""
    q = (f'site:linkedin.com/in "{brand}" '
         f'(partnerships OR influencer OR creator OR sponsorship '
         f'OR "head of marketing")')
    # Serper free tier REJECTS the site: operator ('Query pattern not allowed'), so
    # give it a plain-keyword query; _serper_linkedin filters to /in profile links.
    serper_q = f'"{brand}" (partnerships OR influencer) linkedin'
    return _ddg_linkedin(q, brand, cap, "linkedin", serper_query=serper_q)


def ddg_founder_xray(brand, cap=3):
    """Founder/owner fallback for a SMALL brand with no marketing team."""
    q = f'site:linkedin.com/in "{brand}" (founder OR CEO OR owner OR "co-founder")'
    serper_q = f'"{brand}" (founder OR CEO) linkedin'
    people = _ddg_linkedin(q, brand, cap, "linkedin", serper_query=serper_q)
    for p in people:
        p["tier"] = "founder"
    return people


# ── SOURCE 4: theHarvester (optional CLI) ────────────────────────────────────
def theharvester(domain, cap=20):
    exe = None
    for cand in ("theHarvester", "theharvester"):
        if subprocess.run(["which", cand], capture_output=True).returncode == 0:
            exe = cand
            break
    if not exe:
        return []                       # not installed — silently optional
    try:
        out = subprocess.run([exe, "-d", domain, "-b", "duckduckgo,bing,crtsh"],
                             capture_output=True, text=True, timeout=90)
        mails = {m.lower() for m in EMAIL_RE.findall(out.stdout)
                 if m.lower().endswith("@" + domain)}
        return [{"name": "", "email": e, "title": "", "source": "harvester"}
                for e in list(mails)[:cap]]
    except Exception as e:
        _warn(f"theHarvester skipped: {e}")
        return []


# ── EMAIL PATTERN INFERENCE + GENERATION ─────────────────────────────────────
def _slug(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z]", "", s.lower())


def split_name(full):
    parts = [p for p in re.split(r"\s+", (full or "").strip()) if p]
    if len(parts) < 2:
        return _slug(parts[0]) if parts else "", ""
    return _slug(parts[0]), _slug(parts[-1])


def infer_pattern(emails, domain):
    """Guess the org's email format from any personal address found.
    Returns an ordered list of format ids to try (best first)."""
    for e in emails:
        local = e.split("@", 1)[0]
        if local in ROLE_LOCALPARTS:
            continue
        if "." in local:
            return ["first.last", "first", "flast"]
        if len(local) > 1 and local.isalpha():
            return ["first", "first.last", "flast"]
    return ["first.last", "first", "flast"]     # sensible default


PLACEHOLDER_NAMES = {"", "unknown", "na", "tbd", "none", "unknownperson",
                     "firstname", "lastname", "name"}


def gen_emails(full, domain, patterns):
    first, last = split_name(full)
    if not first or first in PLACEHOLDER_NAMES:
        return []                       # never fabricate `unknown@domain`
    forms = {
        "first": first,
        "last": last,
        "first.last": f"{first}.{last}" if last else first,
        "flast": f"{first[:1]}{last}" if last else first,
        "firstlast": f"{first}{last}" if last else first,
        "first_last": f"{first}_{last}" if last else first,
    }
    out, seen = [], set()
    for pid in patterns + ["first"]:
        lp = forms.get(pid)
        if lp and lp not in seen:
            seen.add(lp)
            out.append(f"{lp}@{domain}")
    return out[:2]                      # cap per person → protect verify credits


# ── LLM RANK (primary + 2 alternates) ────────────────────────────────────────
def _extract_json(raw):
    """Pull the FIRST complete JSON object out of an LLM reply — robust to ```json
    fences, prose around it, a trailing second object/example ('Extra data'), and
    truncation. Brace-matches from the first '{', ignoring braces inside strings.
    Returns the parsed object, or None (truncated/unbalanced → caller falls back)."""
    if not raw:
        return None
    s = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", raw.strip()).strip()
    start = s.find("{")
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            esc = (ch == "\\" and not esc)
            if ch == '"' and not esc:
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(s[start:i + 1])
                except Exception:
                    return None
    return None                             # never closed → truncated


def _llm_json(prompt):
    """Groq → Gemini → None. Returns parsed JSON or None (caller falls back)."""
    for name, fn, ok in (("groq", groq_chat, HAS_GROQ),
                         ("gemini", gemini_chat, HAS_GEMINI)):
        if not (ok and fn):
            continue
        try:
            raw = fn([{"role": "user", "content": prompt}], max_tokens=1000) \
                if name == "groq" else fn(prompt, max_tokens=1000)
            data = _extract_json(raw)
            if data is not None:
                return data
            _warn(f"{name} returned no parseable JSON (truncated/none)")
        except Exception as e:
            _warn(f"{name} rank failed: {e}")
    return None


def _tier_rank(p):
    """Note-41 contact priority, ranked by FUNCTION not seniority: named deal-maker >
    named marketing generalist > partner inbox > founder > other named > gatekeeper."""
    title = p.get("title", "")
    named = bool(p.get("name"))
    if named and DIRECT_PARTNER_TITLE_RE.search(title):
        return 5                                  # creator/partnership deal-maker
    if named and PARTNER_TITLE_RE.search(title):
        return 4                                  # marketing generalist (owns budget)
    if p.get("tier") == "partner_inbox":
        return 3                                  # influencers@ / partnerships@ inbox
    if p.get("tier") == "founder" or FOUNDER_TITLE_RE.search(title):
        return 2                                  # founder/owner (small-brand fallback)
    if named:
        return 1                                  # some other named person
    return 0


def _ground_contacts(picks, people, site_text):
    """Anti-hallucination guardrail: a NAMED contact the ranker returns must have its
    name actually appear in the source (the candidate people or the site text). The
    LLM sometimes invents plausible people ('Alex - Head of Marketing') when the real
    candidate pool is thin — this drops them so we never draft to a made-up address.
    Unnamed (inbox) contacts always pass."""
    src = " ".join([(p.get("name") or "") for p in people]).lower() + " " + (site_text or "").lower()
    out = []
    for c in picks:
        name = (c.get("name") or "").strip()
        first = name.split()[0].lower() if name else ""
        if first and len(first) > 1 and first not in src:
            _warn(f"dropped invented contact '{name}' (name not in source data)")
            continue
        out.append(c)
    return out


def rank_contacts(brand, domain, people, mailtos, site_text):
    """Pick primary + 2 alternates. LLM if available, else heuristic."""
    roster = []
    for p in people:
        roster.append({k: p.get(k, "") for k in
                       ("name", "title", "email", "source", "tier")})
    prompt = (
        "You are picking who to cold-email at a brand to pitch a YouTube creator "
        "sponsorship. Rank by FUNCTION, not seniority — the partnerships desk beats "
        "a bigger marketing title. Priority:\n"
        "1. A NAMED person whose core job is creator/influencer/brand PARTNERSHIPS "
        "(Partnerships Manager, Influencer Marketing, Creator Partnerships, Brand "
        "Partnerships, Sponsorships, Affiliate). Rank these ABOVE more senior "
        "marketing generalists.\n"
        "2. A NAMED marketing generalist who owns the outcome/budget (Head of "
        "Marketing, CMO, VP/Director Marketing, Marketing Manager, Brand Manager, "
        "Head of Brand Awareness). Pick only if no partnerships-specific person exists.\n"
        "3. A partnership/creator INBOX (tier 'partner_inbox', e.g. influencers@, "
        "partnerships@).\n"
        "4. For a SMALL brand with no marketing team, the founder/CEO/owner "
        "(tier 'founder').\n"
        "NEVER pick a generic gatekeeper inbox (info@, hello@, support@).\n"
        "Return STRICT JSON only:\n"
        '{"contacts":[{"name":"","title":"","email":"","why":""}],'
        '"email_pattern":""}\n'
        "Up to 3 contacts, best first. email_pattern is one of first.last|first|"
        "flast|'' if unknown. If a person's real name is not in the data, set "
        "\"name\":\"\" — NEVER write 'Unknown'. Leave \"email\":\"\" unless it "
        "appears verbatim in the data.\n"
        f"EXCLUDE anyone who clearly works at a different company than {domain} "
        "(watch for similarly-named brands, e.g. a '.ai' look-alike).\n\n"
        f"BRAND: {brand}  DOMAIN: {domain}\n"
        f"CANDIDATE PEOPLE (from OSINT): {json.dumps(roster)[:3500]}\n"
        f"EMAILS FOUND ON SITE: {json.dumps(sorted(mailtos))[:800]}\n"
        f"SITE TEXT (about/team/contact excerpts): {site_text[:2500]}\n")
    data = _llm_json(prompt)
    if data and isinstance(data.get("contacts"), list) and data["contacts"]:
        picks = _ground_contacts(data["contacts"][:3], people, site_text)
        if picks:
            return picks, (data.get("email_pattern") or "")
        _warn("LLM returned only ungrounded/invented contacts — using heuristic")

    # heuristic fallback — never fail silently, just degrade
    _warn("LLM ranking unavailable — using heuristic (Note-41 tier) ranking")
    scored = sorted(people, key=lambda p: (_tier_rank(p), bool(p.get("email"))),
                    reverse=True)
    picks = [{"name": p.get("name", ""), "title": p.get("title", ""),
              "email": p.get("email", ""), "why": "keyword match"}
             for p in scored[:3]]
    return picks, ""


# ── ORCHESTRATION ────────────────────────────────────────────────────────────
def find_brand_contacts(brand, url, verify=True, max_verify=3, manual_contact=None):
    domain = urllib.parse.urlparse(url if "://" in url else "http://" + url).netloc
    domain = re.sub(r"^www\.", "", domain).lower()
    print(f"\n══ {brand}  ({domain}) ══")

    people, mailtos = [], set()

    site = scrape_site(url, domain)
    mailtos |= site["emails"]
    print(f"  · site: {len(site['emails'])} email(s), {len(site['text'])} chars text")

    gh = github_contacts(domain)
    if gh:
        print(f"  · github: {len(gh)} employee email(s)")
    people += gh
    mailtos |= {p["email"] for p in gh if p.get("email")}

    ddg = ddg_xray(brand)
    if ddg:
        print(f"  · linkedin x-ray: {len(ddg)} profile(s)")
    people += ddg

    th = theharvester(domain)
    if th:
        print(f"  · theHarvester: {len(th)} email(s)")
    people += th
    mailtos |= {p["email"] for p in th if p.get("email")}

    # Small-brand fallback (Apollo policy): if the X-ray surfaced no named
    # marketing/partnership person, look for the founder/owner instead.
    has_named_marketer = any(
        p.get("name") and (DIRECT_PARTNER_TITLE_RE.search(p.get("title", ""))
                           or PARTNER_TITLE_RE.search(p.get("title", "")))
        for p in people)
    if not has_named_marketer:
        founders = ddg_founder_xray(brand)
        if founders:
            print(f"  · founder x-ray: {len(founders)} profile(s)")
        people += founders

    # Inbox tiers: keep PARTNER inboxes (influencers@, partnerships@) as
    # a strong fallback; drop gatekeepers (info@, hello@) — UNLESS they're the only
    # contact this brand has, in which case a general inbox beats nothing.
    gatekeepers = []
    for e in mailtos:
        lp = e.split("@", 1)[0].lower()
        if lp in PARTNER_INBOXES:
            people.append({"name": "", "title": f"partnerships inbox ({lp}@)",
                           "email": e, "source": "site", "tier": "partner_inbox"})
        elif lp in GATEKEEPER_INBOXES and lp not in _NO_REPLY:
            gatekeepers.append((e, lp))

    # Last resort: no named person and no partner inbox → use the best gatekeeper.
    if gatekeepers and not any(p.get("name") or p.get("tier") == "partner_inbox"
                               for p in people):
        gatekeepers.sort(key=lambda x: _GATEKEEPER_PREF.index(x[1])
                         if x[1] in _GATEKEEPER_PREF else len(_GATEKEEPER_PREF))
        e, lp = gatekeepers[0]
        people.append({"name": "", "title": f"general inbox ({lp}@)",
                       "email": e, "source": "site", "tier": "gatekeeper"})
        print(f"  · last resort: only a gatekeeper inbox found — using {lp}@")

    contacts, llm_pattern = rank_contacts(brand, domain, people, mailtos, site["text"])
    if manual_contact and manual_contact.get("email"):
        # A user-supplied contact (e.g. hand-found via Gemini) is forced to PRIMARY.
        # DDG X-ray misses partnerships people on regional entities, so this routes a
        # hand-found lead through the same verify → color → draft guardrails, while the
        # X-ray/site results below still fill the Alt Contact columns.
        manual_contact.setdefault("manual", True)
        me = (manual_contact.get("email") or "").lower()
        contacts = [manual_contact] + [c for c in contacts
                                       if (c.get("email") or "").lower() != me]
        print(f"  · manual contact forced primary: "
              f"{manual_contact.get('name') or '(no name)'} <{manual_contact['email']}>")
    patterns = ([llm_pattern] if llm_pattern else []) + infer_pattern(mailtos, domain)

    # assemble candidate emails — ALWAYS on the target domain. Only trust an
    # LLM-supplied address if it was actually found in the wild (mailtos/github);
    # otherwise regenerate from the name so we never email a look-alike domain.
    for c in contacts:
        if c.get("manual"):
            c["candidates"] = [c["email"]] if c.get("email") else []
            continue                            # trust the user's address verbatim (any domain)
        prov = (c.get("email") or "").strip().lower()
        is_gatekeeper = prov.split("@")[0] in GATEKEEPER_INBOXES if prov else False
        if c.get("tier") == "gatekeeper" and prov:
            c["candidates"] = [prov]            # last-resort inbox: trust its real address
        elif prov and prov in mailtos and prov.endswith("@" + domain) and not is_gatekeeper:
            c["candidates"] = [prov]            # real found address (incl. partner inbox)
        elif c.get("name"):
            c["candidates"] = gen_emails(c["name"], domain, patterns)
        else:
            c["candidates"] = []                # gatekeeper/no-name → not a target
        c["email"] = c["candidates"][0] if c["candidates"] else ""

    if verify:
        _verify_contacts(contacts, domain, max_verify)
        _enrich_risky_with_finder(contacts, domain)

    _print_brand(brand, domain, contacts)
    return {"brand": brand, "domain": domain, "contacts": contacts,
            "site_text": site["text"]}


def _enrich_risky_with_finder(contacts, domain):
    """Spend at most ONE finder credit (Prospeo/Snov) per brand, and ONLY on the
    PRIMARY contact — the person who will fill Decision-Maker / Contact Email and
    who you'll actually email. Never on alternates (conserve scarce finder credits).
    No spend at all if the primary is already SMTP-valid or is a nameless inbox.
    Database lookup = reputation-safe."""
    import email_finder as ef
    c = _pick_writable(contacts)                    # the one person we'll email
    if not c or c.get("manual"):
        return                                      # user supplied this email; don't spend a credit
    st = (c.get("verify") or ("unknown", ""))[0]
    name = (c.get("name") or "").strip()
    if st == "valid" or " " not in name:            # already confirmed, or nameless inbox
        return
    res = ef.find_email(name.split()[0], name.split()[-1], domain)
    if res:
        c["found_email"] = res["email"]
        c["found_confidence"] = res["confidence"]
        c["found_provider"] = res["provider"]
        if res["email"] != (c.get("email") or "").lower():
            c["email"] = res["email"]               # database email beats a guess
        print(f"    ✔ finder: {name} → {res['email']} "
              f"({res['confidence']} via {res['provider']})")


def _verify_contacts(contacts, domain, max_verify):
    """Verify top candidate emails; short-circuit once a domain proves catch-all."""
    spent, catch_all = 0, False
    for c in contacts:
        c["verify"] = None
        for email in c.get("candidates", []):
            if not email:
                continue
            if catch_all:
                c["verify"] = ("risky", "catch_all_domain")
                break
            if spent >= max_verify:
                c["verify"] = ("unknown", "verify_cap_reached")
                break
            res = ev.verify_email(email)
            spent += 1
            c["email"], c["verify"] = email, (res.status, res.reason)
            if res.reason == "catch_all":
                catch_all = True
                break                   # every variant on a catch-all domain is catch_all
            if res.status == "valid":
                break                   # keep this address
            # invalid → try this person's next candidate pattern


def _print_brand(brand, domain, contacts):
    labels = {"valid": "✅", "invalid": "⛔", "risky": "⚠", "unknown": "❓", None: "·"}
    print("  ── contacts (primary first) ──")
    if not contacts:
        print("    (none found — try theHarvester or a manual LinkedIn check)")
    for i, c in enumerate(contacts):
        st = c.get("verify")
        mark = labels.get(st[0] if st else None, "·")
        vtxt = f"{st[0]} [{st[1]}]" if st else "not verified"
        who = c.get("name") or "(name unknown)"
        print(f"    {i+1}. {mark} {who} — {c.get('title','')}")
        print(f"        {c.get('email','(no email)')}   → {vtxt}")
        if c.get("why"):
            print(f"        why: {c['why']}")


# ── SHEET WRITE-BACK (reuses brand_sourcer's service-account connection) ─────
_PLACEHOLDER_CELL = {"", "(find on linkedin)", "(find)", "tbd", "n/a", "-"}


def _writable_ranked(contacts):
    """Contacts with an email, best first. A user-supplied manual contact always
    ranks first (kept even if SMTP says invalid — the user chose it deliberately);
    otherwise valid > risky > unknown, ties keep the original rank order.
    [0]=primary, [1]/[2]=alternates."""
    order = {"valid": 0, "risky": 1, "unknown": 2}
    cands = []
    for i, c in enumerate(contacts):
        st = (c.get("verify") or ("unknown", ""))[0]
        if c.get("manual") and c.get("email"):
            cands.append((0, order.get(st, 3), i, c))       # manual wins, never dropped
        elif c.get("email") and st != "invalid":
            cands.append((1, order.get(st, 3), i, c))
    cands.sort(key=lambda x: (x[0], x[1], x[2]))
    return [c for _, _, _, c in cands]


def _pick_writable(contacts):
    ranked = _writable_ranked(contacts)
    return ranked[0] if ranked else None


def _alt_cell(c):
    """Alt-contact cell: email (+ ⚠ if unverified), then 'name, title' on line 2."""
    if not c or not c.get("email"):
        return ""
    st = (c.get("verify") or ("unknown", ""))[0]
    flag = "" if st == "valid" else "  ⚠"
    who = ", ".join(x for x in ((c.get("name") or "").strip(),
                                (c.get("title") or "").strip()) if x)
    return f"{c['email']}{flag}" + (f"\n{who}" if who else "")


def write_back(result, commit=False, force=False):
    """Write the primary contact into the brand's row on the Brands sheet.
    Blanks-only unless force. Verified-invalid emails are never written; risky/
    unknown are written WITH the ⚠ EMAIL UNVERIFIED marker. Prints the plan and
    changes nothing unless commit=True (dry-run by default)."""
    from datetime import date
    import brand_sourcer as bs

    primary = _pick_writable(result["contacts"])
    tag = "WRITE" if commit else "dry-run"
    if not primary:
        print(f"  (write-back/{tag}) no verifiable contact — nothing to write")
        return
    try:
        ws, hrow = bs.connect_brands_ws()
        grid = ws.get_all_values()
    except SystemExit as e:            # gspread missing
        _warn(f"write-back skipped: {e}")
        return
    except Exception as e:
        _warn(f"write-back could not open sheet: {e}")
        return

    header = grid[hrow - 1] if hrow - 1 < len(grid) else []
    col = {name.strip().lower(): i for i, name in enumerate(header)}
    bcol = col.get("brand", 0)
    want = result["brand"].strip().lower()
    target, existing = None, []
    for ri in range(hrow, len(grid)):
        row = grid[ri]
        if len(row) > bcol and row[bcol].strip().lower() == want:
            target, existing = ri + 1, row       # 1-based row number
            break
    if not target:
        _warn(f"'{result['brand']}' not in Brands tab — add the brand row first, skipping")
        return

    def cur(name):
        i = col.get(name.lower())
        return existing[i].strip() if i is not None and i < len(existing) else ""

    ranked = _writable_ranked(result["contacts"])
    alt1 = ranked[1] if len(ranked) > 1 else None
    alt2 = ranked[2] if len(ranked) > 2 else None
    status = (primary.get("verify") or ("unknown", ""))[0]
    flag = "" if status == "valid" else "  ⚠ EMAIL UNVERIFIED"
    # primary → Decision-Maker/Title/Contact Email; alternates → Alt Contact 1/2
    plan = {"Decision-Maker": primary.get("name", ""),
            "Title": primary.get("title", ""),
            "Contact Email": f"{primary['email']}{flag}",
            "Alt Contact 1": _alt_cell(alt1),
            "Alt Contact 2": _alt_cell(alt2)}
    # which contact backs each email column, for green/red colouring
    _by_col = {"Contact Email": primary, "Alt Contact 1": alt1, "Alt Contact 2": alt2}

    updates = []
    for name, val in plan.items():
        if not val or name.lower() not in col:
            continue
        existing_val = cur(name)
        if force or existing_val.lower() in _PLACEHOLDER_CELL:
            updates.append((name, val))
        else:
            print(f"  (write-back/{tag}) keep {name} = {existing_val!r} "
                  f"(has a value; use --force to overwrite)")

    prov = f"[auto {date.today()}] primary {status}; {len(ranked)} verified contact(s)"

    print(f"  ── write-back ({tag}) → {result['brand']} row {target} ──")
    for name, val in updates:
        a1 = f"{bs._col_letter(col[name.lower()] + 1)}{target}"
        disp = val.replace("\n", " | ")
        if commit:
            ws.update(values=[[val]], range_name=a1)
            print(f"    wrote {a1} [{name}] ← {disp!r}")
        else:
            print(f"    would write {a1} [{name}] ← {disp!r}")
    if "notes" in col:
        a1 = f"{bs._col_letter(col['notes'] + 1)}{target}"
        new_notes = (cur("Notes") + " · " if cur("Notes") else "") + prov
        if commit:
            ws.update(values=[[new_notes]], range_name=a1)
            print(f"    appended Notes {a1}")
        else:
            print(f"    would append Notes {a1} ← '… {prov}'")
    # colour each email cell we wrote, green if that contact's email verified valid
    for cname, contact in _by_col.items():
        if contact and any(n == cname for n, _ in updates):
            cst = (contact.get("verify") or ("unknown", ""))[0]
            color_cell(ws, col, target, cname, cst == "valid", commit=commit)
    if not updates:
        print("    (columns already filled — only Notes provenance would change)")


# ── LIVE SHEET: read by Status + flip Status ─────────────────────────────────
def open_brands_ws():
    """(worksheet, header_row, col_map, grid) for the live Brands tab."""
    import brand_sourcer as bs
    ws, hrow = bs.connect_brands_ws()
    grid = ws.get_all_values()
    header = grid[hrow - 1] if hrow - 1 < len(grid) else []
    col = {h.strip().lower(): i for i, h in enumerate(header)}
    return ws, hrow, col, grid


def read_by_status(grid, hrow, col, status="Sourced", limit=0):
    """Brands whose Status matches, in sheet order, with 1-based row numbers."""
    bi = col.get("brand", 0)
    si = col.get("status")
    ui, ni, oi, ei = (col.get("brand url"), col.get("niche"),
                      col.get("brand objective"), col.get("contact email"))
    want = status.strip().lower()
    out = []
    for ri in range(hrow, len(grid)):
        r = grid[ri]
        if si is None or len(r) <= si or not r[bi].strip():
            continue
        if r[si].strip().lower() != want:
            continue

        def cell(idx):
            return r[idx].strip() if idx is not None and idx < len(r) else ""
        url = cell(ui)
        if url.startswith("="):
            url = ""
        out.append({"brand": r[bi].strip(), "url": url, "niche": cell(ni),
                    "objective": cell(oi), "email": cell(ei), "row": ri + 1})
        if limit and len(out) >= limit:
            break
    return out


def set_cell(ws, col, row, colname, value, commit=True):
    """Set one cell by column name for a 1-based row (dry-run prints; commit writes)."""
    import brand_sourcer as bs
    ci = col.get(colname.strip().lower())
    if ci is None:
        _warn(f"no '{colname}' column — can't update")
        return
    a1 = f"{bs._col_letter(ci + 1)}{row}"
    if commit:
        ws.update(values=[[value]], range_name=a1)
        print(f"  📊 {colname} {a1} → {value}")
    else:
        print(f"  📊 (dry-run) would set {colname} {a1} → {value}")


def set_status(ws, col, row, value, commit=True):
    set_cell(ws, col, row, "Status", value, commit)


_GREEN = {"red": 0.80, "green": 0.94, "blue": 0.80}   # verified
_RED = {"red": 0.96, "green": 0.80, "blue": 0.80}     # unverified


def color_cell(ws, col, row, colname, valid, commit=True):
    """Shade a cell green (verified valid) or red (not), by column name."""
    ci = col.get(colname.strip().lower())
    if ci is None:
        return
    hue = "green ✅" if valid else "red ⚠"
    if not commit:
        print(f"  🎨 (dry-run) would color {colname} row {row} {hue}")
        return
    req = {"repeatCell": {
        "range": {"sheetId": ws.id, "startRowIndex": row - 1, "endRowIndex": row,
                  "startColumnIndex": ci, "endColumnIndex": ci + 1},
        "cell": {"userEnteredFormat": {"backgroundColor": _GREEN if valid else _RED}},
        "fields": "userEnteredFormat.backgroundColor"}}
    ws.spreadsheet.batch_update({"requests": [req]})
    print(f"  🎨 colored {colname} row {row} {hue}")


# ── BRANDS CSV ───────────────────────────────────────────────────────────────
def read_brands_csv(path, limit=0):
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header_seen = False
        cols = {}
        for row in reader:
            if not header_seen:
                if row and row[0].strip() == "Brand":
                    cols = {name.strip(): idx for idx, name in enumerate(row)}
                    header_seen = True
                continue
            if not row or not row[0].strip():
                continue

            def cell(name):
                i = cols.get(name)
                return row[i].strip() if i is not None and i < len(row) else ""
            url = cell("Brand URL")
            if url.startswith("="):     # sheet formula, not a real URL
                url = ""
            rows.append({"brand": cell("Brand"), "url": url,
                         "email": cell("Contact Email"),
                         "niche": cell("Niche"),
                         "objective": cell("Brand Objective")})
            if limit and len(rows) >= limit:
                break
    return rows


# ── CLI ──────────────────────────────────────────────────────────────────────
def main(argv):
    ap = argparse.ArgumentParser(description="Find brand decision-maker contacts.")
    ap.add_argument("brand", nargs="?", help="brand name (single-brand mode)")
    ap.add_argument("url", nargs="?", help="brand homepage URL")
    ap.add_argument("--csv", type=int, metavar="N",
                    help="process first N brands from Brands.csv")
    ap.add_argument("--no-verify", action="store_true", help="skip email verification")
    ap.add_argument("--max-verify", type=int, default=3,
                    help="max verifications per brand (credit guard)")
    ap.add_argument("--write", action="store_true",
                    help="write the primary contact back to the Brands sheet "
                         "(dry-run preview unless --commit)")
    ap.add_argument("--commit", action="store_true",
                    help="with --write: actually write (default is preview only)")
    ap.add_argument("--force", action="store_true",
                    help="with --write: overwrite cells that already have a value")
    args = ap.parse_args(argv)

    verify = not args.no_verify
    if args.write and not verify:
        _warn("write-back needs verified contacts — ignoring --write with --no-verify")
        args.write = False

    def handle(brand, url):
        res = find_brand_contacts(brand, url, verify, args.max_verify)
        if args.write:
            write_back(res, commit=args.commit, force=args.force)
        return res

    if args.csv:
        brands = read_brands_csv(CSV_PATH, args.csv)
        if not brands:
            print("No brands read from CSV.")
            return
        for b in brands:
            if not b["url"]:
                _warn(f"{b['brand']}: no Brand URL in sheet — skipping (add a URL)")
                continue
            handle(b["brand"], b["url"])
    elif args.brand and args.url:
        handle(args.brand, args.url)
    else:
        ap.error("give a brand + url, or --csv N")


if __name__ == "__main__":
    main(sys.argv[1:])
