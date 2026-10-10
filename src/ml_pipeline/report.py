"""
Coverage / data-quality report: renders the pipeline's stats.json as markdown.

    python -m ml_pipeline report --stats data/stats.json      # re-render + print
"""

import json
import os


def _pct(a, b):
    return f"{100 * a / b:.1f}%" if b else "—"


def _table(rows, head):
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def build_report(stats):
    c, d, f, e = stats["clean"], stats["dedup"], stats["filter"], stats["export"]
    n_in = c["input"]
    lines = [
        "# Training data report", "",
        f"Generated {stats.get('generated_at', '')} from `{stats.get('input_path', '')}`.", "",
        "## Funnel", "",
        _table([
            ["Input (raw records)", f"{n_in:,}", "100%"],
            ["After cleaning", f"{c['kept']:,}", _pct(c["kept"], n_in)],
            ["After dedup", f"{d['kept']:,}", _pct(d["kept"], n_in)],
            ["After quality + language filter", f"{f['kept']:,}", _pct(f["kept"], n_in)],
            ["**Final dataset**", f"**{e['records']:,}**", f"**{_pct(e['records'], n_in)}**"],
        ], ["Stage", "Records", "of input"]), "",
        "## Dropped, by reason", "",
    ]
    reasons = [("clean", k, v) for k, v in c["dropped"].items()]
    reasons += [("dedup", "exact_duplicate", d["exact_removed"]),
                ("dedup", "near_duplicate (MinHash/LSH)", d["near_removed"])]
    reasons += [("filter", k, v) for k, v in f["dropped"].items()]
    reasons = [r for r in reasons if r[2]]
    reasons.sort(key=lambda r: -r[2])
    lines += [_table([[s, k, f"{v:,}", _pct(v, n_in)] for s, k, v in reasons],
                     ["Stage", "Reason", "Records", "of input"]), ""]

    lines += [
        "## Deduplication", "",
        f"- Dedup rate: **{100 * d['dedup_rate']:.1f}%** of cleaned records "
        f"({d['exact_removed']:,} exact + {d['near_removed']:,} near-duplicate)",
        f"- MinHash: {d['num_perm']} permutations, word {d['shingle_k']}-gram shingles, "
        f"LSH Jaccard threshold {d['threshold']}",
    ]
    if d.get("examples"):
        lines += ["", "Example near-duplicate pairs (kept ← dropped):", ""]
        lines += [f"- `{x['kept']}` ← `{x['dropped']}` (est. Jaccard {x['est_jaccard']})"
                  for x in d["examples"]]
    lines += ["", "## Language", "",
              _table([[k, f"{v:,}", f"{f['languages_kept'].get(k, 0):,}"]
                      for k, v in f["languages_detected"].items()],
                     ["Detected", "Records", "Kept"]),
              "", f"Kept languages: `{', '.join(f['config']['langs'])}`", ""]

    cov = stats.get("coverage", {})
    if cov:
        lines += ["## Coverage by source", ""]
        stages = ["input", "clean", "dedup", "final"]
        srcs = sorted({s for st in stages for s in cov.get(st, {})})
        lines += [_table([[s] + [f"{cov.get(st, {}).get(s, 0):,}" for st in stages]
                          + [_pct(cov.get("final", {}).get(s, 0), cov.get("input", {}).get(s, 0))]
                          for s in srcs],
                         ["Source", "Input", "Cleaned", "Deduped", "Final", "Yield"]), ""]

    lines += [
        "## Final dataset", "",
        f"- File: `{e['path']}` ({e['bytes'] / 1024:,.1f} KB)",
        f"- Records: **{e['records']:,}** · rough tokens: **{e['rough_tokens']:,}** "
        f"· words: {e['words']:,} · avg {e['avg_chars']:,} chars/record",
        f"- Languages: {e['languages']}",
        "", "## Filter config", "", "```json",
        json.dumps(f["config"], indent=2), "```", "",
    ]
    return "\n".join(lines)


def write_report(stats, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "stats.json"), "w") as fh:
        json.dump(stats, fh, indent=2)
    md = build_report(stats)
    path = os.path.join(out_dir, "report.md")
    with open(path, "w") as fh:
        fh.write(md)
    return path, md


def load_and_render(stats_path):
    with open(stats_path) as fh:
        return build_report(json.load(fh))
