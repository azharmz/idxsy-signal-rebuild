#!/usr/bin/env python3
"""Read-only connectivity preflight for IDXSY Signal rebuild.

Checks configured secrets without mutating Supabase, Telegram, or Zeta.
"""

from __future__ import annotations

import asyncio
import os
import sys

import requests
from supabase import create_client
from telethon import TelegramClient
from telethon.sessions import StringSession

REQUIRED = [
    "SUPABASE_URL",
    "SUPABASE_SERVICE_KEY",
    "SUPABASE_USER_ID",
    "TG_API_ID",
    "TG_API_HASH",
    "TG_SESSION_STRING",
    "TG_GROUP_ID",
    "TG_TOPIC_ID",
    "ZETA_MEMBER_COOKIE",
]


def require_env() -> None:
    missing = [name for name in REQUIRED if not os.environ.get(name, "").strip()]
    if missing:
        raise RuntimeError("Missing required secrets: " + ", ".join(missing))


def check_supabase() -> None:
    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
    response = (
        sb.table("signals")
        .select("id", count="exact")
        .eq("user_id", os.environ["SUPABASE_USER_ID"])
        .limit(1)
        .execute()
    )
    count = getattr(response, "count", None)
    print(f"SUPABASE_OK signals_count={count if count is not None else 'available'}")


def check_zeta() -> None:
    cookie = os.environ["ZETA_MEMBER_COOKIE"].strip()
    if "=" not in cookie:
        cookie = f"zeta_member={cookie}"
    response = requests.get(
        "https://member.zeta-ai.pro/api/signals",
        headers={"Cookie": cookie, "Accept": "application/json"},
        timeout=(15, 45),
    )
    if response.status_code in (401, 403):
        raise RuntimeError(f"Zeta member auth failed: HTTP {response.status_code}")
    response.raise_for_status()
    payload = response.json()
    rows = payload if isinstance(payload, list) else payload.get("signals") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("Zeta member API returned no signal rows")
    print(f"ZETA_OK rows={len(rows)}")


async def check_telegram() -> None:
    client = TelegramClient(
        StringSession(os.environ["TG_SESSION_STRING"]),
        int(os.environ["TG_API_ID"]),
        os.environ["TG_API_HASH"],
    )
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram session is not authorized")
        entity = await client.get_entity(int(os.environ["TG_GROUP_ID"]))
        title = getattr(entity, "title", None) or str(getattr(entity, "id", "unknown"))
        print(f"TELEGRAM_OK group={title} topic_id={os.environ['TG_TOPIC_ID']}")
    finally:
        await client.disconnect()


def main() -> None:
    require_env()
    check_supabase()
    check_zeta()
    asyncio.run(check_telegram())
    print("PREFLIGHT_PASS")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"PREFLIGHT_FAIL {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
