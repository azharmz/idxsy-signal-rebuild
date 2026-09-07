#!/usr/bin/env python3
"""Build the single-file IDXSY Signal frontend for Cloudflare Pages."""

from pathlib import Path

SOURCE = Path("web/idxsy-signal.html")
DIST = Path("dist")

LEGACY_URL = "https://gfxhtpqzebxycijdnpjp.supabase.co"
LEGACY_KEY = "sb_publishable_BXkSOULyV06I6N6DhIIAdw_TJXQUGOS"
CANONICAL_URL = "https://wshtfqqusiifdkqqoejs.supabase.co"
CANONICAL_KEY = "sb_publishable_45BjutlMC-tHSoS-lGXIUQ_AOIuqF1y"

html = SOURCE.read_text(encoding="utf-8")
html = html.replace(LEGACY_URL, CANONICAL_URL).replace(LEGACY_KEY, CANONICAL_KEY)

if LEGACY_URL in html or LEGACY_KEY in html:
    raise SystemExit("Legacy Supabase frontend configuration is still present")
if CANONICAL_URL not in html or CANONICAL_KEY not in html:
    raise SystemExit("Canonical Supabase frontend configuration is missing")

DIST.mkdir(parents=True, exist_ok=True)
(DIST / "index.html").write_text(html, encoding="utf-8")
(DIST / "idxsy-signal.html").write_text(html, encoding="utf-8")
print("Cloudflare Pages build ready: dist/index.html")
