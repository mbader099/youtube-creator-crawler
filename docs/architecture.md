# Architecture

Four stages, each a standalone CLI that also composes into one pipeline. State
lives in a Google Sheet (two tabs: a deduped **Brands** master and an append-only
**Sightings** log) plus a few small local JSON files that are git-ignored.

```
 ┌──────────────┐  passers   ┌──────────────────┐  new creators  ┌───────────────┐
 │ 1. FIND      │──────────▶│ 2. CRAWL         │──────────────▶│ 3. SOURCE     │
 │ keyword      │ seed_queue │ rabbit-hole BFS  │  (API-confirmed)│ sponsor links │
 │ search (API) │◀───────────│ over related-vid │                 │ from video    │
 └──────────────┘  re-seed   │ sidebar (browser)│                 │ descriptions  │
                             └──────────────────┘                 └──────┬────────┘
                                                                         │ Brands + Sightings
                                                                         ▼
                             ┌──────────────────┐   enriched rows  ┌───────────────┐
                             │ 5. CONTACT       │◀─────────────────│ 4. ENRICH     │
                             │ OSINT → rank →   │                  │ homepage →    │
                             │ verify → write   │                  │ objective/fit │
                             └──────────────────┘                  └───────────────┘
```

| Stage | Module | Input → Output |
|---|---|---|
| Find | `influncers_yt_finderScript.py` | keyword list → channels passing size/engagement/recency filters → sheet + `seed_queue.json` |
| Crawl | `rabbit_hole_crawler.py` | seed videos → creators reachable only by *association* (never surface in search) |
| Source | `brand_sourcer.py` | creator → sponsor brands detected in recent video descriptions |
| Enrich | `brand_enricher.py` | brand URL → objective, what they sell, niche fit |
| Contact | `brand_contact_finder.py` + `email_verifier.py` + `email_finder.py` | brand → ranked decision-maker contacts with verified emails |

`config.py` is the single registry for API keys, endpoints, and per-provider rate
limits, plus the hardened HTTP client every stage shares.

## Engineering

### Queue-based BFS crawl
`crawl()` walks YouTube's related-videos graph breadth-first from a `deque` of
`(video_id, hop)` pairs, bounded by `--hops`. Neighbours are only enqueued if they
pass a cheap **Tier-0 glance** (on-niche title, view floor/ceiling, under the
per-node `--expand-cap`), so the frontier stays small. Hard stops: empty frontier,
`--max-pages` ceiling, or `--target` discoveries reached. Discoveries are then
**Tier-1 confirmed** against the YouTube Data API (subs, engagement, recency)
before anything is written.

A persistent **seed queue** (`seed_queue.json`) links runs together: the keyword
finder enqueues every passer as `pending`, `--from-queue` consumes N seeds, and
seeds are only marked `crawled` after a clean run.

### Deduplication (at every layer)
- **Within a crawl:** `visited` video IDs, plus `expanded_channels` so each channel
  is tunnelled from at most once per run.
- **Across runs:** a processed-channel cache, and existing sheet names/URLs are
  loaded up front so known creators are skipped before any API spend.
- **Brands:** canonical-name normalisation merges variants (`NordVPN` / `nordvpn.com`);
  sightings dedupe on `(brand, influencer, link)`; the Brands tab keeps one row per
  brand with live `Times Spotted` / `# Influencers` counts. Brands seen in 2+
  videos are tagged as recurring sponsors.
- **Junk filter:** affiliate networks, URL shorteners, link aggregators, socials,
  gov/edu TLDs, and DNS-dead domains are dropped before they reach the sheet.

### Rate limiting & backoff
- Every HTTP client sends a real browser User-Agent (Cloudflare-fronted APIs 403 the
  default `Python-urllib` agent).
- HTTP 429 → retry with backoff that honours `retry-after` (or the provider's
  "try again in Xs" hint); every request has an explicit timeout.
- Browser navigation is paced with randomised human-like delays (3–8 s).
- Failures are **counted and surfaced, never swallowed**: a broken key or exhausted
  quota shows up as a warning, not as silently empty results.
- Metered providers (email verifiers/finders) run as a **cascade**: cheapest local
  checks first (syntax → MX → disposable → role inbox), then paid tiers only on
  inconclusive results, so credits are spent only where they change the answer.

### Anti-bot posture & CAPTCHA handling
- **Circuit breakers** stop the crawl the moment YouTube pushes back:
  `MAX_CONSEC_EMPTY` empty sidebars or `MAX_CONSEC_ERRORS` nav failures in a row
  → bail and keep seeds `pending` for a later retry (no partial writes).
- **CAPTCHA:** `is_flagged()` detects interstitials ("unusual traffic", reCAPTCHA,
  consent walls). The crawler then pauses with a human in the loop: brings the window
  forward, plays an alert, and posts a desktop notification. It resumes only after
  the operator solves it. It never tries to bypass a CAPTCHA automatically.
- Dedicated persistent browser profile (consent cookies survive runs), optional
  `playwright-stealth` (v1 and v2 APIs both supported, degrades gracefully if absent).
- **Escalating fetch ladders** for third-party sites:
  - Enrichment: `crawl4ai` (JS render) → `Scrapling` stealth fetcher → plain `urllib`.
  - Contact pages: `urllib` → `curl_cffi` with a real Chrome TLS/JA3 fingerprint →
    full headless browser. Hosts that 403 once are remembered and skip straight to tier 2.
  - Search: `ddgs` → DuckDuckGo HTML endpoint → Serper (Google SERP) fallback.
- Selectors are defensive: multiple fallbacks per field and per-item `try/except`,
  so one DOM change degrades a field instead of killing the run.

### LLM use: gated and grounded
LLMs (Groq, with a Gemini fallback) are only used as cheap yes/no **gates** (is this
creator on-niche? is this a tech brand?) and to **rank** OSINT candidates. Contact
picks are grounded: every name the model returns must appear in the scraped source
data, so a hallucinated contact is dropped. Every gate has a keyword-rule fallback,
so the pipeline runs with no LLM key at all.

### Security notes
- All secrets come from env vars (or a git-ignored `.env` / `.groq_key`); none are
  hard-coded or logged. Keys are sent in headers/params, never in log output.
- Untrusted scraped text passed to macOS notifications is supplied as `osascript`
  argv, never interpolated into AppleScript source.
- Contact write-back is preview-only until `--commit`; the sourcer and enricher
  support `--dry-run`.
