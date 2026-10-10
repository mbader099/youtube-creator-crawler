"""
Email verifier (Stage 1, the "verify" half).

A credit-saving funnel that answers one question per address: **can I safely
draft to this without risking your warmed sending inboxes?**

    LOCAL PRE-FILTER (free, no port 25)      →  kills obvious junk
       syntax → MX (via `dig`) → disposable → role-account
    PROVIDER CASCADE (metered free tiers)    →  Reoon → Hunter → ZeroBounce
       falls through ONLY on error / out-of-credits / inconclusive "unknown";
       a definitive catch-all is terminal (a 2nd provider would just agree).

Every provider's own vocabulary is normalised to four words —
    valid · invalid · risky · unknown
then routed the way the stack spec dictates:
    valid    → draft
    risky    → draft + ⚠ EMAIL UNVERIFIED     (catch-all, role inbox, webmail…)
    unknown  → draft + ⚠ EMAIL UNVERIFIED     (all providers inconclusive — never hard-fail Google catch-alls)
    invalid  → skip (never emailed: bad mailbox, disposable, spamtrap, do-not-mail)

Conventions inherited from config.py: real browser UA on every call, 429 backoff
honouring retry-after, and **never fail silently** — provider errors are counted
and warned so a broken/exhausted key is visible, not swallowed.

CLI:
    python email_verifier.py alice@brand.com partnerships@brand.com   # verify
    python email_verifier.py --status                                 # which keys are live
    python email_verifier.py                                          # no-key self-test (local layer)
"""

import envfile  # noqa: F401  — loads .env before any os.environ reads
import re
import subprocess
import sys

from config import APIS, get_json

# ── NORMALISED VOCABULARY ─────────────────────────────────────────────────────
# Four terminal states + how each routes downstream.
ROUTE = {
    "valid":   "draft",
    "risky":   "draft_unverified",
    "unknown": "draft_unverified",   # inconclusive → flag, don't hard-fail
    "invalid": "skip",
}
LABEL = {
    "valid":   "✅ valid",
    "invalid": "⛔ invalid",
    "risky":   "⚠ risky",
    "unknown": "❓ unknown",
}

# Provider status tokens → our vocabulary. Matched case-insensitively. Anything
# unmatched becomes "unknown" (→ cascade falls through) rather than a wrong guess.
VALID_TOKENS = {"valid", "safe", "deliverable", "ok"}
INVALID_TOKENS = {                       # must NOT be emailed
    "invalid", "undeliverable", "disabled", "disable", "disposable",
    "spamtrap", "abuse", "do_not_mail", "bounce", "rejected", "failed",
}
RISKY_TOKENS = {                         # deliverable-ish but unconfirmed → flag
    "catch_all", "catch-all", "accept_all", "accept-all", "risky",
    "role", "role_account", "webmail", "greylisted", "full_mailbox",
}
UNKNOWN_TOKENS = {"unknown", "unverifiable", "timeout", "temp_error", "greylist"}

# Local-part prefixes that are shared inboxes, not a person. For brand outreach a
# partnerships@ / marketing@ inbox is still worth a (flagged) draft, so these are
# RISKY, not INVALID.
ROLE_LOCALPARTS = {
    "info", "sales", "support", "admin", "contact", "hello", "team",
    "marketing", "press", "partnerships", "partner", "hr", "jobs", "careers",
    "help", "billing", "office", "mail", "enquiries", "inquiries", "general",
    "noreply", "no-reply", "donotreply", "media", "pr", "hi",
}

# A short, high-signal disposable set — not exhaustive; providers catch the rest.
DISPOSABLE_DOMAINS = {
    "mailinator.com", "guerrillamail.com", "10minutemail.com", "tempmail.com",
    "temp-mail.org", "throwawaymail.com", "yopmail.com", "trashmail.com",
    "getnada.com", "sharklasers.com", "maildrop.cc", "dispostable.com",
}

SYNTAX_RE = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]+$")

# ── FAILURE VISIBILITY (never silent) ─────────────────────────────────────────
COUNTERS = {}          # e.g. {"reoon_error": 2, "verified_valid": 5, ...}


def _bump(key, n=1):
    COUNTERS[key] = COUNTERS.get(key, 0) + n


def _warn(msg):
    print(f"  ⚠ {msg}", file=sys.stderr)


# ── LOCAL PRE-FILTER (free, no port 25) ───────────────────────────────────────
def _mx_hosts(domain):
    """MX hosts for a domain via `dig` (no dnspython dependency).
    Returns [] when the domain has no MX (can't receive mail), or None when the
    lookup itself failed (network/tooling) — callers treat None as 'don't know'."""
    try:
        out = subprocess.run(
            ["dig", "+short", "MX", domain],
            capture_output=True, text=True, timeout=8)
    except Exception as e:
        _warn(f"MX lookup failed for {domain}: {e}")
        return None
    if out.returncode != 0:
        return None
    hosts = []
    for line in out.stdout.splitlines():
        parts = line.split()
        if parts:
            hosts.append(parts[-1].rstrip(".").lower())
    return hosts


