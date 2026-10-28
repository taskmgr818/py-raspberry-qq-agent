from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import psycopg
from psycopg.rows import dict_row

from .keycloak import KeycloakDirectory

if TYPE_CHECKING:
    from .napcat import OutboundQueue

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class WeeklyReportConfig:
    enabled: bool = True
    schedule: dict[str, Any] = field(default_factory=dict)
    directory: str = "guild-reports"
    prompt: str = "总结任务进展、协作讨论、未完成事项和需要关注的问题。"
    upload_tool: str = ""


@dataclass(slots=True)
class GuildConfig:
    enabled: bool = False
    database_url: str = ""
    public_url: str = ""
    poll_seconds: float = 60
    due_enabled: bool = True
    advance_hours: float = 24
    assignments_enabled: bool = True
    weekly: WeeklyReportConfig = field(default_factory=WeeklyReportConfig)


class GuildRepository:
    """Only reads guild data; no work queues, snapshots or schema changes."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    async def query(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        async with await psycopg.AsyncConnection.connect(
            self._dsn,
            autocommit=True,
            row_factory=dict_row,
            connect_timeout=10,
            options="-c default_transaction_read_only=on -c statement_timeout=10000",
        ) as connection:
            cursor = await connection.execute(sql, params)
            return await cursor.fetchall()

    async def now(self) -> datetime:
        return (await self.query("SELECT clock_timestamp() AS now"))[0]["now"]

    async def due(
        self, start: datetime, end: datetime, advance: timedelta
    ) -> list[dict[str, Any]]:
        # Scan reminder trigger times, rather than the entire upcoming horizon.
        return await self.query(
            """
            SELECT n.id, n.title, n.due_at, m.subject
            FROM nodes n JOIN node_members m ON m.node_id=n.id AND m.role='leader'
            WHERE n.completed_at IS NULL AND n.due_at >= %s AND n.due_at < %s
              AND n.due_at > %s
            ORDER BY n.due_at, n.id, m.subject
        """,
            (start + advance, end + advance, end),
        )


class WindowPoller:
    """In-memory contiguous [start, end) windows; a failed query keeps its cursor."""

    def __init__(
        self,
        repository: GuildRepository,
        seconds: float,
        handler: Callable[[datetime, datetime], Awaitable[None]],
        name: str,
    ) -> None:
        self._repository = repository
        self._seconds = seconds
        self._handler = handler
        self._name = name
        self._cursor: datetime | None = None
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=self._name)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def tick(self) -> None:
        end = await self._repository.now()
        if self._cursor is None:
            self._cursor = end
            return
        if end <= self._cursor:
            return
        await self._handler(self._cursor, end)
        self._cursor = end

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                LOGGER.exception("guild poll failed name=%s", self._name)
            await asyncio.sleep(self._seconds)


async def notify(
    directory: KeycloakDirectory, outbound: OutboundQueue, subject: str, content: str
) -> None:
    try:
        user = await directory.user(subject)
        if user is None or not user.qq:
            LOGGER.warning(
                "notification skipped: no enabled QQ binding subject=%s", subject
            )
            return
        outbound.enqueue_notification("private", user.qq, content)
    except Exception:
        LOGGER.exception("notification failed subject=%s", subject)


class DueReminders:
    def __init__(
        self,
        config: GuildConfig,
        repository: GuildRepository,
        directory: KeycloakDirectory,
        outbound: OutboundQueue,
        timezone: Any,
    ) -> None:
        self._config = config
        self._repository = repository
        self._directory = directory
        self._outbound = outbound
        self._timezone = timezone

    async def check(self, start: datetime, end: datetime) -> None:
        rows = await self._repository.due(
            start, end, timedelta(hours=self._config.advance_hours)
        )
        for row in rows:
            deadline = (
                row["due_at"].astimezone(self._timezone).strftime("%Y-%m-%d %H:%M %Z")
            )
            content = f"任务即将到期：{row['title']}\n截止时间：{deadline}"
            if self._config.public_url:
                content += f"\n{self._config.public_url.rstrip('/')}/nodes/{row['id']}"
            await notify(self._directory, self._outbound, row["subject"], content)
