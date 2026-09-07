#!/usr/bin/env python3
"""Build the S6 single-file IDXSY Signal frontend for Cloudflare Pages."""

from pathlib import Path

SOURCE = Path("web/index.html")
DIST = Path("dist")
CANONICAL_URL = "https://wshtfqqusiifdkqqoejs.supabase.co"
CANONICAL_KEY = "sb_publishable_45BjutlMC-tHSoS-lGXIUQ_AOIuqF1y"
FORBIDDEN = (
    "gfxhtpqzebxycijdnpjp.supabase.co",
    "trades_data",
    "recompute_trades",
    "SUPABASE_SERVICE_KEY",
    "ZETA_MEMBER_COOKIE",
    "TG_SESSION_STRING",
)

html = SOURCE.read_text(encoding="utf-8")
for token in FORBIDDEN:
    if token in html:
        raise SystemExit(f"Forbidden frontend token present: {token}")
if CANONICAL_URL not in html or CANONICAL_KEY not in html:
    raise SystemExit("Canonical Supabase frontend configuration is missing")

DIST.mkdir(parents=True, exist_ok=True)
(DIST / "index.html").write_text(html, encoding="utf-8")
print("Cloudflare Pages build ready: dist/index.html")
