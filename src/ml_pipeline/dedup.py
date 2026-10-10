"""
Dedup stage: exact (content hash) then near-duplicate (MinHash + LSH).

Exact:  SHA-256 of case-folded, whitespace-collapsed text. O(n).
Near:   each document → set of word 5-gram shingles → 128-permutation MinHash.
        MinHashLSH buckets candidates whose estimated Jaccard similarity is likely
        ≥ threshold, so we never compare all pairs (sub-quadratic). Each LSH
        candidate is then confirmed with the MinHash Jaccard estimate before a
        record is dropped, which cuts LSH false positives.

The first occurrence is kept; later near-copies are dropped. Example pairs are
kept in the stats so the report can show *what* was considered a duplicate.
"""

import hashlib
import re

from datasketch import MinHash, MinHashLSH

WORD_RE = re.compile(r"\w+", re.UNICODE)


def content_hash(text):
    norm = " ".join(text.casefold().split())
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def shingles(text, k=5):
    words = WORD_RE.findall(text.casefold())
    if len(words) <= k:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i:i + k]) for i in range(len(words) - k + 1)}


def minhash(text, num_perm=128, k=5):
    m = MinHash(num_perm=num_perm, seed=1)
    m.update_batch([s.encode("utf-8") for s in shingles(text, k)])
    return m


def exact_dedup(records):
    seen, kept = set(), []
    for r in records:
        h = content_hash(r["text"])
        if h in seen:
            continue
        seen.add(h)
        kept.append(r)
    return kept, len(records) - len(kept)


def near_dedup(records, threshold=0.8, num_perm=128, k=5, max_examples=5):
    lsh = MinHashLSH(threshold=threshold, num_perm=num_perm)
    sigs, kept, examples = {}, [], []
    removed = 0
    for i, r in enumerate(records):
        m = minhash(r["text"], num_perm, k)
        dup_of = None
        for key in lsh.query(m):
            sim = sigs[key][0].jaccard(m)
            if sim >= threshold:
                dup_of = (key, sim)
                break
        if dup_of:
            removed += 1
            if len(examples) < max_examples:
                orig = sigs[dup_of[0]][1]
                examples.append({"kept": orig.get("url", orig.get("id")),
                                 "dropped": r.get("url", r.get("id")),
                                 "est_jaccard": round(dup_of[1], 3)})
            continue
        key = str(i)
        lsh.insert(key, m)
        sigs[key] = (m, r)
        kept.append(r)
    return kept, removed, examples


def dedup_records(records, threshold=0.8, num_perm=128, k=5):
    """→ (kept_records, stats)."""
    after_exact, exact_removed = exact_dedup(records)
    kept, near_removed, examples = near_dedup(after_exact, threshold, num_perm, k)
    n = len(records)
    return kept, {
        "input": n, "kept": len(kept),
        "exact_removed": exact_removed, "near_removed": near_removed,
        "dedup_rate": round((exact_removed + near_removed) / n, 4) if n else 0.0,
        "threshold": threshold, "num_perm": num_perm, "shingle_k": k,
        "examples": examples,
    }
