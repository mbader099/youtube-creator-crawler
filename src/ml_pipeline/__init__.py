"""
ml_pipeline — turn the crawler's output into a clean, training-ready dataset.

    collect  (adapter)  crawler outputs → raw.jsonl   (fetches the pages/descriptions they point at)
    run                 raw.jsonl → clean → dedup → filter → export → report
    train               training.jsonl → fine-tune a small causal LM, log loss before/after

The crawler's own code is never imported for writing and never modified; this
package only reads the files it leaves on disk.
"""

__all__ = ["clean", "dedup", "filter", "export", "report", "collect", "pipeline"]
