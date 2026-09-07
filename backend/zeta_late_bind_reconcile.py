#!/usr/bin/env python3
"""Late-bind canonical reconciliation for ZETA_ONLY publications.

This maintenance pass handles the valid race where a Zeta publication was
canonicalized before its Telegram counterpart arrived. It does not fuzzy-dedup
frontend rows. It reuses the frozen canonical matcher and only calls the atomic
merge RPC for a defensible MATCHED result.
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from typing import Any, Callable

from supabase import create_client

try:
    from backend.reconciliation import match_zeta_to_telegram
    from backend.zeta_sync import normalized_zeta
except ModuleNotFoundError:
    from reconciliation import match_zeta_to_telegram
    from zeta_sync import normalized_zeta

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("zeta-late-bind")

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
SUPABASE_USER_ID = os.environ["SUPABASE_USER_ID"]
PAGE = 1000
sb = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def fetch_all(table: str, columns: str, filters: Callable[[Any], Any] | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    start = 0
    while True:
        q = sb.table(table).select(columns).eq("user_id", SUPABASE_USER_ID)
        if filters:
            q = filters(q)
        batch = q.range(start, start + PAGE - 1).execute().data or []
        rows.extend(batch)
        if len(batch) < PAGE:
            return rows
        start += PAGE


def preload() -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]], set[str]]:
    sources = fetch_all(
        "source_records",
        "id,source_system,source_record_type,native_source_id,raw_payload,source_timestamp_utc",
    )
    links = fetch_all(
        "signal_source_links",
        "id,signal_id,source_record_id,match_method,match_confidence,evidence,review_status",
    )
    signals = fetch_all(
        "signals",
        "id,ticker,signal_timestamp,entry_price,tp1_price,sl_default_price",
    )

    source_by_id = {str(r["id"]): r for r in sources}
    links_by_signal: dict[str, list[dict[str, Any]]] = defaultdict(list)
    links_by_source: dict[str, dict[str, Any]] = {}
    for link in links:
        links_by_signal[str(link["signal_id"])].append(link)
        links_by_source[str(link["source_record_id"])] = link

    telegram_signal_ids: set[str] = set()
    telegram_source_for_signal: dict[str, dict[str, Any]] = {}
    for signal_id, signal_links in links_by_signal.items():
        for link in signal_links:
            src = source_by_id.get(str(link["source_record_id"]))
            if src and src.get("source_system") == "TELEGRAM" and src.get("source_record_type") == "SIGNAL":
                telegram_signal_ids.add(signal_id)
                telegram_source_for_signal[signal_id] = src
                break

    signal_by_id = {str(r["id"]): r for r in signals}
    candidates_by_ticker: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for signal_id in telegram_signal_ids:
        signal = signal_by_id.get(signal_id)
        tg = telegram_source_for_signal.get(signal_id)
        if not signal or not tg:
            continue
        candidates_by_ticker[str(signal.get("ticker") or "").upper()].append({
            **signal,
            "telegram_source_record_id": str(tg["id"]),
            "telegram_msg_id": str(tg["native_source_id"]),
        })

    zeta_only: list[dict[str, Any]] = []
    for src in sources:
        if src.get("source_system") != "ZETA":
            continue
        link = links_by_source.get(str(src["id"]))
        if not link:
            continue
        if str(link["signal_id"]) in telegram_signal_ids:
            continue
        zeta_only.append({"source": src, "link": link})

    return zeta_only, candidates_by_ticker, telegram_signal_ids


def main() -> int:
    zeta_only, candidates_by_ticker, telegram_signal_ids = preload()
    stats = {
        "zeta_only_checked": len(zeta_only),
        "matched": 0,
        "merged": 0,
        "ambiguous": 0,
        "still_zeta_only": 0,
        "unparsed": 0,
        "telegram_canonical_signals": len(telegram_signal_ids),
    }

    for item in zeta_only:
        source = item["source"]
        raw = source.get("raw_payload") or {}
        try:
            parsed = normalized_zeta(raw)
        except Exception as exc:
            stats["unparsed"] += 1
            log.warning("Skip Zeta source %s: %s", source.get("native_source_id"), exc)
            continue

        candidates = candidates_by_ticker.get(parsed["ticker"], [])
        result = match_zeta_to_telegram({
            "source_timestamp_utc": parsed["source_timestamp_utc"],
            "entry_price": parsed.get("entry_price"),
            "tp1_price": parsed.get("tp1_price"),
            "sl_default_price": parsed.get("sl_default_price"),
        }, candidates)

        if result.result == "AMBIGUOUS":
            stats["ambiguous"] += 1
            log.warning(
                "AMBIGUOUS Zeta %s ticker=%s evidence=%s",
                parsed["zeta_id"], parsed["ticker"], result.evidence,
            )
            continue
        if result.result != "MATCHED" or not result.signal_id:
            stats["still_zeta_only"] += 1
            continue

        stats["matched"] += 1
        evidence = {
            "source_system": "ZETA",
            "native_source_id": parsed["zeta_id"],
            "telegram_msg_id": result.telegram_msg_id,
            "late_bind": True,
            **(result.evidence or {}),
        }
        sb.rpc("merge_zeta_only_into_telegram", {
            "p_user_id": SUPABASE_USER_ID,
            "p_zeta_source_record_id": source["id"],
            "p_target_signal_id": result.signal_id,
            "p_evidence": evidence,
        }).execute()
        stats["merged"] += 1
        log.info(
            "MERGED Zeta %s %s -> Telegram canonical signal %s",
            parsed["zeta_id"], parsed["ticker"], result.signal_id,
        )

    log.info("Zeta late-bind reconciliation complete: %s", stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
