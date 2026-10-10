"""
Export stage: write training-ready JSONL — one {"text": "..."} object per line,
UTF-8, no other fields (the format HuggingFace `datasets` and most fine-tuning
tools load directly). Provenance (url/source/lang) goes to a separate
`*.meta.jsonl` sidecar with the same line order, so the training file stays clean.
"""

import json
import os
from collections import Counter

CHARS_PER_TOKEN = 4      # rough BPE average for English; exact counts come from train.py


def rough_tokens(text):
    return max(1, round(len(text) / CHARS_PER_TOKEN))


def export_jsonl(records, path, meta=True):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps({"text": r["text"]}, ensure_ascii=False) + "\n")
    meta_path = None
    if meta:
        meta_path = path[:-len(".jsonl")] + ".meta.jsonl" if path.endswith(".jsonl") else path + ".meta"
        with open(meta_path, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps({k: r.get(k) for k in ("id", "source", "url", "lang")},
                                   ensure_ascii=False) + "\n")
    chars = sum(len(r["text"]) for r in records)
    return {
        "path": path, "meta_path": meta_path,
        "records": len(records),
        "chars": chars,
        "words": sum(len(r["text"].split()) for r in records),
        "rough_tokens": sum(rough_tokens(r["text"]) for r in records),
        "bytes": os.path.getsize(path),
        "languages": dict(Counter(r.get("lang", "?") for r in records).most_common()),
        "sources": dict(Counter(r.get("source", "?") for r in records).most_common()),
        "avg_chars": round(chars / len(records)) if records else 0,
    }


def print_stats(export_stats, dupes_dropped):
    s = export_stats
    print("\n  ── Dataset stats ─────────────────────────")
    print(f"  file           {s['path']}")
    print(f"  records        {s['records']:,}")
    print(f"  rough tokens   {s['rough_tokens']:,}  (~{CHARS_PER_TOKEN} chars/token)")
    print(f"  words          {s['words']:,}   avg {s['avg_chars']:,} chars/record")
    print(f"  size           {s['bytes'] / 1024:,.1f} KB")
    print(f"  languages      {s['languages']}")
    print(f"  sources        {s['sources']}")
    print(f"  dupes dropped  {dupes_dropped:,}")
