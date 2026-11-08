from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from py_raspberry_qq_agent.allowlist import ChatAllowlist
from py_raspberry_qq_agent.app import Application
from py_raspberry_qq_agent.config import AppConfig, ConfigError, MessageGatewayConfig
from py_raspberry_qq_agent.message_gateway import (
    MessageGatewayClient,
    MessageGatewayError,
)
from py_raspberry_qq_agent.napcat import NapCatClient, NapCatError, OutboundQueue


def gateway_config(**overrides: Any) -> MessageGatewayConfig:
    return MessageGatewayConfig(
        **{
            "enabled": True,
            "url": "http://gateway.test",
            "api_key": "test-secret",
            "poll_interval_seconds": 0.001,
            **overrides,
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["user", "group"])
async def test_waits_for_sent_receipt_without_resubmitting(kind: str) -> None:
    requests: list[httpx.Request] = []
    states = iter(
        [
            {"status": "queued"},
            {"status": "retrying", "attempts": 1},
            {"status": "sent", "receipt": {"message_id": "123"}},
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer test-secret"
        if request.method == "POST":
            assert json.loads(request.content) == {
                "platform": "qq",
                "target": {"kind": kind, "id": "10001"},
                "text": "hello",
            }
            return httpx.Response(202, json={"id": "qq-1", "status": "queued"})
        assert request.url.path == "/api/v1/messages/qq-1"
        return httpx.Response(200, json={"id": "qq-1", **next(states)})

    client = MessageGatewayClient(
        gateway_config(), transport=httpx.MockTransport(handler)
    )
    try:
        send = client.send_private if kind == "user" else client.send_group
        assert await send("10001", "hello") == {"message_id": "123"}
    finally:
        await client.close()
    assert [r.method for r in requests] == ["POST", "GET", "GET", "GET"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "failed",
        "expired",
        "timeout",
        "post_timeout",
        "queue_full",
        "missing_receipt",
        "wrong_id",
    ],
)
async def test_failures_never_resubmit(failure: str) -> None:
    posts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        if request.method == "POST":
            posts += 1
            if failure == "post_timeout":
                raise httpx.ReadTimeout("outcome unknown", request=request)
            if failure == "queue_full":
                return httpx.Response(429, json={"error": "message queue is full"})
            return httpx.Response(202, json={"id": "qq-1", "status": "queued"})
        if failure == "expired":
            return httpx.Response(404, json={"error": "unknown delivery id"})
        if failure == "timeout":
            return httpx.Response(200, json={"id": "qq-1", "status": "queued"})
        if failure == "missing_receipt":
            return httpx.Response(200, json={"id": "qq-1", "status": "sent"})
        if failure == "wrong_id":
            return httpx.Response(
                200,
                json={"id": "qq-2", "status": "sent", "receipt": {"message_id": "123"}},
            )
        return httpx.Response(
            200, json={"id": "qq-1", "status": "failed", "error": "permanent rejection"}
        )

    client = MessageGatewayClient(
        gateway_config(delivery_timeout_seconds=0.03),
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises((MessageGatewayError, httpx.HTTPError)):
            await client.send_private("10001", "hello")
    finally:
        await client.close()
    assert posts == 1


@pytest.mark.asyncio
async def test_queue_uses_gateway_receipt_and_skips_local_pacing(
    config: AppConfig,
) -> None:
    config.outbound_min_interval_seconds = config.outbound_max_interval_seconds = 60
    seen: list[str] = []
    posted: list[str] = []

    class Store:
        async def append(self, **message: Any) -> None:
            seen.append(message["message_id"])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posted.append(json.loads(request.content)["target"]["kind"])
            return httpx.Response(
                202, json={"id": f"qq-{len(posted)}", "status": "queued"}
            )
        return httpx.Response(
            200,
            json={
                "id": request.url.path.rsplit("/", 1)[1],
                "status": "sent",
                "receipt": {"message_id": str(100 + len(posted))},
            },
        )

    client = MessageGatewayClient(
        gateway_config(), transport=httpx.MockTransport(handler)
    )
    queue = OutboundQueue(
        config,
        client,
        ChatAllowlist(enabled=False, private_qq_ids=[], group_ids=[]),
        Store(),
    )  # type: ignore[arg-type]
    queue.enqueue("private", "10001", "reply")
    queue.enqueue_notification("group", "20001", "notification")
    assert seen == []
    queue.start()
    try:
        await asyncio.wait_for(queue._queue.join(), timeout=1)
    finally:
        await queue.stop()
        await client.close()
    assert posted == ["user", "group"]
    assert seen == ["101", "102"]


@pytest.mark.asyncio
async def test_application_selects_gateway_and_blocks_direct_napcat_sends(
    config: AppConfig,
) -> None:
    config.message_gateway = gateway_config()
    app = Application.build(config)
    try:
        assert app.outbound._sender is app.message_gateway
        assert isinstance(app.napcat, NapCatClient)
        with pytest.raises(NapCatError, match="must use message-gateway"):
            await app.napcat.call_action(
                "send_group_msg", {"group_id": 20001, "message": "blocked"}
            )
    finally:
        await app.stop()


@pytest.mark.parametrize(
    "overrides",
    [
        {"api_key": ""},
        {"url": "ws://gateway"},
        {"account": "invalid account"},
        {"delivery_timeout_seconds": 0},
        {"poll_interval_seconds": float("nan")},
        {"request_timeout_seconds": float("inf")},
    ],
)
def test_invalid_gateway_config_fails_early(overrides: dict[str, Any]) -> None:
    with pytest.raises(ConfigError):
        gateway_config(**overrides).validate()


def test_gateway_yaml_expands_environment(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MESSAGE_GATEWAY_API_KEY", "yaml-key")
    with config.config_path.open("a") as f:
        f.write(
            "\nmessage_gateway:\n  enabled: true\n  url: http://message-gateway:8081\n  api_key: ${MESSAGE_GATEWAY_API_KEY}\n  account: qq\n"
        )
    loaded = AppConfig.load(config.config_path)
    assert loaded.message_gateway.enabled
    assert loaded.message_gateway.api_key == "yaml-key"
