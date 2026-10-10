"""
Cleaning stage: raw records → plain, normalised text.

  html records  → trafilatura main-content extraction (drops nav, footers,
                  cookie banners, sidebars, ads, scripts/styles)
  text records  → stray tags stripped, entities unescaped
  both          → Unicode NFKC, control/zero-width chars removed, whitespace
                  collapsed (paragraph breaks kept), emails/URLs/@handles replaced
                  with [EMAIL]/[URL]/[HANDLE] so no contact details reach training data

Records that end up empty are dropped with a reason; everything else passes on
to dedup/filter, which own the quality decisions.
"""

import html as _html
import re
import unicodedata
from collections import Counter

import trafilatura

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
URL_RE = re.compile(r"\S*(?:https?://|www\.)\S+", re.I)    # \S* also eats typos like "hhttps://"
# Social handles (@name, @first.last) — run after EMAIL_RE so emails are already gone.
HANDLE_RE = re.compile(r"(?<![\w@])@[A-Za-z0-9_](?:[A-Za-z0-9_.]{0,28}[A-Za-z0-9_])?")
TAG_RE = re.compile(r"<[^>]{1,500}>")
# Cc/Cf categories cover control chars + zero-width/bidi marks; keep \n and \t.
_KEEP = {"\n", "\t"}


def extract_html(html, url=None):
    """Main-content text via trafilatura, or None if nothing extractable."""
    return trafilatura.extract(
        html, url=url, include_comments=False, include_tables=False,
        include_links=False, include_images=False, favor_precision=True,
        deduplicate=True,
    )


def strip_tags(text):
    return _html.unescape(TAG_RE.sub(" ", text))


def normalize(text):
    """NFKC, drop control/format chars, collapse whitespace, keep paragraphs."""
    text = unicodedata.normalize("NFKC", text).replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(c for c in text
                   if c in _KEEP or unicodedata.category(c) not in ("Cc", "Cf"))
    lines = [re.sub(r"[ \t ]+", " ", ln).strip() for ln in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def scrub_pii(text):
    """Emails → URLs → @handles. Order matters: a URL may contain '@', and an
    email's '@domain' must not be mistaken for a handle."""
    text = URL_RE.sub("[URL]", EMAIL_RE.sub("[EMAIL]", text))
    return HANDLE_RE.sub("[HANDLE]", text)


def clean_record(rec):
    """Return (text, None) on success or (None, reason)."""
    raw = rec.get("raw") or ""
    if not raw.strip():
        return None, "empty_raw"
    if rec.get("kind") == "html":
        text = extract_html(raw, rec.get("url"))
        if not text:
            return None, "extract_failed"
    else:
        text = strip_tags(raw)
    text = scrub_pii(normalize(text))
    if not text:
        return None, "empty_after_clean"
    return text, None


def clean_records(records):
    """→ (kept_records, stats). Each kept record gains 'text' and loses 'raw'."""
    kept, dropped = [], Counter()
    for rec in records:
        text, reason = clean_record(rec)
        if reason:
            dropped[reason] += 1
            continue
        out = {k: v for k, v in rec.items() if k != "raw"}
        out["text"] = text
        kept.append(out)
    return kept, {"input": len(records), "kept": len(kept), "dropped": dict(dropped)}
