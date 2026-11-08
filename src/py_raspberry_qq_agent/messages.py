from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .storage import append_jsonl, read_jsonl


class MessageStore:
    def __init__(
        self,
        path: Path,
        max_fetch: int = 500,
        max_content_chars: int = 12000,
    ) -> None:
        self._path = path
        self._max_fetch = max_fetch
        self._max_content_chars = max_content_chars
        self._lock = asyncio.Lock()

    async def append(
        self,
        *,
        chat_type: str,
        chat_id: str,
        sender_id: str,
        sender_name: str,
        content: str,
        timestamp: float,
        message_id: str = "",
        mentioned_bot: bool = False,
        mentions: list[dict[str, str]] | None = None,
    ) -> None:
        record = {
            "chat_type": chat_type,
            "chat_id": chat_id,
            "sender_id": sender_id,
            "sender_name": sender_name[:128],
            "content": content[: self._max_content_chars],
            "timestamp": timestamp,
            "message_id": message_id,
            "mentioned_bot": mentioned_bot,
            "mentions": mentions or [],
        }
        async with self._lock:
            await asyncio.to_thread(append_jsonl, self._path, record)

    async def history(
        self,
        chat_type: str,
        chat_id: str,
        *,
        limit: int = 50,
        start_time: float | None = None,
        end_time: float | None = None,
    ) -> list[dict[str, Any]]:
        records = await asyncio.to_thread(read_jsonl, self._path)
        selected = [
            item
            for item in records
            if item.get("chat_type") == chat_type
            and item.get("chat_id") == chat_id
            and (start_time is None or float(item.get("timestamp", 0)) >= start_time)
            and (end_time is None or float(item.get("timestamp", 0)) < end_time)
        ]
        selected = selected[-min(limit, self._max_fetch) :]
        return selected

    async def group_message_count(
        self,
        chat_id: str,
        start_time: float,
        end_time: float,
    ) -> int:
        return len(
            await self.history(
                "group",
                chat_id,
                limit=self._max_fetch,
                start_time=start_time,
                end_time=end_time,
            )
        )

    @staticmethod
    def now_timestamp() -> float:
        return datetime.now(UTC).timestamp()
