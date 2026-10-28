from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from websockets.asyncio.client import ClientConnection, connect

from .allowlist import ChatAllowlist
from .config import AppConfig
from .messages import MessageStore

LOGGER = logging.getLogger(__name__)


class NapCatError(RuntimeError):
    pass


@dataclass(slots=True)
class IncomingMessage:
    chat_type: str
    raw_chat_id: str
    raw_sender_id: str
    sender_name: str
    content: str
    message_id: str
    timestamp: float
    mentioned_bot: bool


class NapCatClient:
    def __init__(
        self,
        config: AppConfig,
        on_message: Callable[[IncomingMessage], Awaitable[None]],
    ) -> None:
        self._config = config
        self._on_message = on_message
        self._ws: ClientConnection | None = None
        self._connected = asyncio.Event()
        self._stop = asyncio.Event()
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._write_lock = asyncio.Lock()
        self._runner: asyncio.Task[None] | None = None
        self._message_tasks: set[asyncio.Task[None]] = set()
        self._self_id = ""
        self._friend_ids: set[str] = set()
        self._friend_ids_expires_at = 0.0
        self._group_ids: set[str] = set()
        self._group_ids_expires_at = 0.0
        self._group_members: dict[str, tuple[float, set[str]]] = {}
        self._discovery_lock = asyncio.Lock()
        self._cache_generation = 0

    def start(self) -> None:
        if self._runner is None:
            self._runner = asyncio.create_task(self._run(), name="napcat-client")

    async def stop(self) -> None:
        self._stop.set()
        if self._ws:
            await self._ws.close()
        for task in list(self._message_tasks):
            task.cancel()
        if self._message_tasks:
            await asyncio.gather(*self._message_tasks, return_exceptions=True)
        if self._runner:
            await self._runner
            self._runner = None
        self._reset_caches()

    def connected(self) -> bool:
        return self._ws is not None and not self._ws.close_code

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                headers = {}
                if self._config.napcat_access_token:
                    headers["Authorization"] = (
                        f"Bearer {self._config.napcat_access_token}"
                    )
                async with connect(
                    self._config.napcat_ws_url,
                    additional_headers=headers,
                    ping_interval=20,
                    ping_timeout=20,
                    open_timeout=self._config.napcat_request_timeout_seconds,
                    max_size=8 * 1024 * 1024,
                ) as websocket:
                    self._ws = websocket
                    self._connected.set()
                    LOGGER.info("connected to NapCat at %s", self._config.napcat_ws_url)
                    async for raw in websocket:
                        await self._handle_raw(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect boundary
                LOGGER.warning(
                    "NapCat connection lost or unavailable: %s", type(exc).__name__
                )
            finally:
                self._ws = None
                self._connected.clear()
                self._reset_caches()
                for future in self._pending.values():
                    if not future.done():
                        future.set_exception(NapCatError("NapCat disconnected"))
                self._pending.clear()
            if not self._stop.is_set():
                try:
                    await asyncio.wait_for(
                        self._stop.wait(),
                        timeout=self._config.napcat_reconnect_seconds,
                    )
                except TimeoutError:
                    pass

    async def _handle_raw(self, raw: str | bytes) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            LOGGER.warning("ignored malformed NapCat WebSocket payload")
            return
        if not isinstance(payload, dict):
            return

        echo = payload.get("echo")
        if echo:
            future = self._pending.pop(str(echo), None)
            if future and not future.done():
                future.set_result(payload)
            return

        self_id = str(payload.get("self_id") or "")
        if self_id:
            if self._self_id and self_id != self._self_id:
                self._reset_caches()
            self._self_id = self_id
        if payload.get("post_type") != "message":
            return
        message = self._parse_message(payload)
        if message is None:
            return
        task = asyncio.create_task(
            self._process_incoming(message),
            name="incoming-message",
        )
        self._message_tasks.add(task)
        task.add_done_callback(self._message_tasks.discard)

    async def _process_incoming(self, message: IncomingMessage) -> None:
        try:
            await self._on_message(message)
        except Exception:
            LOGGER.exception(
                "incoming message handler failed chat=%s sender=%s",
                self._alias_for_log(message.raw_chat_id),
                self._alias_for_log(message.raw_sender_id),
            )

    def _parse_message(self, payload: dict[str, Any]) -> IncomingMessage | None:
        self_id = str(payload.get("self_id", ""))
        if self_id:
            self._self_id = self_id
        raw_sender = str(payload.get("user_id", ""))
        if not raw_sender or raw_sender == self_id:
            return None
        message_type = str(payload.get("message_type", ""))
        if message_type not in {"private", "group"}:
            return None

        text_parts: list[str] = []
        mentioned = False
        message = payload.get("message")
        if isinstance(message, str):
            if self_id and re.search(
                rf"\[CQ:at,qq={re.escape(self_id)}\]", message
            ):
                mentioned = True
            text_parts.append(_strip_cq_codes(message))
        elif isinstance(message, list):
            for segment in message:
                if not isinstance(segment, dict):
                    continue
                segment_type = str(segment.get("type", ""))
                data = segment.get("data") or {}
                if segment_type == "text":
                    text_parts.append(str(data.get("text", "")))
                elif (
                    segment_type == "at"
                    and self_id
                    and str(data.get("qq", "")) == self_id
                ):
                    mentioned = True
        if not text_parts and isinstance(payload.get("raw_message"), str):
            text_parts.append(_strip_cq_codes(payload["raw_message"]))
        content = "".join(text_parts).strip()
        if not content:
            return None

        raw_chat = (
            str(payload.get("group_id", ""))
            if message_type == "group"
            else raw_sender
        )
        if not raw_chat:
            return None
        sender = payload.get("sender") or {}
        timestamp = float(payload.get("time") or time.time())
        return IncomingMessage(
            chat_type=message_type,
            raw_chat_id=raw_chat,
            raw_sender_id=raw_sender,
            sender_name=str(sender.get("card") or sender.get("nickname") or ""),
            content=content,
            message_id=str(payload.get("message_id", "")),
            timestamp=timestamp,
            mentioned_bot=mentioned,
        )

    async def call_action(
        self,
        action: str,
        params: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        try:
            await asyncio.wait_for(
                self._connected.wait(),
                timeout=timeout or self._config.napcat_request_timeout_seconds,
            )
        except TimeoutError as exc:
            raise NapCatError("NapCat is not connected") from exc
        websocket = self._ws
        if websocket is None:
            raise NapCatError("NapCat is not connected")

        echo = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[echo] = future
        request = {"action": action, "params": params, "echo": echo}
        try:
            async with self._write_lock:
                await websocket.send(json.dumps(request, ensure_ascii=False))
            response = await asyncio.wait_for(
                future,
                timeout=timeout or self._config.napcat_request_timeout_seconds,
            )
        finally:
            self._pending.pop(echo, None)

        retcode = response.get("retcode", 0)
        if retcode not in {0, None} or response.get("status") == "failed":
            raise NapCatError(
                f"NapCat action {action} failed retcode={retcode} "
                f"message={response.get('message') or response.get('wording') or ''}"
            )
        data = response.get("data")
        return data if isinstance(data, dict) else {"data": data}

    async def send_private(self, raw_user_id: str, content: str) -> dict[str, Any]:
        params: dict[str, Any] = {"user_id": int(raw_user_id), "message": content}
        if self._config.napcat_allow_temp_session:
            # Discovery has its own budget; delivery keeps the configured send timeout.
            async with asyncio.timeout(self._config.napcat_request_timeout_seconds):
                async with self._discovery_lock:
                    generation = self._cache_generation
                    if time.monotonic() >= self._friend_ids_expires_at:
                        data = await self.call_action("get_friend_list", {})
                        friends = _parse_id_set(data, "friends", "user_id")
                        self._check_cache_generation(generation)
                        self._friend_ids = friends
                        self._friend_ids_expires_at = (
                            time.monotonic() + self._config.napcat_cache_ttl_seconds
                        )
                    if raw_user_id not in self._friend_ids:
                        group_id = await self._find_temp_group(raw_user_id, generation)
                        # NapCat can fall back to a group send for an unresolved user.
                        user = await self.call_action(
                            "get_stranger_info", {"user_id": int(raw_user_id)}
                        )
                        if (
                            str(user.get("user_id", "")) != raw_user_id
                            or not user.get("uid")
                        ):
                            raise NapCatError(
                                f"Cannot resolve user for temporary session target={raw_user_id}"
                            )
                        params["group_id"] = int(group_id)
                    self._check_cache_generation(generation)
        LOGGER.info(
            "QQ private send route target=%s mode=%s source_group=%s",
            raw_user_id,
            "temporary" if "group_id" in params else "private",
            params.get("group_id", "-"),
        )
        # Never retry an uncertain send, including through another group.
        return await self.call_action(
            "send_private_msg",
            params,
            timeout=self._config.outbound_send_timeout_seconds,
        )

    def _reset_caches(self) -> None:
        self._cache_generation += 1
        self._friend_ids.clear()
        self._friend_ids_expires_at = 0.0
        self._group_ids.clear()
        self._group_ids_expires_at = 0.0
        self._group_members.clear()

    def _check_cache_generation(self, generation: int) -> None:
        if generation != self._cache_generation:
            raise NapCatError("NapCat connection or account changed during discovery")

    async def _find_temp_group(self, raw_user_id: str, generation: int) -> str:
        # Called under _discovery_lock, so overlapping callers share completed loads.
        if time.monotonic() >= self._group_ids_expires_at:
            data = await self.call_action("get_group_list", {})
            groups = _parse_id_set(data, "groups", "group_id")
            self._check_cache_generation(generation)
            self._group_ids = groups
            self._group_ids_expires_at = (
                time.monotonic() + self._config.napcat_cache_ttl_seconds
            )
        now = time.monotonic()
        self._group_members = {
            group_id: snapshot
            for group_id, snapshot in self._group_members.items()
            if group_id in self._group_ids and now < snapshot[0]
        }
        incomplete = False

        async def is_member(group_id: str) -> bool:
            nonlocal incomplete
            try:
                member = await self.call_action(
                    "get_group_member_info",
                    {
                        "group_id": int(group_id),
                        "user_id": int(raw_user_id),
                        "no_cache": True,
                    },
                )
            except NapCatError:
                self._check_cache_generation(generation)
                incomplete = True
                return False
            self._check_cache_generation(generation)
            return (
                str(member.get("user_id", "")) == raw_user_id
                and str(member.get("group_id", group_id)) == group_id
            )

        for group_id, (_, members) in list(self._group_members.items()):
            if raw_user_id in members and await is_member(group_id):
                return group_id
        for group_id in sorted(self._group_ids, key=int):
            if group_id in self._group_members:
                continue
            try:
                data = await self.call_action(
                    "get_group_member_list",
                    {"group_id": int(group_id), "no_cache": True},
                    timeout=min(8, self._config.napcat_request_timeout_seconds),
                )
                members = _parse_id_set(data, "members", "user_id", group_id=group_id)
            except (NapCatError, TimeoutError):
                self._check_cache_generation(generation)
                incomplete = True
                continue
            self._check_cache_generation(generation)
            self._group_members[group_id] = (
                time.monotonic() + self._config.napcat_cache_ttl_seconds,
                members,
            )
            if raw_user_id in members and await is_member(group_id):
                return group_id
        self._check_cache_generation(generation)
        if incomplete:
            raise NapCatError(f"Common group lookup incomplete target={raw_user_id}")
        raise NapCatError(
            f"No verified common group for temporary session target={raw_user_id}"
        )

    async def send_group(self, raw_group_id: str, content: str) -> dict[str, Any]:
        return await self.call_action(
            "send_group_msg",
            {"group_id": int(raw_group_id), "message": content},
            timeout=self._config.outbound_send_timeout_seconds,
        )

    async def fetch_history(
        self,
        chat_type: str,
        raw_chat_id: str,
        count: int,
    ) -> list[dict[str, Any]]:
        if chat_type == "group":
            data = await self.call_action(
                "get_group_msg_history",
                {"group_id": int(raw_chat_id), "count": count},
            )
        else:
            data = await self.call_action(
                "get_friend_msg_history",
                {"user_id": int(raw_chat_id), "count": count},
            )
        if isinstance(data, dict):
            raw_messages = data.get("messages", [])
        elif isinstance(data, list):
            raw_messages = data
        else:
            raw_messages = []
        return raw_messages if isinstance(raw_messages, list) else []

    async def fetch_friend_list(self) -> list[dict[str, Any]]:
        data = await self.call_action("get_friend_list", {})
        return _extract_list_data(data, "friends")

    async def fetch_group_list(self) -> list[dict[str, Any]]:
        data = await self.call_action("get_group_list", {})
        return _extract_list_data(data, "groups")

    def _alias_for_log(self, raw_id: str) -> str:
        if not raw_id:
            return "-"
        digest = uuid.uuid5(uuid.NAMESPACE_URL, raw_id).hex[:8]
        return f"contact-{digest}"


class OutboundQueue:
    def __init__(
        self,
        config: AppConfig,
        napcat: NapCatClient,
        allowlist: ChatAllowlist,
        messages: MessageStore | None = None,
    ) -> None:
        self._config = config
        self._napcat = napcat
        self._allowlist = allowlist
        self._messages = messages
        self._queue: asyncio.Queue[tuple[str, str, str, bool]] = asyncio.Queue(
            maxsize=config.outbound_queue_size
        )
        self._worker: asyncio.Task[None] | None = None
        self._last_sent_at: float | None = None

    def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._run(), name="outbound-queue")

    async def stop(self) -> None:
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None

    def enqueue(self, chat_type: str, target_id: str, content: str) -> None:
        self._enqueue(chat_type, target_id, content, enforce_allowlist=True)

    def enqueue_notification(self, chat_type: str, target_id: str, content: str) -> None:
        """Queue a program-selected business notification, never a model action."""
        self._enqueue(chat_type, target_id, content, enforce_allowlist=False)

    def _enqueue(
        self,
        chat_type: str,
        target_id: str,
        content: str,
        *,
        enforce_allowlist: bool,
    ) -> None:
        if chat_type not in {"private", "group"}:
            raise ValueError("chat_type must be private or group")
        if not target_id.isdecimal():
            raise ValueError("target_id must be a QQ number")
        if enforce_allowlist and not self._allowlist.allows(chat_type, target_id):
            raise PermissionError("chat is not in the enabled allowlist")
        if not content.strip():
            raise ValueError("message content cannot be empty")
        if self._queue.full():
            raise RuntimeError("outbound message queue is full")
        self._queue.put_nowait((chat_type, target_id, content, enforce_allowlist))

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            chat_type, target_id, content, enforce_allowlist = await self._queue.get()
            try:
                if self._last_sent_at is not None:
                    interval = random.uniform(
                        self._config.outbound_min_interval_seconds,
                        self._config.outbound_max_interval_seconds,
                    )
                    wait_for = self._last_sent_at + interval - loop.time()
                    if wait_for > 0:
                        await asyncio.sleep(wait_for)
                if enforce_allowlist and not self._allowlist.allows(chat_type, target_id):
                    raise PermissionError("chat is not in the enabled allowlist")
                if chat_type == "group":
                    response = await self._napcat.send_group(target_id, content)
                else:
                    response = await self._napcat.send_private(target_id, content)
                self._last_sent_at = loop.time()
                if self._messages is not None:
                    await self._messages.append(
                        chat_type=chat_type,
                        chat_id=target_id,
                        sender_id="qq-bot:self",
                        sender_name="self",
                        content=content,
                        timestamp=time.time(),
                        message_id=_response_message_id(response),
                    )
                LOGGER.info(
                    "QQ message sent chat=%s target=%s length=%d",
                    chat_type,
                    target_id,
                    len(content),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - isolate one delivery
                LOGGER.error(
                    "QQ message send failed chat=%s target=%s error=%s detail=%s",
                    chat_type,
                    target_id,
                    type(exc).__name__,
                    str(exc)[:500],
                )
            finally:
                self._queue.task_done()


def _strip_cq_codes(value: str) -> str:
    """Remove OneBot CQ payloads so raw targets cannot leak into message text."""
    return re.sub(r"\[CQ:[^\]]+\]", "", value)


def _response_message_id(response: dict[str, Any]) -> str:
    value = response.get("message_id", "")
    return str(value) if value not in {None, ""} else ""


def _parse_id_set(
    data: Any, label: str, field: str, *, group_id: str | None = None
) -> set[str]:
    # Authoritative cache snapshots must not turn missing or malformed data into [].
    rows = data.get("data", data.get(label)) if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise NapCatError(f"Invalid NapCat {label} list response")
    ids: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise NapCatError(f"Invalid NapCat {label} list entry")
        raw_id = str(row.get(field, ""))
        if not raw_id.isdecimal() or int(raw_id) <= 0:
            raise NapCatError(f"Invalid NapCat {label} {field}")
        if group_id is not None and str(row.get("group_id", group_id)) != group_id:
            raise NapCatError("NapCat member list returned a different group")
        ids.add(raw_id)
    return ids


def _extract_list_data(data: Any, label: str) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("data", label):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    raise NapCatError(f"NapCat returned invalid {label} list")
