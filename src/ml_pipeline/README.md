# ml_pipeline: crawler output → training-ready dataset

Turns the crawler's output into a clean, deduplicated, language- and quality-filtered
JSONL corpus. A small fine-tune then shows the data moves held-out loss.

```
crawler outputs ──collect──▶ raw.jsonl ──clean──▶ ──dedup──▶ ──filter──▶ ──export──▶ training.jsonl
(Brands CSV,                 (HTML +     trafilatura  exact +    quality     {"text": …}    + report.md
 channel_cache.json,          text)      + NFKC +     MinHash/   heuristics  per line       + stats.json
 seed_queue.json)                        PII scrub    LSH        + langdetect
                                                                         └──train──▶ distilgpt2 + metrics.json
```

The crawler's code is never modified. This package only reads the files it leaves on disk.

## Stages

| Module | What it does |
|---|---|
| `collect.py` | **Adapter.** The crawler stores *pointers* (brand URLs, channel IDs), not page text. This follows them: brand homepages plus up to 5 same-site pages (robots.txt respected, per-site pacing, 429 backoff, `curl_cffi` fallback), and recent video descriptions of every channel the finder marked `pass` (YouTube Data API, ~1 unit per channel). |
| `clean.py` | trafilatura main-content extraction (drops nav, footers, cookie banners, scripts). NFKC Unicode normalisation, control/zero-width removal, whitespace collapse. Emails, URLs and `@handles` become `[EMAIL]`/`[URL]`/`[HANDLE]`. |
| `dedup.py` | Exact dedup by SHA-256 of normalised text, then **near-duplicate detection with MinHash + LSH** (`datasketch`; 128 permutations, word 5-gram shingles, Jaccard ≥ 0.8, each LSH candidate confirmed against its MinHash estimate). |
| `filter.py` | Quality heuristics (`too_short`, `symbol_heavy`, `link_heavy`, `repetitive`, `boilerplate`), then language ID with seeded `langdetect` against a configurable keep-set. Each drop gets exactly one reason. |
| `export.py` | `training.jsonl` with exactly `{"text": …}` per line; provenance (id/source/url/lang) in a line-aligned `training.meta.jsonl` sidecar. |
| `report.py` | Coverage/quality report: funnel, drops by reason, dedup rate and example pairs, language breakdown, per-source yield, final size. |
| `train.py` | Fine-tunes `distilgpt2` (1 epoch, 500-record sample by default) and logs **held-out** eval loss/perplexity before vs after. |

## Run it

From the repo root (Python 3.10+). No system packages are needed; every dependency
ships prebuilt wheels.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r src/ml_pipeline/requirements.txt
cd src
```

**1. Collect** from the crawler's outputs. `YOUTUBE_API_KEY` must be set (env or `.env`).
This takes about 4–5 minutes and uses about 350 YouTube API units.

```bash
python -m ml_pipeline collect \
  --brands-csv /path/to/Brands.csv \
  --channel-cache /path/to/channel_cache.json \
  --seed-queue /path/to/seed_queue.json
```

**2. Build the dataset**: clean → dedup → filter → export → report, in about 20 seconds.

```bash
python -m ml_pipeline run                       # keep English (default)
python -m ml_pipeline run --langs en,de --near-dup-threshold 0.7 --min-chars 300
```

**3. Fine-tune proof**: about 2 minutes on an Apple-silicon GPU (MPS), longer on CPU.

```bash
python -m ml_pipeline.train                     # 500 records, 1 epoch, distilgpt2
python -m ml_pipeline.train --max-records 200 --max-length 128   # faster
```

**4. Tests**

```bash
python -m pytest ml_pipeline/tests -q
```

Outputs go to `src/ml_pipeline/data/` (dataset, report, stats) and `src/ml_pipeline/models/`
(weights, `metrics.json`). Both are git-ignored.

## Results (reference run, Oct 2026)

Input was real crawler output: 79 brand sites and 278 creator channels.

| Stage | Records |
|---|---|
| Raw records collected | 3,151 (385 web pages + 2,766 video descriptions) |
| After cleaning | 3,120 |
| After dedup | 3,022 (29 exact + **69 near-duplicates via MinHash/LSH**, 3.1% dedup rate) |
| After quality + language filter | **2,096** (all `en`; 74 non-English dropped across 8 languages) |
| Final dataset | 2,096 records · ~936k tokens · 3.8 MB |

The largest drops were `too_short` (540), `link_heavy` (176) and `repetitive` (74),
mostly templated link-dump video descriptions. Exact-match dedup misses the near-duplicates
MinHash catches, for example product pages that differ only in the product name
(estimated Jaccard 0.81).

**Fine-tune** (`distilgpt2`, 450 train / 50 held-out records, 96k train tokens, 1 epoch, MPS):

| | Eval loss | Perplexity |
|---|---|---|
| Before | 3.952 | 52.0 |
| After | **3.643** | **38.2** |

That is a 7.8% lower held-out loss (27% lower perplexity) in 106 seconds. The eval
records were never trained on.

## Design notes

- **Memory:** stages are in-memory lists, which is fine for tens of thousands of documents.
  At larger scale, stream JSONL and persist the LSH index (`datasketch` supports Redis/Cassandra backends).
- **Rough tokens** in the stats use about 4 chars/token. `train.py` reports exact counts from the tokenizer.
- **Reproducible:** seeded language detection, shuffling and training.
- **Privacy:** contact details are replaced before export. Raw HTML and descriptions stay
  local in the git-ignored `data/` folder.
