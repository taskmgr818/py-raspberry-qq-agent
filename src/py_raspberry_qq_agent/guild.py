from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import httpx
import psycopg
from bitnp_authz import SitePermissions
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .keycloak import AmbiguousQQBinding, KeycloakDirectory, User
from .tasks import AgentTask

if TYPE_CHECKING:
    from .napcat import OutboundQueue

LOGGER = logging.getLogger(__name__)


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


# Detail returns all direct memberships; lists only need leaders and a member count.
_NODE_MEMBERS = """
    COALESCE((
        SELECT jsonb_agg(jsonb_build_object('subject', m.subject, 'role', m.role)
                         ORDER BY m.role, m.subject)
        FROM node_members m WHERE m.node_id=n.id
    ), '[]'::jsonb) AS members
"""

_NODE_CONTEXT = """
    (
        WITH RECURSIVE ancestors AS (
            SELECT id, parent_id, title, 0 AS depth FROM nodes WHERE id=n.id
            UNION ALL
            SELECT p.id, p.parent_id, p.title, a.depth+1
            FROM nodes p JOIN ancestors a ON p.id=a.parent_id
        )
        SELECT jsonb_agg(jsonb_build_object('id', id, 'title', title) ORDER BY depth DESC)
        FROM ancestors
    ) AS path,
    (SELECT count(*) FROM nodes c WHERE c.parent_id=n.id) AS child_count
"""

