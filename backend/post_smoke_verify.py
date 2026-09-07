#!/usr/bin/env python3
"""Read-only post-smoke verification for IDXSY Signal rebuild.

Uses the Supabase REST API only. No inserts, updates, deletes, RPC writes, or
cursor changes are performed.
"""

from __future__ import annotations

import os
from collections import Counter
from typing import Any

from supabase import create_client

URL = os.environ["SUPABASE_URL"]
KEY = os.environ["SUPABASE_SERVICE_KEY"]
USER_ID = os.environ["SUPABASE_USER_ID"]
sb = create_client(URL, KEY)
PAGE = 1000


def fetch_all(table: str, columns: str, filters=None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    start = 0
    while True:
        q = sb.table(table).select(columns).eq("user_id", USER_ID)
        if filters:
            q = filters(q)
        batch = q.range(start, start + PAGE - 1).execute().data or []
        rows.extend(batch)
        if len(batch) < PAGE:
            return rows
        start += PAGE


def count_table(table: str, filters=None) -> int:
    q = sb.table(table).select("id", count="exact", head=True).eq("user_id", USER_ID)
    if filters:
        q = filters(q)
    r = q.execute()
    return int(r.count or 0)


def check(name: str, ok: bool, detail: str) -> bool:
    print(f"{'PASS' if ok else 'FAIL'} {name}: {detail}")
    return ok


def main() -> int:
    good = True

    metrics = [
        ("canonical signals", count_table("signals"), 1138),
        ("source records", count_table("source_records"), 3023),
        ("source links", count_table("signal_source_links"), 2025),
        ("matched signal events", count_table("signal_events"), 460),
        ("Zeta outcome assertions", count_table("signal_outcome_assertions"), 1000),
        ("active canonical outcomes", count_table("signal_outcomes", lambda q: q.is_("superseded_at", "null")), 1000),
        ("Telegram event sources", count_table("source_records", lambda q: q.eq("source_system", "TELEGRAM").eq("source_record_type", "EVENT")), 480),
    ]
    for name, actual, floor in metrics:
        good &= check(name, actual >= floor, f"actual={actual} baseline={floor}")

    sources = fetch_all("source_records", "id,source_system,native_source_id")
    dup_native = [k for k, n in Counter((r["source_system"], r["native_source_id"]) for r in sources).items() if n > 1]
    good &= check("duplicate native source identities", not dup_native, f"duplicates={len(dup_native)}")

    links = fetch_all("signal_source_links", "id,signal_id,source_record_id")
    dup_links = [k for k, n in Counter(r["source_record_id"] for r in links).items() if n > 1]
    good &= check("duplicate source links", not dup_links, f"duplicates={len(dup_links)}")

    events = fetch_all("signal_events", "id,signal_id,source_record_id")
    dup_events = [k for k, n in Counter(r["source_record_id"] for r in events if r.get("source_record_id")).items() if n > 1]
    good &= check("duplicate event source ids", not dup_events, f"duplicates={len(dup_events)}")

    assertions = fetch_all("signal_outcome_assertions", "id,signal_id,source_record_id,outcome_status,assertion_data")
    dup_assertions = [k for k, n in Counter(r["source_record_id"] for r in assertions).items() if n > 1]
    good &= check("duplicate assertion source ids", not dup_assertions, f"duplicates={len(dup_assertions)}")

    outcomes = fetch_all("signal_outcomes", "id,signal_id,outcome_status,resolver_version,authority,superseded_at")
    active = [r for r in outcomes if r.get("superseded_at") is None]
    dup_active = [k for k, n in Counter(r["signal_id"] for r in active).items() if n > 1]
    good &= check("multiple active outcomes", not dup_active, f"signals={len(dup_active)}")

    signals = fetch_all("signals", "id")
    signal_ids = {r["id"] for r in signals}
    source_ids = {r["id"] for r in sources}
    orphan_sets = [
        ("orphan source link -> signal", [r for r in links if r["signal_id"] not in signal_ids]),
        ("orphan source link -> source", [r for r in links if r["source_record_id"] not in source_ids]),
        ("orphan event -> signal", [r for r in events if r["signal_id"] not in signal_ids]),
        ("orphan assertion -> signal", [r for r in assertions if r["signal_id"] not in signal_ids]),
        ("orphan assertion -> source", [r for r in assertions if r["source_record_id"] not in source_ids]),
        ("orphan outcome -> signal", [r for r in outcomes if r["signal_id"] not in signal_ids]),
    ]
    for name, rows in orphan_sets:
        good &= check(name, not rows, f"count={len(rows)}")

    source_system_by_id = {r["id"]: r["source_system"] for r in sources}
    non_zeta_assertions = [r for r in assertions if source_system_by_id.get(r["source_record_id"]) != "ZETA"]
    good &= check("all outcome assertions sourced from ZETA", not non_zeta_assertions, f"count={len(non_zeta_assertions)}")

    # The reconstructed baseline contains historical assertion vocabulary outside
    # the current live resolver's three final statuses. Preserve those facts;
    # only enforce the current S4 vocabulary on assertions written by MEMBER_API.
    vocabulary_counts = Counter(str(r.get("outcome_status")) for r in assertions)
    print(f"INFO baseline assertion vocabulary: {dict(sorted(vocabulary_counts.items()))}")
    live_assertions = [r for r in assertions if (r.get("assertion_data") or {}).get("source_interface") == "MEMBER_API"]
    invalid_live_status = [r for r in live_assertions if r.get("outcome_status") not in {"TP_HIT", "SL_HIT", "EXPIRED"}]
    good &= check("live S4 assertion vocabulary", not invalid_live_status, f"member_api_assertions={len(live_assertions)} invalid={len(invalid_live_status)}")

    bad_authority = [r for r in active if r.get("resolver_version") == "zeta-authority-v1" and r.get("authority") != "ZETA"]
    good &= check("zeta-authority resolver authority", not bad_authority, f"invalid={len(bad_authority)}")

    unresolved = fetch_all("reconciliation_items", "id,source_record_id,item_type,status", lambda q: q.eq("item_type", "UNMATCHED_EVENT").neq("status", "ACCEPTED"))
    unresolved_tg = [r for r in unresolved if source_system_by_id.get(r.get("source_record_id")) == "TELEGRAM"]
    good &= check("unresolved Telegram events baseline", len(unresolved_tg) >= 20, f"actual={len(unresolved_tg)} baseline=20")

    print("POST_SMOKE_VERIFICATION_PASS" if good else "POST_SMOKE_VERIFICATION_FAIL")
    return 0 if good else 1


if __name__ == "__main__":
    raise SystemExit(main())
