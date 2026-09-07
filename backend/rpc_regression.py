#!/usr/bin/env python3
"""Live regression harness for canonical Signal RPCs.

Creates uniquely-named synthetic source evidence under the configured canonical
user, exercises the production RPCs, verifies invariants, and removes all test
rows in a finally block. This is manual hardening, never a scheduled ingest job.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

from supabase import create_client

URL = os.environ["SUPABASE_URL"]
KEY = os.environ["SUPABASE_SERVICE_KEY"]
USER_ID = os.environ["SUPABASE_USER_ID"]
sb = create_client(URL, KEY)
RUN = uuid.uuid4().hex
NOW = datetime.now(timezone.utc).isoformat()
MARK = f"RPC_REGRESSION_{RUN}"
source_ids: list[str] = []
signal_ids: set[str] = set()


def first(resp):
    rows = resp.data or []
    return rows[0] if rows else None


def insert_source(system: str, suffix: str) -> dict:
    row = first(sb.table("source_records").insert({
        "user_id": USER_ID,
        "source_system": system,
        "native_source_id": f"{MARK}_{suffix}",
        "source_record_type": "SIGNAL",
        "source_timezone_semantics": "KNOWN_UTC",
        "source_timestamp_raw": NOW,
        "source_timestamp_utc": NOW,
        "market_date": NOW[:10],
        "raw_payload": {"regression": MARK, "suffix": suffix},
        "parser_version": "rpc-regression-v1",
    }).execute())
    if not row:
        raise AssertionError(f"failed to create {system} source fixture")
    source_ids.append(str(row["id"]))
    return row


def signal_payload(ticker: str) -> dict:
    return {
        "ticker": ticker,
        "signal_type": "BUY",
        "signal_timestamp": NOW,
        "market_date": NOW[:10],
        "entry_price": 100,
        "tp1_price": 110,
        "tp1_pct": 10,
        "tp2_price": None,
        "tp2_pct": None,
        "sl_default_price": 95,
        "sl_default_pct": -5,
        "sl_moderat_price": None,
        "sl_moderat_pct": None,
        "sl_konservatif_price": None,
        "sl_konservatif_pct": None,
        "confidence_score": None,
        "confidence_label": None,
        "detail": {"regression": MARK},
    }


def rpc(name: str, args: dict):
    return sb.rpc(name, args).execute().data


def active_outcomes(signal_id: str):
    return sb.table("signal_outcomes").select("id,outcome_status,superseded_at").eq(
        "user_id", USER_ID
    ).eq("signal_id", signal_id).is_("superseded_at", "null").execute().data or []


def cleanup() -> None:
    # Delete dependants explicitly where useful; FK cascades handle the rest.
    for sid in signal_ids:
        sb.table("signal_outcomes").delete().eq("user_id", USER_ID).eq("signal_id", sid).execute()
        sb.table("signal_outcome_assertions").delete().eq("user_id", USER_ID).eq("signal_id", sid).execute()
        sb.table("signal_source_links").delete().eq("user_id", USER_ID).eq("signal_id", sid).execute()
        sb.table("signals").delete().eq("user_id", USER_ID).eq("id", sid).execute()
    for src in source_ids:
        sb.table("reconciliation_items").delete().eq("user_id", USER_ID).eq("source_record_id", src).execute()
        sb.table("source_records").delete().eq("user_id", USER_ID).eq("id", src).execute()


def main() -> None:
    try:
        # 1) Telegram duplicate publication -> same canonical signal.
        tg = insert_source("TELEGRAM", "TG_DUP")
        p = signal_payload("ZZTGTEST")
        s1 = str(rpc("ensure_telegram_publication", {"p_user_id": USER_ID, "p_source_record_id": tg["id"], "p_signal": p}))
        signal_ids.add(s1)
        s2 = str(rpc("ensure_telegram_publication", {"p_user_id": USER_ID, "p_source_record_id": tg["id"], "p_signal": p}))
        assert s1 == s2, "duplicate Telegram publication created a different signal"
        links = sb.table("signal_source_links").select("id").eq("user_id", USER_ID).eq("source_record_id", tg["id"]).execute().data or []
        assert len(links) == 1, "duplicate Telegram publication created duplicate links"
        print("PASS duplicate Telegram publication -> same canonical signal")

        # 2) RPC validation failure -> no partial publication/link state.
        tg_bad = insert_source("TELEGRAM", "TG_FAIL")
        failed = False
        try:
            rpc("ensure_telegram_publication", {"p_user_id": USER_ID, "p_source_record_id": tg_bad["id"], "p_signal": {"market_date": NOW[:10]}})
        except Exception:
            failed = True
        assert failed, "invalid Telegram RPC unexpectedly succeeded"
        bad_links = sb.table("signal_source_links").select("id").eq("user_id", USER_ID).eq("source_record_id", tg_bad["id"]).execute().data or []
        assert not bad_links, "failed Telegram RPC left partial source link"
        print("PASS RPC failure -> no partial publication/link state")

        # 3) Zeta duplicate publication -> same canonical signal.
        z = insert_source("ZETA", "Z_DUP")
        zp = signal_payload("ZZZTEST")
        zs1 = str(rpc("ensure_zeta_publication", {"p_user_id": USER_ID, "p_source_record_id": z["id"], "p_signal": zp}))
        signal_ids.add(zs1)
        zs2 = str(rpc("ensure_zeta_publication", {"p_user_id": USER_ID, "p_source_record_id": z["id"], "p_signal": zp}))
        assert zs1 == zs2, "duplicate Zeta publication created a different signal"
        print("PASS duplicate Zeta publication -> same canonical signal")

        # 4) Resolver same assertion rerun -> no outcome churn.
        assertion = first(sb.table("signal_outcome_assertions").insert({
            "user_id": USER_ID,
            "signal_id": zs1,
            "source_record_id": z["id"],
            "outcome_status": "TP_HIT",
            "profit_pct": 10,
            "source_resolved_at": NOW,
            "assertion_data": {"source_interface": "MEMBER_API", "regression": MARK},
        }).execute())
        assert assertion, "failed to create Zeta assertion fixture"
        o1 = str(rpc("resolve_zeta_outcome", {"p_user_id": USER_ID, "p_signal_id": zs1}))
        o2 = str(rpc("resolve_zeta_outcome", {"p_user_id": USER_ID, "p_signal_id": zs1}))
        assert o1 == o2, "same final assertion caused outcome churn"
        assert len(active_outcomes(zs1)) == 1, "resolver produced multiple active outcomes"
        print("PASS same final Zeta assertion rerun -> no outcome churn")

        # 5) Changed final state -> supersede previous, exactly one active.
        sb.table("signal_outcome_assertions").update({
            "outcome_status": "SL_HIT",
            "profit_pct": -5,
            "assertion_data": {"source_interface": "MEMBER_API", "regression": MARK, "changed": True},
        }).eq("id", assertion["id"]).execute()
        o3 = str(rpc("resolve_zeta_outcome", {"p_user_id": USER_ID, "p_signal_id": zs1}))
        assert o3 != o1, "changed final assertion did not create a new versioned outcome"
        active = active_outcomes(zs1)
        assert len(active) == 1 and active[0]["outcome_status"] == "SL_HIT", "changed final state did not leave exactly one correct active outcome"
        old = first(sb.table("signal_outcomes").select("id,superseded_at").eq("id", o1).execute())
        assert old and old.get("superseded_at") is not None, "previous outcome was not superseded"
        print("PASS changed Zeta final state -> previous superseded + exactly one active")

        print("RPC_REGRESSION_PASS")
    finally:
        cleanup()


if __name__ == "__main__":
    main()