_NODE_SUMMARY_MEMBERS = """
    COALESCE((
        SELECT jsonb_agg(jsonb_build_object('subject', m.subject) ORDER BY m.subject)
        FROM node_members m WHERE m.node_id=n.id AND m.role='leader'
    ), '[]'::jsonb) AS leaders,
    (SELECT count(*) FROM node_members m WHERE m.node_id=n.id AND m.role='member') AS member_count
"""


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
    """Guild SQL shared by interactive tools and background automation."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._initialized = False
        self._initialize_lock = asyncio.Lock()

    @asynccontextmanager
    async def _connection(self, *, read_only: bool = True):
        async with await psycopg.AsyncConnection.connect(
            self._dsn,
            row_factory=dict_row,
            connect_timeout=10,
            options=(
                f"-c default_transaction_read_only={'on' if read_only else 'off'} "
                "-c statement_timeout=10000 -c lock_timeout=5000"
            ),
        ) as connection:
            yield connection

    async def initialize(self) -> None:
        """Own only the Bot receipt table; guild-go still owns its migrations."""
        async with self._initialize_lock:
            if self._initialized:
                return
            async with self._connection(read_only=False) as connection:
                await connection.execute("SELECT pg_advisory_xact_lock(717113901)")
                await connection.execute("""
                CREATE TABLE IF NOT EXISTS qq_agent_guild_commands (
                    request_key TEXT PRIMARY KEY,
                    subject TEXT NOT NULL,
                    payload JSONB NOT NULL,
                    result JSONB NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """)
            self._initialized = True

    async def query(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        async with self._connection() as connection:
            cursor = await connection.execute(sql, params)
            return await cursor.fetchall()

    async def node(self, node_id: int) -> dict[str, Any]:
        rows = await self.query(
            f"SELECT n.*, {_NODE_MEMBERS}, {_NODE_CONTEXT} FROM nodes n WHERE n.id=%s",
            (node_id,),
        )
        if not rows:
            raise LookupError("guild 节点不存在")
        return rows[0]

    async def nodes(self, filters: dict[str, Any]) -> list[dict[str, Any]]:
        return await self.query(
            f"""
            WITH RECURSIVE direct AS (
                SELECT node_id, role FROM node_members WHERE subject=%(subject)s
            ), managed(id) AS (
                SELECT node_id FROM direct WHERE role='leader'
                UNION SELECT n.id FROM nodes n JOIN managed p ON n.parent_id=p.id
            ), participated(id) AS (
                SELECT node_id FROM direct
                UNION SELECT n.parent_id FROM nodes n JOIN participated p ON n.id=p.id
                      WHERE n.parent_id IS NOT NULL
            ), related AS (
                SELECT n.*, CASE WHEN %(include_inherited)s THEN
                    CASE WHEN n.id IN (SELECT id FROM managed) THEN 'leader'
                         WHEN n.id IN (SELECT id FROM participated) THEN 'member' END
                    ELSE (SELECT role FROM direct WHERE node_id=n.id) END AS relation
                FROM nodes n
            ), selected AS (
                SELECT n.* FROM related n
                WHERE (%(subject)s::text IS NULL OR
                    (%(relation)s='any' AND n.relation IS NOT NULL) OR n.relation=%(relation)s)
                  AND (%(status)s='all' OR
                    (%(status)s='open' AND n.completed_at IS NULL) OR
                    (%(status)s='completed' AND n.completed_at IS NOT NULL))
                  AND (NOT %(overdue)s OR
                       (n.completed_at IS NULL AND n.due_at < clock_timestamp()))
                  AND (NOT %(filter_parent)s OR n.parent_id IS NOT DISTINCT FROM %(parent_id)s::bigint)
                  AND (%(query)s='' OR strpos(lower(n.title), lower(%(query)s)) > 0)
                ORDER BY CASE WHEN %(sort)s='due' THEN n.due_at END ASC NULLS LAST,
                         CASE WHEN %(sort)s='position' THEN n.sort_order END, n.id
                LIMIT %(limit)s OFFSET %(offset)s
            )
            SELECT n.id, n.parent_id, n.title, n.due_at, n.completed_at, n.tags,
                   n.relation, {_NODE_CONTEXT}, {_NODE_SUMMARY_MEMBERS}
            FROM selected n
            ORDER BY CASE WHEN %(sort)s='due' THEN n.due_at END ASC NULLS LAST,
                     CASE WHEN %(sort)s='position' THEN n.sort_order END, n.id
            """,
            filters,
        )

    async def activity(self, filters: dict[str, Any]) -> list[dict[str, Any]]:
        return await self.query(
            """
            SELECT a.*, n.title FROM (
                SELECT 'node_created' AS kind, id, id AS node_id,
                       created_by AS author_subject, title AS body, created_at FROM nodes
                UNION ALL
                SELECT 'comment', id, node_id, author_subject, body, created_at FROM comments
                UNION ALL
                SELECT 'description_change', id, node_id, author_subject,
                       new_description, created_at FROM node_description_changes
            ) a JOIN nodes n ON n.id=a.node_id
            WHERE (%(node_id)s::bigint IS NULL OR a.node_id=%(node_id)s)
              AND (%(since)s::timestamptz IS NULL OR a.created_at >= %(since)s)
            ORDER BY a.created_at DESC, a.kind DESC, a.id DESC
            LIMIT %(limit)s OFFSET %(offset)s
            """,
            filters,
        )

    async def create_node(
        self,
        subject: str,
        permissions: SitePermissions,
        payload: dict[str, Any],
        request_key: str,
    ) -> dict[str, Any]:
        await self.initialize()
        async with self._connection(read_only=False) as connection:
            # Serialize retries of one message across processes and restarts.
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (request_key,)
            )
            receipt = await (
                await connection.execute(
                    "SELECT subject, payload, result FROM qq_agent_guild_commands WHERE request_key=%s",
                    (request_key,),
                )
            ).fetchone()
            if receipt:
                if receipt["subject"] != subject or receipt["payload"] != payload:
                    raise ValueError("同一消息已提交其他创建内容，请发送一条新消息")
                return {**receipt["result"], "replayed": True}

            parent_id = payload["parent_id"]
            if parent_id is None:
                if not permissions.manager:
                    raise PermissionError("只有 manager 可以创建根节点")
            else:
                parent = await (
                    await connection.execute(
                        "SELECT id FROM nodes WHERE id=%s FOR UPDATE", (parent_id,)
                    )
                ).fetchone()
                if not parent:
                    raise LookupError("guild 父节点不存在")
                if not permissions.system_admin:
                    allowed = await (
                        await connection.execute(
                            """
                        WITH RECURSIVE ancestors AS (
                            SELECT id, parent_id FROM nodes WHERE id=%s
                            UNION ALL
                            SELECT n.id, n.parent_id FROM nodes n
                            JOIN ancestors a ON n.id=a.parent_id
                        ) SELECT EXISTS (
                            SELECT 1 FROM node_members m JOIN ancestors a ON a.id=m.node_id
                            WHERE m.subject=%s AND m.role='leader'
                        ) AS allowed
                        """,
                            (parent_id, subject),
                        )
                    ).fetchone()
                    if not allowed["allowed"]:
                        raise PermissionError("只有父节点有效负责人可以创建子节点")

            subjects = {
                subject,
                *payload["leader_subjects"],
                *payload["member_subjects"],
            }
            for member in sorted(subjects):
                await connection.execute(
                    "INSERT INTO users(subject) VALUES (%s) ON CONFLICT(subject) DO NOTHING",
                    (member,),
                )
            row = await (
                await connection.execute(
                    """
                INSERT INTO nodes(parent_id, title, description, due_at, tags, sort_order, created_by)
                VALUES (%s,%s,%s,%s,%s,
                    COALESCE((SELECT max(sort_order)+1 FROM nodes
                              WHERE parent_id IS NOT DISTINCT FROM %s::bigint),0),%s)
                RETURNING *
                """,
                    (
                        parent_id,
                        payload["title"],
                        payload["description"],
                        payload["due_at"],
                        payload["tags"],
                        parent_id,
                        subject,
                    ),
                )
            ).fetchone()
            members = []
            for role, key in (
                ("leader", "leader_subjects"),
                ("member", "member_subjects"),
            ):
                for member in payload[key]:
                    await connection.execute(
                        "INSERT INTO node_members(node_id, subject, role) VALUES (%s,%s,%s)",
                        (row["id"], member, role),
                    )
                    members.append({"subject": member, "role": role})
            if payload["description"]:
                await connection.execute(
                    """INSERT INTO node_description_changes
                       (node_id,author_subject,old_description,new_description,created_at)
                       VALUES (%s,%s,'',%s,%s)""",
                    (row["id"], subject, payload["description"], row["created_at"]),
                )
            if parent_id is not None:
                await connection.execute(
                    "UPDATE nodes SET updated_at=now() WHERE id=%s", (parent_id,)
                )
            result = _jsonable({**row, "members": members})
            await connection.execute(
                "INSERT INTO qq_agent_guild_commands(request_key,subject,payload,result) VALUES (%s,%s,%s,%s)",
                (request_key, subject, Jsonb(payload), Jsonb(result)),
            )
            return {**result, "replayed": False}

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


class GuildService:
    """Validate message senders and expose bounded guild operations to the agent."""

    def __init__(
        self,
        config: GuildConfig,
        repository: GuildRepository,
        directory: KeycloakDirectory,
        timezone: ZoneInfo,
    ) -> None:
        self._config = config
        self._repository = repository
        self._directory = directory
        self._timezone = timezone

    async def _identity(
        self, *, refresh: bool = False, **identifier: str
    ) -> tuple[User, SitePermissions]:
        try:
            user = await self._directory.lookup_user(**identifier, refresh=refresh)
            if user is None:
                raise PermissionError(
                    "未找到有效的 Keycloak 成员身份，请先由管理员配置绑定"
                )
            roles = await self._directory.realm_roles(user.subject, refresh=refresh)
        except AmbiguousQQBinding:
            raise PermissionError("QQ 绑定冲突，请联系管理员处理") from None
        except httpx.HTTPError:
            raise RuntimeError("Keycloak 暂时不可用，请稍后重试") from None
        permissions = SitePermissions.from_roles(roles)
        if not permissions.admitted:
            raise PermissionError("该成员没有 guild 准入权限")
        return user, permissions

    async def _actor(
        self, task: AgentTask | None, *, write: bool = False
    ) -> tuple[User, SitePermissions]:
        if task is None or task.source != "incoming_message" or not task.sender_id:
            raise PermissionError("guild 工具需要当前 QQ 消息的真实发送者")
        return await self._identity(qq=task.sender_id, refresh=write)

    @staticmethod
    def _arguments(args: dict[str, Any], allowed: set[str]) -> None:
        if set(args) - allowed:
            raise ValueError(
                "未知的 guild 参数：" + ", ".join(sorted(set(args) - allowed))
            )

    @staticmethod
    def _integer(
        value: Any, name: str, minimum: int = 1, maximum: int | None = None
    ) -> int:
        if (
            type(value) is not int
            or value < minimum
            or (maximum is not None and value > maximum)
        ):
            raise ValueError(f"{name} 必须是范围内的整数")
        return value

    @staticmethod
    def _text(value: Any, name: str, maximum: int, *, required: bool = False) -> str:
        if (
            not isinstance(value, str)
            or len(value.strip()) > maximum
            or (required and not value.strip())
        ):
            raise ValueError(f"{name} 必须是有效字符串，长度不超过 {maximum}")
        return value.strip()

    def _time(self, value: Any) -> str | None:
        if value is None:
            return None
        parsed = datetime.fromisoformat(self._text(value, "时间", 64, required=True))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=self._timezone)
        return parsed.astimezone(UTC).isoformat()

    def _page(self, args: dict[str, Any]) -> tuple[int, int, dict[str, int]]:
        page = self._integer(args.get("page", 1), "page")
        size = self._integer(args.get("page_size", 20), "page_size", maximum=50)
        return page, size, {"limit": size + 1, "offset": (page - 1) * size}

    async def _present(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows = _jsonable(rows)
        names: dict[str, str] = {}
        for row in rows:
            subjects = [
                row[key] for key in ("created_by", "author_subject") if key in row
            ]
            members = row.get("members", []) + row.get("leaders", [])
            subjects += [member["subject"] for member in members]
            for subject in subjects:
                if subject not in names:
                    try:
                        user = await self._directory.lookup_user(subject=subject)
                        names[subject] = user.name if user else subject
                    except httpx.HTTPError:
                        names[subject] = subject
            for key, output in (
                ("created_by", "creator"),
                ("author_subject", "author"),
            ):
                if key in row:
                    row[output] = {"subject": row[key], "name": names[row[key]]}
            for member in members:
                member["name"] = names[member["subject"]]
            node_id = row.get("node_id", row["id"])
            if self._config.public_url:
                row["url"] = f"{self._config.public_url.rstrip('/')}/nodes/{node_id}"
        return rows

    async def get_node(
        self, args: dict[str, Any], task: AgentTask | None
    ) -> dict[str, Any]:
        self._arguments(args, {"node_id"})
        await self._actor(task)
        row = await self._repository.node(self._integer(args.get("node_id"), "node_id"))
        return (await self._present([row]))[0]

    async def list_nodes(
        self, args: dict[str, Any], task: AgentTask | None
    ) -> dict[str, Any]:
        self._arguments(
            args,
            {
                "scope",
                "subject",
                "relation",
                "status",
                "overdue",
                "include_inherited",
                "parent_id",
                "query",
                "sort",
                "page",
                "page_size",
            },
        )
        actor, _ = await self._actor(task)
        scope = args.get("scope", "mine")
        relation, status = args.get("relation", "any"), args.get("status", "open")
        if (
            scope not in ("mine", "user", "all")
            or relation not in ("any", "leader", "member")
            or status not in ("open", "completed", "all")
        ):
            raise ValueError("无效的 scope、relation 或 status")
        if ("subject" in args) != (scope == "user"):
            raise ValueError("只有 scope=user 时必须提供 subject")
        sort = args.get("sort", "due")
        if sort not in ("due", "position"):
            raise ValueError("无效的 sort")
        subject = actor.subject if scope == "mine" else None
        if scope == "user":
            subject = self._text(args["subject"], "subject", 255, required=True)
        for key in ("overdue", "include_inherited"):
            if key in args and type(args[key]) is not bool:
                raise ValueError(f"{key} 必须是布尔值")
        parent_id = args.get("parent_id")
        if parent_id is not None:
            self._integer(parent_id, "parent_id")
        page, size, pagination = self._page(args)
        rows = await self._repository.nodes(
            {
                **pagination,
                "subject": subject,
                "relation": relation,
                "status": status,
                "overdue": args.get("overdue", False),
                "include_inherited": args.get("include_inherited", False),
                "filter_parent": "parent_id" in args,
                "parent_id": parent_id,
                "query": self._text(args.get("query", ""), "query", 200),
                "sort": sort,
            }
        )
        return {
            "items": await self._present(rows[:size]),
            "page": page,
            "has_more": len(rows) > size,
            "next_page": page + 1 if len(rows) > size else None,
        }

    async def list_activity(
        self, args: dict[str, Any], task: AgentTask | None
    ) -> dict[str, Any]:
        self._arguments(args, {"node_id", "since", "page", "page_size"})
        await self._actor(task)
        node_id = args.get("node_id")
        if node_id is not None:
            self._integer(node_id, "node_id")
            await self._repository.node(node_id)
        page, size, pagination = self._page(args)
        rows = await self._repository.activity(
            {**pagination, "node_id": node_id, "since": self._time(args.get("since"))}
        )
        for row in rows:
            row["body"] = row["body"][:1000]
        return {
            "items": await self._present(rows[:size]),
            "page": page,
            "has_more": len(rows) > size,
            "next_page": page + 1 if len(rows) > size else None,
        }

    async def create_node(
        self, args: dict[str, Any], task: AgentTask | None
    ) -> dict[str, Any]:
        self._arguments(
            args,
            {
                "parent_id",
                "title",
                "description",
                "due_at",
                "tags",
                "leader_subjects",
                "member_subjects",
            },
        )
        actor, permissions = await self._actor(task, write=True)
        if "parent_id" not in args:
            raise ValueError("创建前请明确节点位置：根节点传 parent_id=null，子节点传已核实的父节点 ID")
        parent_id = args.get("parent_id")
        if parent_id is not None:
            self._integer(parent_id, "parent_id")
        payload = {
            "parent_id": parent_id,
            "title": self._text(args.get("title"), "title", 200, required=True),
            "description": self._text(
                args.get("description", ""), "description", 20000
            ),
            "due_at": self._time(args.get("due_at")),
        }
        for key, maximum, length in (
            ("tags", 10, 32),
            ("leader_subjects", 100, 255),
            ("member_subjects", 100, 255),
        ):
            values = args.get(key, [])
            if not isinstance(values, list) or len(values) > maximum:
                raise ValueError(f"{key} 必须是最多 {maximum} 项的数组")
            payload[key] = list(
                dict.fromkeys(
                    self._text(value, key, length, required=True) for value in values
                )
            )
        payload["leader_subjects"] = payload["leader_subjects"] or [actor.subject]
        if set(payload["leader_subjects"]) & set(payload["member_subjects"]):
            raise ValueError("负责人和成员不能重叠")
        for subject in sorted(
            {*payload["leader_subjects"], *payload["member_subjects"]} - {actor.subject}
        ):
            await self._identity(subject=subject, refresh=True)
        assert task is not None  # _actor has verified the trusted execution context.
        request_key = ":".join(
            (
                task.chat_type,
                task.target_id,
                str(task.metadata.get("message_id") or task.id),
            )
        )
        row = await self._repository.create_node(
            actor.subject, permissions, payload, request_key
        )
        return (await self._present([row]))[0]


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
        user = await directory.lookup_user(subject=subject)
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
