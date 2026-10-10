"""
Email FINDER cascade (Apollo → Prospeo → Snov.io).

Different job from email_verifier.py. The verifier does an SMTP handshake and is
useless on catch-all domains (they accept everything). The FINDER looks a person
up in a data provider's database and returns their real work email + a confidence,
which is what actually works for risky/catch-all CORPORATE contacts.

Reputation-safe: every provider does a database lookup. Nothing is ever sent from
your domain, so this can't touch your sender reputation or land in spam.

    Apollo   — 85 email-reveal credits/mo (break-glass; best B2B coverage). You own it.
    Prospeo  — 75/mo, ~98% accuracy.
    Snov.io  — 50/mo (OAuth). Strong international.

Stacked, that's ~210 reputation-safe corporate confirmations/month. Each provider
is skipped (loudly) if its key isn't set, so it works with whatever you've added.

    from email_finder import find_email
    r = find_email("Sarah", "O'Mahoney", "proton.me")
    #  → {"email": "...", "confidence": "verified", "provider": "apollo"} or None

CLI:
    python email_finder.py --status
    python email_finder.py "Sarah" "O'Mahoney" proton.me
"""

import envfile  # noqa: F401  — loads .env before any os.environ reads
import argparse
import json
import os
import sys
from datetime import datetime

from config import APIS, HAS_APOLLO, HAS_PROSPEO, HAS_SNOV, get_json, post_json


def _warn(msg):
    print(f"  ⚠ {msg}", file=sys.stderr)


# ── CREDIT LEDGER (so a batch never runs a provider dry) ─────────────────────
# Free monthly limits. Edit if you upgrade a plan.
FINDER_LIMITS = {"prospeo": 75, "snov": 50, "apollo": 85}
_CREDITS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "finder_credits.json")


def _cycle():
    return datetime.now().strftime("%Y-%m")     # calendar-month buckets


def _ledger():
    """Load usage, auto-resetting at the start of a new month."""
    data = {}
    if os.path.exists(_CREDITS_FILE):
        try:
            data = json.load(open(_CREDITS_FILE))
        except Exception:
            data = {}
    if data.get("cycle") != _cycle():
        data = {"cycle": _cycle(), "used": {}}
        _save_ledger(data)
    data.setdefault("used", {})
    return data


def _save_ledger(data):
    try:
        json.dump(data, open(_CREDITS_FILE, "w"), indent=2)
    except Exception as e:
        _warn(f"couldn't save credit ledger: {e}")


def credits_left(provider):
    return FINDER_LIMITS.get(provider, 0) - _ledger()["used"].get(provider, 0)


def _record_spend(provider, n=1):
    data = _ledger()
    data["used"][provider] = data["used"].get(provider, 0) + n
    _save_ledger(data)


def reset_credits():
    _save_ledger({"cycle": _cycle(), "used": {}})
    print("  finder credit ledger reset for this cycle")


def _ok_email(e, domain):
    e = (e or "").strip().lower()
    return e if ("@" in e and (not domain or e.endswith("@" + domain))) else ""


# ── PROVIDER ADAPTERS (each → {email, confidence, provider} or None) ──────────
def _apollo(first, last, domain):
    """Apollo people/match. Consumes 1 lead credit when it reveals an email."""
    data = post_json(
        APIS["apollo"]["base"],
        {"first_name": first, "last_name": last, "domain": domain,
         "reveal_personal_emails": True},
        headers={"X-Api-Key": APIS["apollo"]["key"], "Cache-Control": "no-cache"})
    p = data.get("person") or {}
    email = _ok_email(p.get("email"), domain)
    if email:
        return {"email": email, "confidence": p.get("email_status") or "apollo",
                "provider": "apollo"}
    return None


def _prospeo(first, last, domain):
    """Prospeo enrich-person (finds + verifies a work email from name + domain)."""
    data = post_json(APIS["prospeo"]["base"],
                     {"data": {"first_name": first, "last_name": last,
                               "company_website": domain}},
                     headers={"X-KEY": APIS["prospeo"]["key"]})
    if data.get("error"):
        raise RuntimeError(f"prospeo: {data.get('error_code') or data.get('message')}")
    em = ((data.get("person") or {}).get("email") or {})
    email = _ok_email(em.get("email"), domain)
    if email and "*" not in email:              # skip masked/unrevealed
        return {"email": email, "confidence": em.get("status") or "prospeo",
                "provider": "prospeo"}
    return None


def _snov(first, last, domain):
    """Snov.io: fetch an OAuth token, then look the person up."""
    tok = post_json("https://api.snov.io/v1/oauth/access_token",
                    {"grant_type": "client_credentials",
                     "client_id": APIS["snov"]["key"],
                     "client_secret": APIS["snov"]["secret"]})
    access = tok.get("access_token")
    if not access:
        raise RuntimeError("snov: no access_token (check client id/secret)")
    data = post_json("https://api.snov.io/v1/get-emails-from-names",
                     {"access_token": access, "firstName": first,
                      "lastName": last, "domain": domain})
    # emails live at data.data.emails[] = [{"email":..., "emailStatus":...}]
    for row in ((data.get("data") or {}).get("emails") or []):
        email = _ok_email(row.get("email"), domain)
        if email:
            return {"email": email, "confidence": row.get("emailStatus") or "snov",
                    "provider": "snov"}
    return None


