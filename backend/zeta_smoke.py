#!/usr/bin/env python3
"""Bounded smoke wrapper for the canonical Zeta S4 sync.

Uses the production S4 implementation unchanged, but limits Member API rows
before processing. Intended only for manual pre-cutover validation.
"""

from __future__ import annotations

import os

try:
    from backend import zeta_sync
except ModuleNotFoundError:
    import zeta_sync  # type: ignore

MAX_ROWS = max(1, int(os.environ.get("ZETA_SMOKE_MAX_ROWS", "5")))
_original_fetch = zeta_sync.fetch_member_signals


def limited_fetch_member_signals():
    rows = _original_fetch()
    selected = rows[:MAX_ROWS]
    print(f"ZETA_SMOKE fetched_total={len(rows)} selected={len(selected)} max_rows={MAX_ROWS}")
    return selected


zeta_sync.fetch_member_signals = limited_fetch_member_signals

if __name__ == "__main__":
    raise SystemExit(zeta_sync.main())
