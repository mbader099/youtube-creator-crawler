from ml_pipeline.dedup import dedup_records, exact_dedup, near_dedup

BASE = (
    "In this video we benchmark four local language models on a laptop GPU and "
    "compare tokens per second memory use and output quality across coding "
    "summarisation and reasoning tasks then we walk through quantisation settings "
    "that let a seven billion parameter model run comfortably in sixteen gigabytes "
    "of memory without a noticeable drop in answer quality for everyday use"
)
NEAR = BASE.replace("four local", "five local")          # one word changed
OTHER = (
    "Today we build a home lab firewall with an old mini PC two network cards and "
    "open source routing software covering VLANs intrusion detection and automatic "
    "backups so the whole network keeps running even when a single disk fails"
)


def _recs(*texts):
    return [{"id": str(i), "text": t} for i, t in enumerate(texts)]


def test_exact_dedup_ignores_case_and_whitespace():
    kept, removed = exact_dedup(_recs(BASE, "  " + BASE.upper() + "\n", OTHER))
    assert removed == 1 and len(kept) == 2


def test_minhash_lsh_catches_near_duplicate_that_exact_misses():
    recs = _recs(BASE, NEAR, OTHER)
    assert exact_dedup(recs)[1] == 0                      # not exact copies…
    kept, removed, examples = near_dedup(recs, threshold=0.8)
    assert removed == 1                                   # …but near-duplicates
    assert [r["id"] for r in kept] == ["0", "2"]          # first occurrence kept
    assert examples[0]["est_jaccard"] >= 0.8


def test_distinct_documents_survive_and_stats_add_up():
    kept, stats = dedup_records(_recs(BASE, BASE, NEAR, OTHER))
    assert stats["exact_removed"] == 1 and stats["near_removed"] == 1
    assert stats["kept"] == len(kept) == 2
    assert stats["dedup_rate"] == 0.5
