"""
CLI (run from src/):

    python -m ml_pipeline collect --brands-csv PATH --channel-cache PATH [--seed-queue PATH]
    python -m ml_pipeline run     [--input ml_pipeline/data/raw.jsonl] [--langs en]
    python -m ml_pipeline report  [--stats ml_pipeline/data/stats.json]
    python -m ml_pipeline.train   (separate — see train.py)
"""

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m ml_pipeline",
                                 description="Crawler output → training-ready dataset.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("collect", help="adapter: read crawler outputs, fetch the text they point at")
    c.add_argument("--brands-csv", help="brand_sourcer CSV export (needs a 'Brand URL' column)")
    c.add_argument("--channel-cache", help="finder's channel_cache.json ('pass' channels are used)")
    c.add_argument("--seed-queue", help="rabbit-hole seed_queue.json")
    c.add_argument("--out", default=os.path.join(DATA, "raw.jsonl"))
    c.add_argument("--pages-per-site", type=int, default=6)
    c.add_argument("--max-sites", type=int, default=None)
    c.add_argument("--max-channels", type=int, default=300)
    c.add_argument("--videos-per-channel", type=int, default=10)

    r = sub.add_parser("run", help="clean → dedup → filter → export → report")
    r.add_argument("--input", default=os.path.join(DATA, "raw.jsonl"))
    r.add_argument("--out-dir", default=DATA)
    r.add_argument("--langs", default="en", help="comma-separated ISO codes to keep (default: en)")
    r.add_argument("--min-chars", type=int, default=200)
    r.add_argument("--min-words", type=int, default=40)
    r.add_argument("--max-symbol-ratio", type=float, default=0.10)
    r.add_argument("--near-dup-threshold", type=float, default=0.8,
                   help="MinHash/LSH Jaccard threshold (default 0.8)")
    r.add_argument("--num-perm", type=int, default=128)

    p = sub.add_parser("report", help="re-render the report from stats.json")
    p.add_argument("--stats", default=os.path.join(DATA, "stats.json"))

    a = ap.parse_args(argv)

    if a.cmd == "collect":
        from .collect import collect
        collect(a.out, a.brands_csv, a.channel_cache, a.seed_queue,
                a.pages_per_site, a.max_sites, a.max_channels, a.videos_per_channel)
    elif a.cmd == "run":
        if not os.path.exists(a.input):
            sys.exit(f"No input at {a.input} — run `python -m ml_pipeline collect …` first.")
        from .filter import FilterConfig
        from .pipeline import run
        cfg = FilterConfig(langs=tuple(x.strip() for x in a.langs.split(",") if x.strip()),
                           min_chars=a.min_chars, min_words=a.min_words,
                           max_symbol_ratio=a.max_symbol_ratio)
        run(a.input, a.out_dir, cfg, threshold=a.near_dup_threshold, num_perm=a.num_perm)
    elif a.cmd == "report":
        from .report import load_and_render
        print(load_and_render(a.stats))


if __name__ == "__main__":
    main()
