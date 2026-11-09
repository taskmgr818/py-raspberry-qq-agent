from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from .reports import publish, summarize
from .scheduler import compute_next_run

if TYPE_CHECKING:
    from .config import AppConfig
    from .guild import GuildRepository
    from .keycloak import KeycloakDirectory
    from .knowledge import KnowledgeBase
    from .llm import OpenAICompatibleClient
    from .mcp_runtime import McpManager

LOGGER = logging.getLogger(__name__)
REPORT_TIMEZONE = ZoneInfo("Asia/Shanghai")


def previous_week(at: datetime) -> tuple[datetime, datetime]:
    local = at.astimezone(REPORT_TIMEZONE)
    end = local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
        days=local.weekday()
    )
    return end - timedelta(days=7), end


class WeeklyReports:
    def __init__(
        self,
        config: AppConfig,
        repository: GuildRepository,
        directory: KeycloakDirectory,
        llm: OpenAICompatibleClient,
        knowledge: KnowledgeBase,
        mcp: McpManager,
    ) -> None:
        self._config = config.guild.weekly
        self._repository = repository
        self._directory = directory
        self._llm = llm
        self._knowledge = knowledge
        self._mcp = mcp
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._config.enabled and self._config.schedule:
            self._task = asyncio.create_task(self._run(), name="guild-weekly")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        while True:
            now = datetime.now(REPORT_TIMEZONE)
            at = compute_next_run(self._config.schedule, now, REPORT_TIMEZONE)
            await asyncio.sleep((at - now).total_seconds())
            try:
                await self.generate(at)
            except Exception:
                LOGGER.exception(
                    "weekly report failed; waiting for next scheduled occurrence"
                )

    async def generate(self, at: datetime) -> str:
        start, end = previous_week(at)
        snapshot_at = await self._repository.now()
        nodes = await self._repository.query("""
            SELECT id,parent_id,title,description,due_at,completed_at,created_at
            FROM nodes ORDER BY id
        """)
        members = await self._repository.query(
            "SELECT node_id,subject,role FROM node_members ORDER BY node_id,subject"
        )
        comments = await self._repository.query(
            """
            SELECT c.node_id,n.title AS node_title,c.author_subject,c.body,c.created_at
            FROM comments c JOIN nodes n ON n.id=c.node_id
            WHERE c.created_at >= %s AND c.created_at < %s ORDER BY c.created_at,c.id
        """,
            (start, end),
        )
        names = {}
        for subject in sorted(
            {m["subject"] for m in members} | {c["author_subject"] for c in comments}
        ):
            try:
                user = await self._directory.lookup_user(subject=subject)
                names[subject] = user.name if user else subject
            except Exception:  # noqa: BLE001 - display lookup must not block the report
                LOGGER.warning("weekly report identity unavailable subject=%s", subject)
                names[subject] = subject
        for node in nodes:
            node["leaders"] = [
                names[m["subject"]]
                for m in members
                if m["node_id"] == node["id"] and m["role"] == "leader"
            ]
            node["members"] = [
                names[m["subject"]]
                for m in members
                if m["node_id"] == node["id"] and m["role"] == "member"
            ]
        for comment in comments:
            comment["author"] = names[comment.pop("author_subject")]
        created = [n for n in nodes if start <= n["created_at"] < end]
        completed = [
            n for n in nodes if n["completed_at"] and start <= n["completed_at"] < end
        ]
        open_nodes = [n for n in nodes if n["completed_at"] is None]
        overdue = [n for n in open_nodes if n["due_at"] and n["due_at"] < snapshot_at]
        data = {
            "period_start": start,
            "period_end_exclusive": end,
            "current_state_at": snapshot_at,
            "created_in_period": created,
            "completed_in_period": completed,
            "comments_in_period": comments,
            "currently_open": open_nodes,
        }
        title = f"看板周报 {start:%Y-%m-%d} 至 {end - timedelta(days=1):%Y-%m-%d}"
        summary = await summarize(
            self._llm,
            self._config.prompt
            + "\n统计上一个完整自然周，时区 UTC+8。当前未完成列表是生成时状态，不是历史快照。只依据提供的时间字段和评论总结。",
            data,
        )
        markdown = (
            f"# {title}\n\n"
            f"统计区间：[{start.isoformat()}, {end.isoformat()})\n\n"
            f"当前状态采集时间：{snapshot_at.astimezone(REPORT_TIMEZONE).isoformat()}\n\n"
            f"- 周内新增：{len(created)}\n- 周内完成：{len(completed)}\n"
            f"- 周内评论：{len(comments)}\n- 当前未完成：{len(open_nodes)}\n- 当前逾期：{len(overdue)}\n\n"
            + summary
            + "\n"
        )
        path = f"{self._config.directory.rstrip('/')}/{start:%Y-%m-%d}.md"
        await publish(
            self._knowledge,
            self._mcp,
            path=path,
            title=title,
            markdown=markdown,
            upload_tool=self._config.upload_tool,
        )
        LOGGER.info("weekly report saved path=%s", path)
        return path
