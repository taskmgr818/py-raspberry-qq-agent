from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from typing import Any

from .allowlist import ChatAllowlist
from .config import AppConfig
from .contacts import ContactDirectory, RoleStore
from .guild import guild_command
from .llm import LlmError, OpenAICompatibleClient, extract_json_object
from .messages import MessageStore
from .model_boundary import ModelIds

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class ImportanceDecision:
    important: bool
    reason: str
    method: str
    confidence: float = 1.0


class ImportanceGate:
    def __init__(
        self,
        config: AppConfig,
        classifier: OpenAICompatibleClient,
        allowlist: ChatAllowlist,
        roles: RoleStore,
        contacts: ContactDirectory,
        messages: MessageStore,
    ) -> None:
        self._ids = ModelIds()
        self._config = config
        self._classifier = classifier
        self._allowlist = allowlist
        self._roles = roles
        self._contacts = contacts
        self._messages = messages

    async def decide(
        self,
        *,
        chat_type: str,
        chat_id: str,
        sender_id: str,
        content: str,
        mentioned_bot: bool,
    ) -> ImportanceDecision:
        if not self._allowlist.allows(chat_type, chat_id):
            return ImportanceDecision(False, "聊天不在启用中的允许列表", "allowlist")
        if chat_type == "group" and not mentioned_bot:
            return ImportanceDecision(False, "群消息没有 @ 机器人", "routing")
        if chat_type == "private" and not self._config.private_enabled:
            return ImportanceDecision(False, "私聊自动回复已关闭", "routing")
        if chat_type == "group" and not self._config.group_mention_enabled:
            return ImportanceDecision(False, "群聊 @ 自动回复已关闭", "routing")

        if self._config.guild.enabled and guild_command(content):
            return ImportanceDecision(True, "明确的 guild 命令", "command")

        role = self._roles.role_of(sender_id)
        if role == "admin":
            return ImportanceDecision(True, "发送者具有 admin 权限", "role")

        profile = self._contacts.get(sender_id)
        recent = await self._messages.history(
            chat_type,
            chat_id,
            limit=self._config.classifier_context_messages,
        )
        compact_history = [
            {
                "sender_id": item.get("sender_id", ""),
                "name": item.get("sender_name", ""),
                "content": str(item.get("content", ""))[
                    : self._config.classifier_context_chars
                ],
            }
            for item in recent
        ]
        prompt = {
            "task": "判断刚收到的 QQ 消息是否值得机器人立刻处理",
            "important_definition": (
                "消息明确在向机器人提问、请求协助、安排协作、报告重要事项，"
                "或需要机器人参与有信息价值的讨论。寒暄、水群、纯表情、"
                "无明显需要的 @、重复内容、广告或无法行动的闲聊不重要。"
            ),
            "chat_type": chat_type,
            "chat_id": chat_id,
            "sender_id": sender_id,
            "role": role,
            "profile": {
                "display_name": profile.get("display_name", ""),
                "nickname": profile.get("nickname", ""),
                "notes": profile.get("notes", ""),
            },
            "message": content[: self._config.classifier_message_chars],
            "recent_context": compact_history,
            "output_schema": {
                "important": "boolean",
                "confidence": "number from 0 to 1",
                "reason": "short Chinese reason",
            },
        }
        messages = [
            {
                "role": "system",
                "content": (
                    "你是保守的 QQ 消息分流器。只输出一个 JSON 对象，"
                    "字段必须是 important、confidence、reason。"
                    "无法确定时输出 important=false。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(self._ids.encode(prompt), ensure_ascii=False),
            },
        ]
        try:
            reply = await self._classifier.complete(messages, force_json=True)
            parsed = extract_json_object(reply.content)
            important = _coerce_bool(parsed.get("important", False))
            confidence = float(parsed.get("confidence", 0))
            if not math.isfinite(confidence):
                confidence = 0.0
            confidence = min(1.0, max(0.0, confidence))
            reason = str(parsed.get("reason", ""))[:500]
            if important and confidence < self._config.classifier_min_confidence:
                return ImportanceDecision(
                    False,
                    f"分类置信度 {confidence:.2f} 低于阈值：{reason}",
                    "classifier",
                    confidence,
                )
            return ImportanceDecision(important, reason, "classifier", confidence)
        except (LlmError, ValueError, TypeError) as exc:
            LOGGER.warning(
                "importance classification failed sender=%s error=%s",
                sender_id,
                type(exc).__name__,
            )
            return ImportanceDecision(
                not self._config.classifier_fail_closed,
                "分类失败，按 fail-closed 配置处理",
                "classifier_error",
                0.0,
            )


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)
