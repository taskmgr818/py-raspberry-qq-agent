from __future__ import annotations


class ChatAllowlist:
    """Hard allowlist for chat triggers and model-initiated QQ messages."""

    def __init__(
        self,
        *,
        enabled: bool,
        private_qq_ids: list[str],
        group_ids: list[str],
    ) -> None:
        self._enabled = enabled
        self._private_ids = (
            set(private_qq_ids)
            if enabled
            else set()
        )
        self._group_ids = (
            set(group_ids)
            if enabled
            else set()
        )

    @property
    def enabled(self) -> bool:
        return self._enabled

    def allows(self, chat_type: str, chat_id: str) -> bool:
        if not self._enabled:
            return True
        if chat_type == "private":
            return chat_id in self._private_ids
        if chat_type == "group":
            return chat_id in self._group_ids
        return False
