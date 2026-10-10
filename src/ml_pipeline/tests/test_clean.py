from ml_pipeline.clean import clean_record, clean_records, normalize, scrub_pii

ARTICLE = (
    "Password managers store every credential in an encrypted vault that only "
    "you can unlock. Instead of reusing one weak password across dozens of sites, "
    "you remember a single strong master password and let the manager generate "
    "long random passwords for everything else. Most managers also warn you when a "
    "saved site appears in a known data breach, so you can rotate that password "
    "before anyone abuses it."
)

PAGE = f"""<!doctype html><html><head><title>Guide</title>
<style>.x{{color:red}}</style><script>trackVisitor('NAVSCRIPT')</script></head>
<body>
  <nav><a href="/">Home</a> <a href="/pricing">Pricing</a> <a href="/login">Log in</a></nav>
  <div class="cookie-banner">We use cookies. Accept all</div>
  <main><article><h1>Why use a password manager?</h1>
    <p>{ARTICLE}</p><p>{ARTICLE}</p>
  </article></main>
  <footer>© 2026 Example Inc. All rights reserved. Subscribe to our newsletter.</footer>
</body></html>"""


def test_html_extraction_keeps_article_drops_chrome():
    text, reason = clean_record({"kind": "html", "raw": PAGE, "url": "https://example.com/guide"})
    assert reason is None
    assert "encrypted vault" in text
    for junk in ("NAVSCRIPT", "color:red", "Accept all", "All rights reserved", "Log in"):
        assert junk not in text


def test_normalize_unicode_and_whitespace():
    raw = "ﬁne​  tuning\t\tworks\r\n\r\n\r\n\r\nnext   para\x07"
    assert normalize(raw) == "fine tuning works\n\nnext para"


def test_scrub_pii_replaces_emails_and_urls():
    out = scrub_pii("biz: jane.doe@example.com · site https://example.com/x?y=1 · www.foo.io")
    assert "jane.doe" not in out and "example.com/x" not in out and "foo.io" not in out
    assert out.count("[EMAIL]") == 1 and out.count("[URL]") == 2


def test_scrub_pii_catches_typo_urls_and_social_handles():
    out = scrub_pii("Prime: hhttps://amzn.to/abc\nIG: @first.last · mail me@site.io · 50 @ 5pm")
    assert "amzn" not in out and "first.last" not in out and "me@site" not in out
    assert out.count("[URL]") == 1 and out.count("[HANDLE]") == 1 and out.count("[EMAIL]") == 1
    assert "50 @ 5pm" in out                                    # a bare '@' is not a handle


def test_text_records_strip_tags_and_unescape():
    text, reason = clean_record({"kind": "text", "raw": "Great <b>video</b> &amp; demo"})
    assert reason is None and text == "Great video & demo"


def test_empty_and_unextractable_records_are_dropped_with_reason():
    recs = [{"kind": "text", "raw": "   "},
            {"kind": "html", "raw": "<html><body><script>track()</script><img src=x.png></body></html>"},
            {"kind": "text", "raw": "kept"}]
    kept, stats = clean_records(recs)
    assert [r["text"] for r in kept] == ["kept"]
    assert "raw" not in kept[0]
    assert stats["dropped"] == {"empty_raw": 1, "extract_failed": 1}
