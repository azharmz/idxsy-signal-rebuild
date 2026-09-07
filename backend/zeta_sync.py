#!/usr/bin/env python3
"""IDXSY Signal S4 — canonical Zeta Member API synchronization.

Optimized live flow:
    fetch Member API once
    preload canonical Zeta/source/link/assertion/outcome state in batches
    compare in memory
    write/RPC only rows that are new, changed, incomplete, or conflicted

Canonical ownership is unchanged. `trades_data` is intentionally not used.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from supabase import Client, create_client
from urllib3.util.retry import Retry

try:
    from backend.outcome_resolver import (
        RESOLVER_VERSION,
        assertion_semantically_equal,
        build_assertion_payload,
        is_final_zeta_status,
    )
    from backend.reconciliation import match_zeta_to_telegram
except ModuleNotFoundError:
    from outcome_resolver import RESOLVER_VERSION, assertion_semantically_equal, build_assertion_payload, is_final_zeta_status
    from reconciliation import match_zeta_to_telegram

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
# supabase-py/httpx logs every REST request at INFO; that made a normal 1k-row
# reconciliation produce thousands of lines. Keep application summaries/errors.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("zeta-sync")

MEMBER_API_URL = "https://member.zeta-ai.pro/api/signals"
PARSER_VERSION = "zeta-live-v1"
WIB = ZoneInfo("Asia/Jakarta")
UTC = timezone.utc
PAGE_SIZE = 1000

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
SUPABASE_USER_ID = os.environ["SUPABASE_USER_ID"]
ZETA_MEMBER_COOKIE = os.environ.get("ZETA_MEMBER_COOKIE", "").strip()

sb: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# Per-run snapshots. They remove O(rows) read round-trips while preserving the
# same canonical writes and RPC boundaries for actual deltas.
CACHE: dict[str, Any] = {
    "sources_by_native": {},
    "links_by_source": {},
    "assertions_by_source": {},
    "active_outcomes_by_signal": {},
    "reconciliation_by_key": {},
}


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def first_row(response: Any) -> dict[str, Any] | None:
    rows = response.data or []
    return rows[0] if rows else None


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def payload_equal(left: Any, right: Any) -> bool:
    try:
        return canonical_json(left) == canonical_json(right)
    except (TypeError, ValueError):
        return left == right


def fingerprint_payload(raw: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(raw).encode("utf-8")).hexdigest()


def fetch_all(table: str, columns: str, filters: Callable[[Any], Any] | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    start = 0
    while True:
        query = sb.table(table).select(columns).eq("user_id", SUPABASE_USER_ID)
        if filters:
            query = filters(query)
        batch = query.range(start, start + PAGE_SIZE - 1).execute().data or []
        rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            return rows
        start += PAGE_SIZE


def preload_state() -> dict[str, int]:
    sources = fetch_all(
        "source_records",
        "id,native_source_id,raw_payload,fingerprint,parser_version,source_record_type,source_timestamp_raw,source_timestamp_utc,market_date,source_timezone_semantics",
        lambda q: q.eq("source_system", "ZETA"),
    )
    source_ids = {str(r["id"]) for r in sources}

    links = fetch_all("signal_source_links", "id,signal_id,source_record_id,match_method,match_confidence,evidence,review_status")
    links = [r for r in links if str(r.get("source_record_id")) in source_ids]

    assertions = fetch_all(
        "signal_outcome_assertions",
        "id,signal_id,source_record_id,outcome_status,profit_pct,source_resolved_at,assertion_data",
    )
    assertions = [r for r in assertions if str(r.get("source_record_id")) in source_ids]

    active_outcomes = fetch_all(
        "signal_outcomes",
        "id,signal_id,outcome_status,resolver_version,authority,supporting_evidence,superseded_at",
        lambda q: q.is_("superseded_at", "null"),
    )

    reconciliation = fetch_all(
        "reconciliation_items",
        "id,source_record_id,item_type,status,match_confidence,proposed_action,conflict_type,evidence,review_note,reviewed_at",
    )
    reconciliation = [r for r in reconciliation if str(r.get("source_record_id")) in source_ids]

    CACHE["sources_by_native"] = {str(r["native_source_id"]): r for r in sources}
    CACHE["links_by_source"] = {str(r["source_record_id"]): r for r in links}
    CACHE["assertions_by_source"] = {str(r["source_record_id"]): r for r in assertions}
    CACHE["active_outcomes_by_signal"] = {str(r["signal_id"]): r for r in active_outcomes}
    CACHE["reconciliation_by_key"] = {
        (str(r["source_record_id"]), str(r["item_type"])): r
        for r in reconciliation if r.get("source_record_id")
    }
    stats = {
        "zeta_sources": len(sources),
        "zeta_links": len(links),
        "zeta_assertions": len(assertions),
        "active_outcomes": len(active_outcomes),
        "zeta_reconciliation": len(reconciliation),
    }
    log.info("S4 preload complete: %s", stats)
    return stats


def member_session() -> requests.Session:
    retry = Retry(
        total=4, connect=4, read=4, status=4, backoff_factor=2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}), respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def fetch_member_signals() -> list[dict[str, Any]]:
    if not ZETA_MEMBER_COOKIE:
        raise RuntimeError("ZETA_MEMBER_COOKIE is required; public API fallback is disabled")
    cookie = ZETA_MEMBER_COOKIE if "=" in ZETA_MEMBER_COOKIE else f"zeta_member={ZETA_MEMBER_COOKIE}"
    response = member_session().get(
        MEMBER_API_URL,
        headers={"Cookie": cookie, "Accept": "application/json"},
        timeout=(15, 45),
    )
    if response.status_code in (401, 403):
        raise RuntimeError(f"Zeta member API auth failed ({response.status_code}); refresh ZETA_MEMBER_COOKIE")
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict) and isinstance(payload.get("signals"), list):
        rows = payload["signals"]
    else:
        raise RuntimeError("Unexpected Zeta member API response shape")
    rows = [r for r in rows if isinstance(r, dict)]
    if not rows:
        raise RuntimeError("Zeta member API returned zero signal rows; refusing empty canonical sync")
    return rows


def parse_wib_timestamp(value: Any) -> tuple[str | None, str | None]:
    if value in (None, ""):
        return None, None
    raw = str(value).strip()
    try:
        naive = datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None, None
    local = naive.replace(tzinfo=WIB)
    return local.astimezone(UTC).isoformat(), local.date().isoformat()


def normalized_zeta(raw: dict[str, Any]) -> dict[str, Any]:
    if raw.get("id") is None:
        raise ValueError("Zeta row has no native id")
    source_utc, market_date = parse_wib_timestamp(raw.get("timestamp"))
    resolved_utc, _ = parse_wib_timestamp(raw.get("resolved_at"))
    if not source_utc:
        raise ValueError("Zeta row has invalid/missing timestamp")
    ticker = str(raw.get("symbol") or "").strip().upper()
    if not ticker:
        raise ValueError("Zeta row has no symbol")
    return {
        "zeta_id": str(raw["id"]), "ticker": ticker,
        "signal_type": raw.get("decision"),
        "source_timestamp_raw": str(raw.get("timestamp") or ""),
        "source_timestamp_utc": source_utc, "market_date": market_date,
        "entry_price": raw.get("close_price"), "tp1_price": raw.get("take_profit"),
        "sl_default_price": raw.get("stop_loss"),
        "status": str(raw.get("status") or "").strip().upper(),
        "profit_pct": raw.get("profit_pct"), "resolved_at_raw": raw.get("resolved_at"),
        "resolved_at_utc": resolved_utc, "raw": raw,
    }


def create_batch() -> str:
    row = first_row(sb.table("source_batches").insert({
        "user_id": SUPABASE_USER_ID, "batch_type": "LIVE_ZETA", "status": "COMMITTING",
        "source_label": "Zeta Member API live synchronization", "parser_version": PARSER_VERSION,
        "summary": {"operation": "LIVE_ZETA_SYNC", "source_interface": "MEMBER_API"},
    }).execute())
    if not row:
        raise RuntimeError("Failed to create LIVE_ZETA source batch")
    return str(row["id"])


def finish_batch(batch_id: str, status: str, summary: dict[str, Any]) -> None:
    sb.table("source_batches").update({
        "status": status, "completed_at": utc_now_iso(),
        "summary": {"operation": "LIVE_ZETA_SYNC", **summary},
        "verification_result": summary,
    }).eq("id", batch_id).eq("user_id", SUPABASE_USER_ID).execute()


def source_by_native_id(native_id: str) -> dict[str, Any] | None:
    return CACHE["sources_by_native"].get(str(native_id))


def preserve_source(raw: dict[str, Any], batch_id: str) -> tuple[dict[str, Any], bool]:
    if raw.get("id") is None:
        raise ValueError("Cannot preserve Zeta source without native id")
    native_id = str(raw["id"])
    existing = source_by_native_id(native_id)
    source_utc, market_date = parse_wib_timestamp(raw.get("timestamp"))
    payload = {
        "source_record_type": "SIGNAL",
        "source_timestamp_raw": str(raw.get("timestamp") or "") or None,
        "source_timezone_semantics": "KNOWN_WIB" if source_utc else "UNKNOWN",
        "source_timestamp_utc": source_utc, "market_date": market_date,
        "raw_payload": raw, "fingerprint": fingerprint_payload(raw), "parser_version": PARSER_VERSION,
    }
    if existing:
        if payload_equal(existing.get("raw_payload"), raw):
            return existing, False
        row = first_row(
            sb.table("source_records").update(payload)
            .eq("id", existing["id"]).eq("user_id", SUPABASE_USER_ID).execute()
        ) or {**existing, **payload}
        CACHE["sources_by_native"][native_id] = row
        return row, True

    row = first_row(sb.table("source_records").insert({
        "user_id": SUPABASE_USER_ID, "batch_id": batch_id, "source_system": "ZETA",
        "native_source_id": native_id, **payload,
    }).execute())
    if not row:
        raise RuntimeError(f"Failed to persist Zeta source {native_id}")
    CACHE["sources_by_native"][native_id] = row
    return row, True


def link_for_source(source_record_id: str):
    return CACHE["links_by_source"].get(str(source_record_id))


def assertion_for_source(source_record_id: str):
    return CACHE["assertions_by_source"].get(str(source_record_id))


def active_outcome(signal_id: str):
    return CACHE["active_outcomes_by_signal"].get(str(signal_id))


def reconciliation_for_source(source_record_id: str, item_type: str):
    return CACHE["reconciliation_by_key"].get((str(source_record_id), str(item_type)))


def record_reconciliation(*, batch_id, source_record_id, item_type, status, proposed_action, conflict_type, evidence, match_confidence=None):
    key = (str(source_record_id), str(item_type))
    existing = CACHE["reconciliation_by_key"].get(key)
    values = {
        "status": status, "match_confidence": match_confidence,
        "proposed_action": proposed_action, "conflict_type": conflict_type,
        "evidence": evidence, "review_note": None, "reviewed_at": None,
    }
    if existing:
        row = first_row(sb.table("reconciliation_items").update(values).eq("id", existing["id"]).execute()) or {**existing, **values}
    else:
        row = first_row(sb.table("reconciliation_items").insert({
            "user_id": SUPABASE_USER_ID, "batch_id": batch_id,
            "source_record_id": source_record_id, "item_type": item_type, **values,
        }).execute()) or {"source_record_id": source_record_id, "item_type": item_type, **values}
    CACHE["reconciliation_by_key"][key] = row


def resolve_reconciliation(source_record_id: str, item_types: list[str], note: str):
    for item_type in item_types:
        key = (str(source_record_id), item_type)
        row = CACHE["reconciliation_by_key"].get(key)
        if row and row.get("status") != "ACCEPTED":
            values = {"status": "ACCEPTED", "review_note": note, "reviewed_at": utc_now_iso()}
            updated = first_row(sb.table("reconciliation_items").update(values).eq("id", row["id"]).execute()) or {**row, **values}
            CACHE["reconciliation_by_key"][key] = updated


def derivation_complete(source, parsed):
    sid = str(source["id"])
    link = link_for_source(sid)
    if not link:
        return reconciliation_for_source(sid, "AMBIGUOUS_ZETA_SIGNAL") is not None
    if not is_final_zeta_status(parsed.get("status")):
        return True
    return assertion_for_source(sid) is not None and active_outcome(str(link["signal_id"])) is not None


def telegram_candidates(parsed):
    # Used only for new/unlinked Zeta deltas. Normal unchanged runs never enter
    # this path, so the targeted lookups here are intentionally retained.
    ts = datetime.fromisoformat(parsed["source_timestamp_utc"])
    start = (ts - timedelta(seconds=300)).isoformat()
    end = (ts + timedelta(seconds=300)).isoformat()
    signals = sb.table("signals").select(
        "id,ticker,signal_timestamp,entry_price,tp1_price,sl_default_price"
    ).eq("user_id", SUPABASE_USER_ID).eq("ticker", parsed["ticker"]).gte(
        "signal_timestamp", start
    ).lte("signal_timestamp", end).execute().data or []
    if not signals:
        return []
    signal_ids = [r["id"] for r in signals]
    links = sb.table("signal_source_links").select("signal_id,source_record_id").eq(
        "user_id", SUPABASE_USER_ID
    ).in_("signal_id", signal_ids).execute().data or []
    if not links:
        return []
    tg = sb.table("source_records").select("id,native_source_id,source_timestamp_utc").eq(
        "user_id", SUPABASE_USER_ID
    ).eq("source_system", "TELEGRAM").eq("source_record_type", "SIGNAL").in_(
        "id", [r["source_record_id"] for r in links]
    ).execute().data or []
    tg_by_id = {r["id"]: r for r in tg}
    by_signal = {
        str(l["signal_id"]): tg_by_id[l["source_record_id"]]
        for l in links if l["source_record_id"] in tg_by_id
    }
    return [
        {**s, "telegram_source_record_id": by_signal[str(s["id"])]["id"],
         "telegram_msg_id": by_signal[str(s["id"])]["native_source_id"]}
        for s in signals if str(s["id"]) in by_signal
    ]


def zeta_signal_payload(parsed):
    return {
        "ticker": parsed["ticker"], "signal_type": parsed.get("signal_type"),
        "signal_timestamp": parsed["source_timestamp_utc"], "market_date": parsed["market_date"],
        "entry_price": parsed.get("entry_price"), "tp1_price": parsed.get("tp1_price"),
        "tp1_pct": None, "tp2_price": None, "tp2_pct": None,
        "sl_default_price": parsed.get("sl_default_price"), "sl_default_pct": None,
        "sl_moderat_price": None, "sl_moderat_pct": None,
        "sl_konservatif_price": None, "sl_konservatif_pct": None,
        "confidence_score": None, "confidence_label": None, "detail": {},
    }


def ensure_zeta_signal(source, parsed, batch_id):
    sid = str(source["id"])
    existing = link_for_source(sid)
    if existing:
        response = sb.rpc("ensure_zeta_publication", {
            "p_user_id": SUPABASE_USER_ID, "p_source_record_id": source["id"],
            "p_signal": zeta_signal_payload(parsed),
        }).execute()
        resolve_reconciliation(sid, ["AMBIGUOUS_ZETA_SIGNAL", "UNPARSED_SOURCE"], "Zeta source successfully linked")
        return str(response.data)

    match = match_zeta_to_telegram({
        "source_timestamp_utc": parsed["source_timestamp_utc"],
        "entry_price": parsed.get("entry_price"), "tp1_price": parsed.get("tp1_price"),
        "sl_default_price": parsed.get("sl_default_price"),
    }, telegram_candidates(parsed))

    if match.result == "AMBIGUOUS":
        record_reconciliation(
            batch_id=batch_id, source_record_id=sid, item_type="AMBIGUOUS_ZETA_SIGNAL",
            status="REVIEW_REQUIRED", match_confidence="AMBIGUOUS", proposed_action="REVIEW",
            conflict_type="MULTIPLE_TELEGRAM_CANDIDATES",
            evidence={"source_system": "ZETA", "native_source_id": parsed["zeta_id"],
                      "parser_version": PARSER_VERSION, **(match.evidence or {})},
        )
        return None

    if match.result == "MATCHED" and match.signal_id:
        row = first_row(sb.table("signal_source_links").insert({
            "user_id": SUPABASE_USER_ID, "signal_id": match.signal_id,
            "source_record_id": source["id"], "match_method": "TELEGRAM_ZETA_RECONCILIATION",
            "match_confidence": "HIGH_CONFIDENCE", "review_status": "AUTO_ACCEPTED",
            "evidence": {"zeta_id": parsed["zeta_id"], "telegram_msg_id": match.telegram_msg_id,
                         **(match.evidence or {})},
        }).execute())
        CACHE["links_by_source"][sid] = row or {
            "signal_id": match.signal_id, "source_record_id": source["id"],
            "match_method": "TELEGRAM_ZETA_RECONCILIATION",
        }
        resolve_reconciliation(sid, ["AMBIGUOUS_ZETA_SIGNAL", "UNPARSED_SOURCE"], "Zeta source matched Telegram publication")
        return str(match.signal_id)

    response = sb.rpc("ensure_zeta_publication", {
        "p_user_id": SUPABASE_USER_ID, "p_source_record_id": source["id"],
        "p_signal": zeta_signal_payload(parsed),
    }).execute()
    signal_id = str(response.data)
    CACHE["links_by_source"][sid] = {
        "signal_id": signal_id, "source_record_id": source["id"],
        "match_method": "ZETA_ONLY_PUBLICATION",
    }
    resolve_reconciliation(sid, ["AMBIGUOUS_ZETA_SIGNAL", "UNPARSED_SOURCE"], "ZETA_ONLY publication created")
    return signal_id


def upsert_assertion(source, signal_id, parsed, batch_id):
    sid = str(source["id"])
    existing = assertion_for_source(sid)
    if not is_final_zeta_status(parsed.get("status")):
        if existing:
            record_reconciliation(
                batch_id=batch_id, source_record_id=sid, item_type="ZETA_STATUS_REGRESSION",
                status="QUARANTINED", proposed_action="REVIEW", conflict_type="FINAL_TO_NONFINAL",
                evidence={"source_system": "ZETA", "native_source_id": parsed["zeta_id"],
                          "current_status": parsed.get("status"),
                          "existing_assertion_status": existing.get("outcome_status")},
            )
        return False

    assertion = build_assertion_payload(
        user_id=SUPABASE_USER_ID, signal_id=signal_id, source_record_id=sid, source=parsed
    )
    changed = True
    if existing:
        changed = not assertion_semantically_equal(existing, assertion)
        if changed:
            values = {
                "signal_id": assertion["signal_id"], "outcome_status": assertion["outcome_status"],
                "profit_pct": assertion["profit_pct"], "source_resolved_at": assertion["source_resolved_at"],
                "assertion_data": assertion["assertion_data"],
            }
            row = first_row(sb.table("signal_outcome_assertions").update(values).eq("id", existing["id"]).execute()) or {**existing, **values}
            CACHE["assertions_by_source"][sid] = row
    else:
        row = first_row(sb.table("signal_outcome_assertions").insert(assertion).execute()) or assertion
        CACHE["assertions_by_source"][sid] = row

    # Resolver is required for a changed/new assertion, or if an outcome is
    # missing. Do not call it for a complete unchanged final row.
    current_outcome = active_outcome(signal_id)
    if changed or current_outcome is None:
        sb.rpc("resolve_zeta_outcome", {
            "p_user_id": SUPABASE_USER_ID, "p_signal_id": signal_id,
        }).execute()
        CACHE["active_outcomes_by_signal"][str(signal_id)] = {
            "signal_id": str(signal_id), "outcome_status": assertion["outcome_status"],
            "resolver_version": RESOLVER_VERSION, "authority": "ZETA",
        }
    resolve_reconciliation(sid, ["ZETA_STATUS_REGRESSION"], "Final Zeta assertion restored")
    return changed


def process_source(raw, batch_id):
    source, changed = preserve_source(raw, batch_id)
    try:
        parsed = normalized_zeta(raw)
    except Exception as exc:
        record_reconciliation(
            batch_id=batch_id, source_record_id=str(source["id"]), item_type="UNPARSED_SOURCE",
            status="QUARANTINED", proposed_action="REPROCESS", conflict_type="PARSE_FAILED",
            evidence={"source_system": "ZETA", "native_source_id": str(raw.get("id")),
                      "parser_version": PARSER_VERSION, "error_code": type(exc).__name__,
                      "error_message": str(exc), "source_record_type_candidate": "SIGNAL"},
        )
        return {"result": "UNPARSED", "changed": changed, "final": False}

    if not changed and derivation_complete(source, parsed):
        return {"result": "UNCHANGED", "changed": False,
                "final": is_final_zeta_status(parsed.get("status"))}

    signal_id = ensure_zeta_signal(source, parsed, batch_id)
    if not signal_id:
        return {"result": "AMBIGUOUS", "changed": changed, "final": False}
    assertion_changed = upsert_assertion(source, signal_id, parsed, batch_id)
    return {
        "result": "SYNCED", "changed": changed, "signal_id": signal_id,
        "final": is_final_zeta_status(parsed.get("status")),
        "assertion_changed": assertion_changed,
    }


def main():
    batch_id = create_batch()
    stats = {
        "api_rows": 0, "synced": 0, "unchanged": 0, "unparsed": 0, "ambiguous": 0,
        "final_rows": 0, "assertions_changed": 0, "member_api_only": True,
        "resolver_version": RESOLVER_VERSION, "preloaded": True,
    }
    try:
        rows = fetch_member_signals()
        stats["api_rows"] = len(rows)
        preload_state()
        for raw in rows:
            result = process_source(raw, batch_id)
            if result.get("final"):
                stats["final_rows"] += 1
            key = result["result"].lower()
            if key in stats:
                stats[key] += 1
            if result.get("assertion_changed"):
                stats["assertions_changed"] += 1
        status = "PARTIAL_REVIEW_REQUIRED" if stats["unparsed"] or stats["ambiguous"] else "VERIFIED"
        finish_batch(batch_id, status, stats)
        log.info("Zeta S4 complete: %s", stats)
    except Exception as exc:
        stats["error"] = str(exc)
        try:
            finish_batch(batch_id, "FAILED", stats)
        finally:
            log.exception("Zeta S4 failed")
        raise


if __name__ == "__main__":
    main()
