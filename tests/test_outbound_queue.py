from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from py_raspberry_qq_agent.allowlist import ChatAllowlist
from py_raspberry_qq_agent.config import AppConfig
from py_raspberry_qq_agent.napcat import NapCatError, OutboundQueue


class RecordingNapCat:
    def __init__(self) -> None:
        self.times: list[float] = []
        self.messages: list[tuple[str, str]] = []

    async def send_private(self, raw_user_id: str, content: str) -> None:
        self.times.append(asyncio.get_running_loop().time())
        self.messages.append((raw_user_id, content))

    async def send_group(self, raw_group_id: str, content: str) -> None:
        self.times.append(asyncio.get_running_loop().time())
        self.messages.append((raw_group_id, content))


@pytest.mark.asyncio
async def test_outbound_queue_enforces_send_interval(tmp_path: Path) -> None:
    config = SimpleNamespace(
        outbound_queue_size=10,
        outbound_min_interval_seconds=0.04,
        outbound_max_interval_seconds=0.04,
    )
    alias = "12345"
    napcat = RecordingNapCat()
    allowlist = ChatAllowlist(
        enabled=False,
        private_qq_ids=[],
        group_ids=[],
    )
    queue = OutboundQueue(config, napcat, allowlist)  # type: ignore[arg-type]

    queue.start()
    queue.enqueue("private", alias, "first")
    queue.enqueue("private", alias, "second")
    await asyncio.sleep(0.13)
    await queue.stop()

    assert [content for _, content in napcat.messages] == ["first", "second"]
    assert napcat.times[1] - napcat.times[0] >= 0.03


def test_outbound_queue_rejects_unlisted_target(tmp_path: Path) -> None:
    config = SimpleNamespace(
        outbound_queue_size=10,
        outbound_min_interval_seconds=0,
        outbound_max_interval_seconds=0,
    )
    allowed_alias = "12345"
    blocked_alias = "54321"
    allowlist = ChatAllowlist(
        enabled=True,
        private_qq_ids=["12345"],
        group_ids=[],
    )
    queue = OutboundQueue(
        config, RecordingNapCat(), allowlist  # type: ignore[arg-type]
    )

    queue.enqueue("private", allowed_alias, "allowed")
    with pytest.raises(PermissionError):
        queue.enqueue("private", blocked_alias, "blocked")


@pytest.mark.asyncio
async def test_delivery_rechecks_allowlist_only_for_model_messages(
    config: AppConfig,
) -> None:
    napcat = RecordingNapCat()
    queue = OutboundQueue(
        config,
        napcat,
        ChatAllowlist(enabled=True, private_qq_ids=["12345"], group_ids=[]),
    )
    queue.enqueue("private", "12345", "model message")
    queue.enqueue_notification("private", "12345", "business notification")
    # Revoke permission after enqueueing, before the worker attempts delivery.
    queue._allowlist = ChatAllowlist(enabled=True, private_qq_ids=[], group_ids=[])
    queue.start()
    try:
        await asyncio.wait_for(queue._queue.join(), timeout=1)
    finally:
        await queue.stop()
    assert napcat.messages == [("12345", "business notification")]


@pytest.mark.asyncio
async def test_outbound_queue_logs_napcat_error_details_and_continues(
    config: AppConfig, caplog: pytest.LogCaptureFixture
) -> None:
    class FailingNapCat(RecordingNapCat):
        async def send_private(self, raw_user_id: str, content: str) -> None:
            if content == "first":
                raise NapCatError("retcode=1200 result=16: add friend first")
            await super().send_private(raw_user_id, content)

    napcat = FailingNapCat()
    queue = OutboundQueue(
        config,
        napcat,
        ChatAllowlist(enabled=False, private_qq_ids=[], group_ids=[]),
    )
    queue.start()
    try:
        queue.enqueue("private", "12345", "first")
        queue.enqueue("private", "12345", "second")
        await asyncio.wait_for(queue._queue.join(), timeout=1)
    finally:
        await queue.stop()
    assert "retcode=1200 result=16: add friend first" in caplog.text
    assert napcat.messages == [("12345", "second")]
