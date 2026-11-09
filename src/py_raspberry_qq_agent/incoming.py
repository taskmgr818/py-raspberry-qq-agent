from __future__ import annotations

import logging
from collections import deque

from .contacts import ContactDirectory
from .guild import guild_command
from .messages import MessageStore
from .napcat import IncomingMessage
from .priority import ImportanceGate
from .tasks import TaskQueue

LOGGER = logging.getLogger(__name__)


class IncomingService:
    def __init__(
        self,
        *,
        contacts: ContactDirectory,
        messages: MessageStore,
        gate: ImportanceGate,
        tasks: TaskQueue,
        dedupe_messages: int = 2000,
    ) -> None:
        self._contacts = contacts
        self._messages = messages
        self._gate = gate
        self._tasks = tasks
        self._seen_ids: deque[str] = deque(maxlen=dedupe_messages)
        self._seen_set: set[str] = set()

    async def handle(self, message: IncomingMessage) -> None:
        chat_id = message.raw_chat_id
        seen_key = f"{message.chat_type}:{chat_id}:{message.message_id}"
        if message.message_id and seen_key in self._seen_set:
            return
        if message.message_id:
            if len(self._seen_ids) == self._seen_ids.maxlen:
                self._seen_set.discard(self._seen_ids[0])
            self._seen_ids.append(seen_key)
            self._seen_set.add(seen_key)

        sender_id = message.raw_sender_id
        self._contacts.observe_nickname(sender_id, message.sender_name)
        await self._messages.append(
            chat_type=message.chat_type,
            chat_id=chat_id,
            sender_id=sender_id,
            sender_name=message.sender_name,
            content=message.content,
            timestamp=message.timestamp,
            message_id=message.message_id,
            mentioned_bot=message.mentioned_bot,
            mentions=message.mentions,
        )

        decision = await self._gate.decide(
            chat_type=message.chat_type,
            chat_id=chat_id,
            sender_id=sender_id,
            content=message.content,
            mentioned_bot=message.mentioned_bot,
        )
        if not decision.important:
            LOGGER.info(
                "message ignored chat=%s target=%s sender=%s method=%s",
                message.chat_type,
                chat_id,
                sender_id,
                decision.method,
            )
            return

        label = "群聊 @" if message.chat_type == "group" else "私聊"
        item = await self._tasks.add(
            content=(
                f"处理一条重要{label}消息。请结合最近上下文判断是否需要回复；"
                "如需回复，使用 send_qq_message。"
            ),
            source="incoming_message",
            context=(
                f"消息：{message.content[:8000]}\n重要性依据：{decision.reason}\n"
            ),
            chat_type=message.chat_type,
            target_id=chat_id,
            sender_id=sender_id,
            metadata={
                "kind": "incoming_message",
                "message_id": message.message_id,
                "importance_method": decision.method,
                "importance_reason": decision.reason,
                "confidence": decision.confidence,
                "mentions": message.mentions,
                "guild_command": guild_command(message.content),
            },
        )
        LOGGER.info(
            "message queued task=%s chat=%s target=%s method=%s",
            item.id,
            message.chat_type,
            chat_id,
            decision.method,
        )
