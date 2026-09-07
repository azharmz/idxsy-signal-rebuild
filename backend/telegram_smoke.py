#!/usr/bin/env python3
"""Bounded Telegram smoke runner for IDXSY Signal rebuild.

Uses the production S3 ingestion path but limits the fetched message set to the
first N messages in ascending native-id order. This keeps cursor advancement
contiguous during smoke testing and avoids skipping unpreserved messages.
"""

from __future__ import annotations

import os

import telegram_ingest as ingest


MAX_MESSAGES = int(os.environ.get("SMOKE_MAX_MESSAGES", "5"))
if MAX_MESSAGES < 1:
    raise ValueError("SMOKE_MAX_MESSAGES must be >= 1")

_original_fetch_messages = ingest.fetch_messages


def bounded_fetch_messages(client, cursor):
    messages = _original_fetch_messages(client, cursor)
    messages = sorted(messages, key=lambda message: message.id)
    return messages[:MAX_MESSAGES]


ingest.fetch_messages = bounded_fetch_messages


if __name__ == "__main__":
    raise SystemExit(ingest.main())
