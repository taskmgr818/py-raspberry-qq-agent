from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import quote

import httpx

from .config import MessageGatewayConfig

LOGGER = logging.getLogger(__name__)


class MessageGatewayError(RuntimeError):
    pass


class MessageGatewayClient:
    """Submit once, then wait for the gateway's final delivery receipt."""

    manages_pacing = True

    def __init__(
        self,
        config: MessageGatewayConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        config.validate()
        self._config = config
        self._http = httpx.AsyncClient(
            base_url=config.url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {config.api_key}"},
            timeout=config.request_timeout_seconds,
            transport=transport,
            trust_env=False,
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def send_private(self, raw_user_id: str, content: str) -> dict[str, Any]:
        return await self._send("user", raw_user_id, content)

    async def send_group(self, raw_group_id: str, content: str) -> dict[str, Any]:
        return await self._send("group", raw_group_id, content)

    async def _send(self, kind: str, target_id: str, content: str) -> dict[str, Any]:
        delivery_id = "unknown"
        try:
            async with asyncio.timeout(self._config.delivery_timeout_seconds):
                # No retries: the gateway has no idempotency key. All delivery
                # retries and pacing belong to its shared per-account queue.
                response = await self._http.post(
                    "api/v1/messages",
                    json={
                        "platform": self._config.account,
                        "target": {"kind": kind, "id": target_id},
                        "text": content,
                    },
                )
                response.raise_for_status()
                queued = self._object(response)
                if (
                    response.status_code != 202
                    or queued.get("status") != "queued"
                    or not isinstance(queued.get("id"), str)
                    or not queued["id"]
                ):
                    raise MessageGatewayError(
                        "invalid gateway enqueue response; not resubmitting"
                    )
                delivery_id = queued["id"]
                LOGGER.info(
                    "gateway message queued id=%s account=%s",
                    delivery_id,
                    self._config.account,
                )
                while True:
                    response = await self._http.get(
                        "api/v1/messages/" + quote(delivery_id, safe="")
                    )
                    response.raise_for_status()
                    delivery = self._object(response)
                    if delivery.get("id") != delivery_id:
                        raise MessageGatewayError(
                            "gateway returned a different delivery id"
                        )
                    status = delivery.get("status")
                    if status == "sent":
                        receipt = delivery.get("receipt")
                        if not isinstance(receipt, dict) or receipt.get(
                            "message_id"
                        ) in {None, ""}:
                            raise MessageGatewayError(
                                f"gateway delivery {delivery_id} has no receipt"
                            )
                        LOGGER.info("gateway message sent id=%s", delivery_id)
                        return {"message_id": str(receipt["message_id"])}
                    if status == "failed":
                        raise MessageGatewayError(
                            f"gateway delivery {delivery_id} failed: {delivery.get('error', 'unknown error')}"
                        )
                    if status not in {"queued", "retrying"}:
                        raise MessageGatewayError(
                            f"unknown gateway delivery state {status!r}"
                        )
                    await asyncio.sleep(self._config.poll_interval_seconds)
        except TimeoutError as exc:
            raise MessageGatewayError(
                f"gateway delivery {delivery_id} timed out; outcome unknown, not resubmitting"
            ) from exc

    @staticmethod
    def _object(response: httpx.Response) -> dict[str, Any]:
        try:
            value = response.json()
        except ValueError as exc:
            raise MessageGatewayError("gateway returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise MessageGatewayError("gateway response must be an object")
        return value
