"""
Quality + language filter.

Cheap heuristics run first; language detection (slower) only on survivors.
Every drop is attributed to exactly one reason so the report can break it down.

Heuristics (all configurable via FilterConfig):
  too_short         fewer than min_chars characters or min_words words
  symbol_heavy      share of non-text symbols (emoji, |, #, *, …) above max_symbol_ratio
  link_heavy        [URL]/[EMAIL]/[HANDLE] placeholders make up too much of the words
  repetitive        too many repeated lines (templated footers, link lists)
  boilerplate       too many lines that are site/social chrome (cookies, subscribe, …)
  lang:<code>       detected language not in cfg.langs (langdetect, seeded → deterministic)
"""

import re
from collections import Counter
from dataclasses import dataclass, field

from langdetect import DetectorFactory, LangDetectException, detect

DetectorFactory.seed = 0          # langdetect is randomised; seed for reproducible runs

_TEXT_PUNCT = set(".,;:!?'\"()[]-–—/&%$@’‘“”…")
PLACEHOLDER_RE = re.compile(r"\[(?:URL|EMAIL|HANDLE)\]")
BOILERPLATE_RE = re.compile(
    r"cookie|privacy policy|terms of (?:service|use)|all rights reserved|©|"
    r"subscribe|follow (?:me|us)|sign (?:up|in)|log ?in|newsletter|"
    r"accept all|skip to (?:main )?content|share this|related posts|"
    r"instagram|twitter|tiktok|facebook|discord|patreon", re.I)


@dataclass
class FilterConfig:
    langs: tuple = ("en",)
    min_chars: int = 200
    min_words: int = 40
    max_symbol_ratio: float = 0.10
    max_placeholder_ratio: float = 0.10
    max_dup_line_ratio: float = 0.30
    max_boilerplate_line_ratio: float = 0.40
    lang_sample_chars: int = 2000
    extra: dict = field(default_factory=dict)


def symbol_ratio(text):
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return 1.0
    sym = sum(1 for c in chars if not c.isalnum() and c not in _TEXT_PUNCT)
    return sym / len(chars)


def quality_reason(text, cfg):
    words = text.split()
    if len(text) < cfg.min_chars or len(words) < cfg.min_words:
        return "too_short"
    if symbol_ratio(text) > cfg.max_symbol_ratio:
        return "symbol_heavy"
    if len(PLACEHOLDER_RE.findall(text)) / len(words) > cfg.max_placeholder_ratio:
        return "link_heavy"
    lines = [ln.strip().casefold() for ln in text.split("\n") if ln.strip()]
    if len(lines) >= 4:
        if 1 - len(set(lines)) / len(lines) > cfg.max_dup_line_ratio:
            return "repetitive"
        if sum(1 for ln in lines if BOILERPLATE_RE.search(ln)) / len(lines) > cfg.max_boilerplate_line_ratio:
            return "boilerplate"
    return None


def detect_lang(text, sample=2000):
    try:
        return detect(text[:sample])
    except LangDetectException:
        return "unknown"


def filter_records(records, cfg=None):
    """→ (kept_records, stats). Kept records gain a 'lang' field."""
    cfg = cfg or FilterConfig()
    keep_langs = set(cfg.langs)
    kept, dropped, lang_seen = [], Counter(), Counter()
    for r in records:
        reason = quality_reason(r["text"], cfg)
        if reason:
            dropped[reason] += 1
            continue
        lang = detect_lang(r["text"], cfg.lang_sample_chars)
        lang_seen[lang] += 1
        if keep_langs and lang not in keep_langs:
            dropped[f"lang:{lang}"] += 1
            continue
        kept.append(dict(r, lang=lang))
    return kept, {
        "input": len(records), "kept": len(kept), "dropped": dict(dropped),
        "languages_detected": dict(lang_seen.most_common()),
        "languages_kept": dict(Counter(r["lang"] for r in kept).most_common()),
        "config": {k: (list(v) if isinstance(v, tuple) else v)
                   for k, v in cfg.__dict__.items() if k != "extra"},
    }