def _is_google(mx_hosts):
    return any(("google.com" in h or "googlemail.com" in h) for h in (mx_hosts or []))


# ── PROVIDER ADAPTERS (each → (status, reason, raw) in our vocabulary) ─────────
def _map_token(token):
    """Best-effort map of an arbitrary provider status string to our vocabulary."""
    t = (token or "").lower().strip()
    if t in VALID_TOKENS:
        return "valid"
    if t in INVALID_TOKENS:
        return "invalid"
    if t in RISKY_TOKENS:
        return "risky"
    if t in UNKNOWN_TOKENS or not t:
        return "unknown"
    return "unknown"


def _reoon(email):
    """Reoon Email Verifier (power mode = full SMTP + catch-all)."""
    data = get_json(APIS["reoon"]["base"],
                    {"email": email, "key": APIS["reoon"]["key"], "mode": "power"})
    st = str(data.get("status", "")).lower()
    # Loud on auth/credit problems so a dead key is visible, not swallowed.
    if data.get("error") or st in ("invalid_key", "insufficient_credits",
                                   "key_not_found", "no_credit"):
        raise RuntimeError(f"reoon: {data.get('error') or st}")
    if data.get("is_disposable"):
        return "invalid", "disposable", data
    mapped = _map_token(st)
    if mapped == "valid":
        if data.get("is_catch_all"):
            return "risky", "catch_all", data
        if data.get("is_role_account"):
            return "risky", "role_account", data
        if data.get("is_safe_to_send") is False:
            return "risky", "not_safe_to_send", data
        return "valid", "smtp_ok", data
    return mapped, (st or "no_status"), data


def _hunter(email):
    """Hunter.io email-verifier (also their finder pool — 50/mo shared)."""
    data = get_json(APIS["hunter"]["base"],
                    {"email": email, "api_key": APIS["hunter"]["key"]})
    if data.get("errors"):
        raise RuntimeError(f"hunter: {data['errors']}")
    d = data.get("data", {}) or {}
    result = str(d.get("result", "")).lower()   # deliverable / undeliverable / risky
    st = str(d.get("status", "")).lower()
    if result == "deliverable":
        return "valid", "smtp_ok", data
    if result == "undeliverable":
        return "invalid", (st or "undeliverable"), data
    if result == "risky":
        return "risky", (st or "risky"), data
    return _map_token(st), (st or "no_status"), data


def _zerobounce(email):
    """ZeroBounce validate (100/mo free)."""
    data = get_json(APIS["zerobounce"]["base"],
                    {"api_key": APIS["zerobounce"]["key"],
                     "email": email, "ip_address": ""})
    if data.get("error"):
        raise RuntimeError(f"zerobounce: {data['error']}")
    st = str(data.get("status", "")).lower()
    sub = str(data.get("sub_status", "")).lower()
    if st == "valid":
        return "valid", "smtp_ok", data
    if st == "invalid":
        return "invalid", (sub or "invalid"), data
    if st in ("catch-all", "catch_all"):
        return "risky", "catch_all", data
    if st in ("spamtrap", "abuse", "do_not_mail"):
        return "invalid", st, data          # protect the warmed inbox
    return _map_token(st), (sub or st or "no_status"), data


# Cascade order. Each entry: (name, fn). Configured-ness is checked at call time
# so a key added via env between runs is picked up without editing this file.
_PROVIDER_FNS = [("reoon", _reoon), ("hunter", _hunter), ("zerobounce", _zerobounce)]


# ── RESULT ────────────────────────────────────────────────────────────────────
class VerifyResult:
    def __init__(self, email):
        self.email = email
        self.status = "unknown"       # valid | invalid | risky | unknown
        self.reason = ""
        self.provider = ""            # who produced the verdict ('local' or a provider)
        self.google_hosted = False
        self.attempts = []            # [(provider, status, detail)]
        self.raw = None               # winning provider's raw payload

    def _add(self, provider, status, detail):
        self.attempts.append((provider, status, detail))

    def _finalize(self, status, reason, provider, raw=None):
        self.status, self.reason, self.provider, self.raw = status, reason, provider, raw
        _bump(f"verified_{status}")
        return self

    @property
    def route(self):
        return ROUTE[self.status]

    @property
    def needs_unverified_flag(self):
        return self.route == "draft_unverified"

    @property
    def should_draft(self):
        return self.route in ("draft", "draft_unverified")

    def __repr__(self):
        flag = "  ⚠ EMAIL UNVERIFIED" if self.needs_unverified_flag else ""
        g = "  (google-hosted)" if self.google_hosted else ""
        return (f"{LABEL[self.status]:<12} {self.email:<38} "
                f"via {self.provider:<10} [{self.reason}]{g}{flag}")


