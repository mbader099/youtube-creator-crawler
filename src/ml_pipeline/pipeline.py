"""Glue: raw.jsonl → clean → dedup → filter → export → report."""

import json
import os
import time
from collections import Counter
from datetime import datetime, timezone

from .clean import clean_records
from .dedup import dedup_records
from .export import export_jsonl, print_stats
from .filter import FilterConfig, filter_records
from .report import write_report


def load_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _by_source(records):
    return dict(Counter(r.get("source", "?") for r in records))


def run(input_path, out_dir, cfg=None, threshold=0.8, num_perm=128):
    t0 = time.time()
    raw = load_jsonl(input_path)
    print(f"  [0/4] loaded {len(raw):,} raw records from {input_path}")

    cleaned, s_clean = clean_records(raw)
    print(f"  [1/4] clean   → {len(cleaned):,} kept  dropped {s_clean['dropped']}")

    deduped, s_dedup = dedup_records(cleaned, threshold=threshold, num_perm=num_perm)
    print(f"  [2/4] dedup   → {len(deduped):,} kept  "
          f"exact −{s_dedup['exact_removed']:,}  near (MinHash/LSH) −{s_dedup['near_removed']:,}")

    final, s_filter = filter_records(deduped, cfg or FilterConfig())
    print(f"  [3/4] filter  → {len(final):,} kept  dropped {s_filter['dropped']}")

    s_export = export_jsonl(final, os.path.join(out_dir, "training.jsonl"))
    print(f"  [4/4] export  → {s_export['path']}")

    stats = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "input_path": input_path, "runtime_s": round(time.time() - t0, 1),
        "clean": s_clean, "dedup": s_dedup, "filter": s_filter, "export": s_export,
        "coverage": {"input": _by_source(raw), "clean": _by_source(cleaned),
                     "dedup": _by_source(deduped), "final": _by_source(final)},
    }
    print_stats(s_export, s_dedup["exact_removed"] + s_dedup["near_removed"])
    path, _ = write_report(stats, out_dir)
    print(f"\n  report → {path}   stats → {os.path.join(out_dir, 'stats.json')}")
    return stats
