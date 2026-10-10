from ml_pipeline.filter import FilterConfig, filter_records, quality_reason

EN = (
    "Self-hosting your own password manager gives you full control over where your "
    "vault lives. In this guide we install the server with Docker, put it behind a "
    "reverse proxy with automatic certificates, enable two-factor authentication, and "
    "schedule encrypted nightly backups to a second machine so a single failure never "
    "costs you your data. Finally we test a restore to make sure the backups work."
)
ES = (
    "Alojar tu propio gestor de contraseñas te da control total sobre dónde vive tu "
    "bóveda. En esta guía instalamos el servidor con Docker, lo colocamos detrás de un "
    "proxy inverso con certificados automáticos, activamos la autenticación de dos "
    "factores y programamos copias de seguridad cifradas cada noche en otra máquina."
)
SYMBOLS = EN + " " + "🔥🚀💯 ||| ### *** >>> " * 20
LINKS = "Links: " + " ".join(["[URL]"] * 30) + " " + EN
REPETITIVE = "\n".join(["Follow me on Twitter", "Join the Discord"] * 6 + [EN])


def _recs(*texts):
    return [{"id": str(i), "text": t} for i, t in enumerate(texts)]


def test_quality_heuristics_assign_one_reason_each():
    cfg = FilterConfig()
    assert quality_reason("too short to train on", cfg) == "too_short"
    assert quality_reason(SYMBOLS, cfg) == "symbol_heavy"
    assert quality_reason(LINKS, cfg) == "link_heavy"
    assert quality_reason(REPETITIVE, cfg) == "repetitive"
    assert quality_reason(EN, cfg) is None


def test_language_filter_keeps_english_drops_spanish_by_default():
    kept, stats = filter_records(_recs(EN, ES))
    assert [r["id"] for r in kept] == ["0"] and kept[0]["lang"] == "en"
    assert stats["dropped"] == {"lang:es": 1}
    assert stats["languages_detected"] == {"en": 1, "es": 1}


def test_language_set_is_configurable():
    kept, _ = filter_records(_recs(EN, ES), FilterConfig(langs=("en", "es")))
    assert {r["lang"] for r in kept} == {"en", "es"}


def test_drop_reasons_are_reported():
    _, stats = filter_records(_recs(EN, "tiny", SYMBOLS, ES))
    assert stats["kept"] == 1
    assert stats["dropped"] == {"too_short": 1, "symbol_heavy": 1, "lang:es": 1}