# ── CASCADE ───────────────────────────────────────────────────────────────────
def verify_email(email, verbose=False):
    """Run one address through the local pre-filter then the provider cascade."""
    email = (email or "").strip().lower()
    r = VerifyResult(email)

    # 1) syntax
    if not SYNTAX_RE.match(email):
        return r._finalize("invalid", "bad_syntax", "local")
    local = email.split("@", 1)[0]
    domain = email.split("@", 1)[1]

    # 2) disposable domain
    if domain in DISPOSABLE_DOMAINS:
        return r._finalize("invalid", "disposable", "local")

    # 3) MX — no MX usually means the domain can't receive mail. But a domain
    # with only an A record can still accept mail (RFC 5321 fallback), and the
    # providers check that. So no-MX is terminal ONLY in local-only mode; with a
    # verifier live, pass it through rather than silently dropping a real brand.
    mx = _mx_hosts(domain)
    live = [n for n, _ in _PROVIDER_FNS if APIS[n]["key"]]
    if mx == []:
        if not live:
            return r._finalize("invalid", "no_mx", "local")
        r._add("local", "risky", "no_mx")     # weak signal; provider decides
    r.google_hosted = _is_google(mx)          # mx is None (lookup failed) → skip the hint

    # role inboxes are worth a flagged draft, not a hard skip — noted, verified anyway
    is_role = local in ROLE_LOCALPARTS

    # 4) provider cascade — fall through only on error / exhaustion / "unknown"
    for name, fn in _PROVIDER_FNS:
        if not APIS[name]["key"]:
            r._add(name, "skipped", "no_key")
            continue
        _bump(f"{name}_calls")
        try:
            status, reason, raw = fn(email)
        except Exception as e:
            _bump(f"{name}_error")
            _warn(f"{name} error on {email}: {e}")
            r._add(name, "error", str(e)[:120])
            continue
        r._add(name, status, reason)
        if verbose:
            print(f"    · {name}: {status} ({reason})")
        if status in ("valid", "invalid", "risky"):
            # a valid role inbox is still a shared mailbox → downgrade to flagged
            if status == "valid" and is_role:
                return r._finalize("risky", "role_account", name, raw)
            return r._finalize(status, reason, name, raw)
        # status == "unknown" → try the next provider

    # 5) cascade exhausted (no keys, all errored, or all inconclusive)
    if not live:
        _bump("local_only")
        _warn(f"no verifier keys set — {email} judged on syntax/MX only "
              f"(set REOON_API_KEY to enable SMTP verification)")
    if is_role:
        return r._finalize("risky", "role_account_unconfirmed", "local")
    return r._finalize("unknown", "all_providers_unknown", "local")


def verify_emails(emails, verbose=False):
    """Verify a batch and print a routed summary. Returns list[VerifyResult]."""
    results = [verify_email(e, verbose=verbose) for e in emails]
    print("\n  ── Results ──")
    for res in results:
        print(f"  {res!r}")
    _print_summary(results)
    return results


def _print_summary(results):
    buckets = {"draft": [], "draft_unverified": [], "skip": []}
    for res in results:
        buckets[res.route].append(res)
    print("\n  ── Routing ──")
    print(f"    → draft (verified valid):     {len(buckets['draft'])}")
    print(f"    → draft + ⚠ EMAIL UNVERIFIED: {len(buckets['draft_unverified'])}")
    print(f"    → skip (do not email):        {len(buckets['skip'])}")
    errs = {k: v for k, v in COUNTERS.items() if k.endswith("_error")}
    if errs:
        print(f"\n  ⚠ provider errors (not silent): {errs}")
    if COUNTERS.get("local_only"):
        print("  ⚠ ran LOCAL-ONLY (no verifier keys) — "
              "'unverified' counts are unconfirmed, not SMTP-checked.")


# ── STATUS ────────────────────────────────────────────────────────────────────
def _status():
    print("  Email verifier — provider cascade (primary → fallback → fallback):")
    for name, _ in _PROVIDER_FNS:
        a = APIS[name]
        have = "✅ live" if a["key"] else "— (no key)"
        print(f"    {name:<11} {have:<12} rate: {a['rate']}")
    if not any(APIS[n]["key"] for n, _ in _PROVIDER_FNS):
        print("\n  No keys yet → verifier runs LOCAL-ONLY (syntax + MX). "
              "Add REOON_API_KEY to turn on real SMTP verification.")


# ── CLI ───────────────────────────────────────────────────────────────────────
def _self_test():
    print("  No emails given — running the LOCAL-layer self-test "
          "(proves syntax/MX/disposable/role without any API keys):\n")
    samples = [
        "not-an-email",                                  # → invalid / bad_syntax
        "hi@mailinator.com",                             # → invalid / disposable
        "nobody@nonexistent-domain-xyz-98765.com",       # → invalid / no_mx
        "partnerships@github.com",                       # → risky / role inbox (MX exists)
        "jane.doe@google.com",                           # → google-hosted, personal-looking
    ]
    verify_emails(samples, verbose=True)


def main(argv):
    args = [a for a in argv if a]
    if "-h" in args or "--help" in args:
        print(__doc__)
        return
    if "--status" in args:
        _status()
        return
    emails = [a for a in args if not a.startswith("-")]
    if not emails:
        _self_test()
        return
    verify_emails(emails, verbose=True)


if __name__ == "__main__":
    main(sys.argv[1:])