# Cascade order: the free-tier APIs that actually work first (Prospeo → Snov),
# then Apollo last (its finder API is paid-only, so on a free plan it self-disables
# and is only reached if both others miss — ready if you ever upgrade).
_PROVIDERS = [("prospeo", _prospeo, lambda: HAS_PROSPEO),
              ("snov", _snov, lambda: HAS_SNOV),
              ("apollo", _apollo, lambda: HAS_APOLLO)]

COUNTERS = {}
_DISABLED = set()          # providers turned off for this run (e.g. plan gates the API)


def find_email(first, last, domain, verbose=False):
    """Find a corporate email via the finder cascade. Returns a dict or None.
    Never raises — a dead/absent provider is warned and skipped."""
    first, last = (first or "").strip(), (last or "").strip()
    if not first or not domain:
        return None
    any_live = False
    for name, fn, live in _PROVIDERS:
        if not live() or name in _DISABLED:
            continue
        if credits_left(name) <= 0:               # budget guard — don't run it dry
            _warn(f"{name} at its monthly limit ({FINDER_LIMITS[name]}) — skipping")
            continue
        any_live = True
        COUNTERS[f"{name}_calls"] = COUNTERS.get(f"{name}_calls", 0) + 1
        try:
            res = fn(first, last, domain)
        except Exception as e:
            msg = str(e)
            # a plan/access gate means this provider will fail every call → stop trying it
            if any(s in msg for s in ("Free plan", "not included", "not accessible",
                                      "403", "upgrade")):
                _DISABLED.add(name)
                _warn(f"{name} not available on your plan — disabling for this run")
                continue
            COUNTERS[f"{name}_error"] = COUNTERS.get(f"{name}_error", 0) + 1
            _warn(f"{name} finder error: {e}")
            continue
        if res:
            _record_spend(name)                   # a reveal consumes a credit
            if verbose:
                print(f"    · found via {name}: {res['email']} ({res['confidence']})"
                      f"  [{credits_left(name)} {name} credits left]")
            return res
    if not any_live:
        _warn("no email-finder keys set — risky contacts stay unconfirmed "
              "(add APOLLO_API_KEY / PROSPEO_API_KEY / SNOV_CLIENT_ID+SECRET)")
    return None


def _snov_balance():
    """Snov's real remaining balance from its API (authoritative cross-check)."""
    if not HAS_SNOV:
        return None
    try:
        tok = post_json("https://api.snov.io/v1/oauth/access_token",
                        {"grant_type": "client_credentials",
                         "client_id": APIS["snov"]["key"],
                         "client_secret": APIS["snov"]["secret"]})
        r = get_json("https://api.snov.io/v1/get-balance",
                     {"access_token": tok.get("access_token")})
        return r.get("balance") or (r.get("data") or {}).get("balance")
    except Exception as e:
        _warn(f"snov balance check failed: {e}")
        return None


def credits_report():
    d = _ledger()
    print(f"  Finder credits — cycle {d['cycle']} (resets at month start):")
    for p, lim in FINDER_LIMITS.items():
        used = d["used"].get(p, 0)
        note = "  (API paid-only, not auto-used)" if p == "apollo" else ""
        print(f"    {p:<9} {used}/{lim} used · {lim - used} left{note}")
    bal = _snov_balance()
    if bal is not None:
        print(f"    ↳ Snov real balance (from API): {bal}")
    print("  (Local count assumes 1 credit per email found. If it drifts from your")
    print("   provider dashboard, run `--reset-credits` to realign.)")


def status():
    print("  Email finder cascade (database lookups; reputation-safe):")
    for name, _, live in _PROVIDERS:
        have = "✅ live" if live() else "— (no key)"
        print(f"    {name:<9} {have:<12} rate: {APIS[name]['rate']}")
    if not any(live() for _, _, live in _PROVIDERS):
        print("\n  No finder keys yet → risky/catch-all contacts stay unconfirmed.")
    print()
    credits_report()


def main(argv):
    ap = argparse.ArgumentParser(description="Find a corporate email (database lookup).")
    ap.add_argument("first", nargs="?")
    ap.add_argument("last", nargs="?")
    ap.add_argument("domain", nargs="?")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--credits", action="store_true", help="show credit usage")
    ap.add_argument("--reset-credits", action="store_true",
                    help="realign the local credit count to zero for this cycle")
    args = ap.parse_args(argv)
    if args.reset_credits:
        reset_credits()
        return
    if args.credits:
        credits_report()
        return
    if args.status or not (args.first and args.domain):
        status()
        return
    r = find_email(args.first, args.last or "", args.domain, verbose=True)
    print("\n  result:", r or "not found")


if __name__ == "__main__":
    main(sys.argv[1:])
