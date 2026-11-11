from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from bitnp_authz import positions_from_roles

from .config import AppConfig
from .contacts import ContactDirectory
from .guild import GuildService
from .keycloak import AmbiguousQQBinding, KeycloakDirectory
from .knowledge import KnowledgeBase
from .mcp_runtime import McpManager
from .messages import MessageStore
from .model_boundary import ModelIds
from .napcat import NapCatClient, OutboundQueue, parse_message_content
from .scheduler import ScheduleStore
from .tasks import AgentTask

LOGGER = logging.getLogger(__name__)
ToolHandler = Callable[..., Awaitable[Any]]


@dataclass(slots=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler
    contact_refs: bool = False
    task_context: bool = False

    def openai_definition(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self.ids = ModelIds()
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool

    def definitions(self) -> list[dict[str, Any]]:
        return [tool.openai_definition() for tool in self._tools.values()]

    async def execute(
        self, name: str, arguments: dict[str, Any], *, task: AgentTask | None = None
    ) -> Any:
        tool = self._tools.get(name)
        if tool is None:
            raise KeyError(f"unknown tool: {name}")
        args = self.ids.decode(arguments) if tool.contact_refs else arguments
        result = (
            await tool.handler(args, task)
            if tool.task_context
            else await tool.handler(args)
        )
        return self.ids.encode(result) if tool.contact_refs else result


class HistoryService:
    def __init__(
        self,
        config: AppConfig,
        messages: MessageStore,
        napcat: NapCatClient,
    ) -> None:
        self._config = config
        self._messages = messages
        self._napcat = napcat

    async def get(
        self,
        chat_type: str,
        target_id: str,
        *,
        limit: int,
        start_time: float | None,
        end_time: float | None,
    ) -> list[dict[str, Any]]:
        selected = await self._messages.history(
            chat_type,
            target_id,
            limit=limit,
            start_time=start_time,
            end_time=end_time,
        )
        try:
            remote = await self._napcat.fetch_history(chat_type, target_id, limit)
            selected = self._merge(
                selected,
                self._sanitize_remote(remote, chat_type, target_id),
                start_time,
                end_time,
                limit,
            )
        except Exception as exc:  # noqa: BLE001 - local history remains usable
            LOGGER.warning(
                "NapCat history unavailable chat=%s target=%s error=%s",
                chat_type,
                target_id,
                type(exc).__name__,
            )

        output = []
        for item in selected:
            output.append(
                {
                    "timestamp": datetime.fromtimestamp(
                        float(item.get("timestamp", 0)),
                        self._timezone(),
                    ).isoformat(),
                    "sender_id": item.get("sender_id", ""),
                    "sender_name": item.get("sender_name", ""),
                    "content": item.get("content", ""),
                    "message_id": item.get("message_id", ""),
                    "mentioned_bot": bool(item.get("mentioned_bot", False)),
                    "mentions": item.get("mentions", []),
                }
            )
        return output

    def _timezone(self) -> ZoneInfo:
        return ZoneInfo(self._config.timezone)

    def _sanitize_remote(
        self,
        messages: list[dict[str, Any]],
        chat_type: str,
        chat_id: str,
    ) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for item in messages:
            if not isinstance(item, dict):
                continue
            raw_sender = str(item.get("user_id", ""))
            if not raw_sender:
                continue
            sender = item.get("sender") or {}
            content, mentions, mentioned_bot = parse_message_content(
                item.get("message"), self._napcat.self_id
            )
            output.append(
                {
                    "chat_type": chat_type,
                    "chat_id": chat_id,
                    "sender_id": raw_sender,
                    "sender_name": str(
                        sender.get("card") or sender.get("nickname") or ""
                    ),
                    "content": content[: self._config.history_content_chars],
                    "mentions": mentions,
                    "timestamp": float(item.get("time", 0)),
                    "message_id": str(
                        item.get("message_id") or item.get("message_seq") or ""
                    ),
                    "mentioned_bot": mentioned_bot,
                }
            )
        return output

    @staticmethod
    def _merge(
        local: list[dict[str, Any]],
        remote: list[dict[str, Any]],
        start_time: float | None,
        end_time: float | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        by_key: dict[str, dict[str, Any]] = {}
        for item in [*local, *remote]:
            timestamp = float(item.get("timestamp", 0))
            if start_time is not None and timestamp < start_time:
                continue
            if end_time is not None and timestamp >= end_time:
                continue
            key = str(
                item.get("message_id")
                or (
                    f"{timestamp}:{item.get('sender_id', '')}:{item.get('content', '')}"
                )
            )
            by_key[key] = item
        return sorted(
            by_key.values(), key=lambda item: float(item.get("timestamp", 0))
        )[-limit:]


class ChatDirectoryService:
    """Read the bot's friends and joined groups using plain QQ IDs internally."""

    def __init__(
        self,
        config: AppConfig,
        napcat: NapCatClient,
    ) -> None:
        self._config = config
        self._napcat = napcat

    async def friends(
        self,
        page: int = 1,
        page_size: int | None = None,
    ) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for friend in await self._napcat.fetch_friend_list():
            raw_id = friend.get("user_id")
            if raw_id in {None, ""}:
                continue
            nickname = str(friend.get("nickname") or "")
            remark = str(friend.get("remark") or "")
            items.append(
                {
                    "target_id": str(raw_id),
                    "chat_type": "private",
                    "nickname": nickname,
                    "remark": remark,
                    "display_name": remark or nickname,
                }
            )
        items.sort(
            key=lambda item: (
                str(item["display_name"]).casefold(),
                str(item["target_id"]),
            )
        )
        return self._page("friend", items, page, page_size)

    async def groups(
        self,
        page: int = 1,
        page_size: int | None = None,
    ) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for group in await self._napcat.fetch_group_list():
            raw_id = group.get("group_id")
            if raw_id in {None, ""}:
                continue
            items.append(
                {
                    "target_id": str(raw_id),
                    "chat_type": "group",
                    "group_name": str(group.get("group_name") or ""),
                    "group_memo": str(group.get("group_memo") or ""),
                    "member_count": _optional_int(group.get("member_count")),
                    "max_member_count": _optional_int(group.get("max_member_count")),
                }
            )
        items.sort(
            key=lambda item: (
                str(item["group_name"]).casefold(),
                str(item["target_id"]),
            )
        )
        return self._page("group", items, page, page_size)

    def _page(
        self,
        kind: str,
        items: list[dict[str, Any]],
        page: int,
        page_size: int | None,
    ) -> dict[str, Any]:
        if page < 1:
            raise ValueError("page must be at least 1")
        size = page_size or self._config.directory_default_page_size
        if size < 1:
            raise ValueError("page_size must be at least 1")
        size = min(size, self._config.directory_max_page_size)
        total = len(items)
        total_pages = (total + size - 1) // size if total else 0
        start = (page - 1) * size
        end = start + size
        return {
            "kind": kind,
            "page": page,
            "page_size": size,
            "total": total,
            "total_pages": total_pages,
            "has_more": end < total,
            "items": items[start:end],
        }


def build_tool_registry(
    *,
    config: AppConfig,
    schedules: ScheduleStore,
    knowledge: KnowledgeBase,
    contacts: ContactDirectory,
    outbound: OutboundQueue,
    history: HistoryService,
    chat_directory: ChatDirectoryService,
    mcp: McpManager,
    identities: KeycloakDirectory | None = None,
    guild_service: GuildService | None = None,
) -> ToolRegistry:
    registry = ToolRegistry()

    async def send_qq_message(args: dict[str, Any]) -> dict[str, Any]:
        outbound.enqueue(
            _required_str(args, "chat_type"),
            _required_str(args, "target_id"),
            _required_str(args, "content"),
        )
        return {"ok": True, "queued": True}

    registry.register(
        Tool(
            name="send_qq_message",
            description=(
                "把一条 QQ 消息放入安全发送队列。普通模型文本不会被发送，"
                "回复用户必须调用此工具。target_id 必须来自消息上下文。"
            ),
            input_schema=_object_schema(
                {
                    "chat_type": {
                        "type": "string",
                        "enum": ["private", "group"],
                    },
                    "target_id": {"type": "string"},
                    "content": {"type": "string"},
                },
                required=["chat_type", "target_id", "content"],
            ),
            handler=send_qq_message,
            contact_refs=True,
        )
    )

    async def get_qq_message_history(args: dict[str, Any]) -> dict[str, Any]:
        chat_type = _required_str(args, "chat_type")
        target_id = _required_str(args, "target_id")
        limit = min(
            max(int(args.get("limit", config.history_limit)), 1),
            config.history_max_fetch,
        )
        timezone = ZoneInfo(config.timezone)
        start_time = _parse_time_arg(args.get("start_time"), timezone)
        end_time = _parse_time_arg(args.get("end_time"), timezone)
        if start_time is not None and end_time is not None and start_time > end_time:
            raise ValueError("start_time must not be later than end_time")
        messages = await history.get(
            chat_type,
            target_id,
            limit=limit,
            start_time=start_time,
            end_time=end_time,
        )
        return {"messages": messages, "count": len(messages)}

    registry.register(
        Tool(
            name="get_qq_message_history",
            description=(
                "读取私聊或群聊的文本消息历史，按时间升序返回发送者引用与内容。"
                "时间参数可使用 ISO 8601 或 Unix 秒。"
            ),
            input_schema=_object_schema(
                {
                    "chat_type": {
                        "type": "string",
                        "enum": ["private", "group"],
                    },
                    "target_id": {"type": "string"},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": config.history_max_fetch,
                    },
                    "start_time": {"anyOf": [{"type": "string"}, {"type": "number"}]},
                    "end_time": {"anyOf": [{"type": "string"}, {"type": "number"}]},
                },
                required=["chat_type", "target_id"],
            ),
            handler=get_qq_message_history,
            contact_refs=True,
        )
    )

    async def list_qq_friends(args: dict[str, Any]) -> dict[str, Any]:
        return await chat_directory.friends(
            page=int(args.get("page", 1)),
            page_size=_optional_page_size(args),
        )

    async def list_qq_groups(args: dict[str, Any]) -> dict[str, Any]:
        return await chat_directory.groups(
            page=int(args.get("page", 1)),
            page_size=_optional_page_size(args),
        )

    for name, description, handler in (
        (
            "list_qq_friends",
            (
                "列出机器人当前 QQ 好友。返回好友引用、昵称和备注，"
                "并使用 page、page_size 分页；不要暴露原始 QQ 号。"
            ),
            list_qq_friends,
        ),
        (
            "list_qq_groups",
            (
                "列出机器人当前加入的 QQ 群。返回群引用、群名、群备注和人数，"
                "并使用 page、page_size 分页；不要暴露原始群号。"
            ),
            list_qq_groups,
        ),
    ):
        registry.register(
            Tool(
                name=name,
                description=description,
                input_schema=_object_schema(
                    {
                        "page": {"type": "integer", "minimum": 1},
                        "page_size": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": config.directory_max_page_size,
                        },
                    }
                ),
                handler=handler,
                contact_refs=True,
            )
        )

    async def manage_scheduled_tasks(args: dict[str, Any]) -> dict[str, Any]:
        action = _required_str(args, "action")
        if action == "list":
            return {"tasks": [asdict(task) for task in await schedules.list()]}
        if action == "create":
            task = await schedules.create(
                title=_required_str(args, "title"),
                content=_required_str(args, "content"),
                schedule=_required_object(args, "schedule"),
                target_id=str(args.get("target_id", "")),
                chat_type=str(args.get("chat_type", "")),
            )
            return {"ok": True, "task": asdict(task)}
        if action == "update":
            schedule = args.get("schedule")
            if schedule is not None and not isinstance(schedule, dict):
                raise ValueError("schedule must be an object")
            task = await schedules.update(
                _required_str(args, "task_id"),
                title=_optional_str(args, "title"),
                content=_optional_str(args, "content"),
                schedule=schedule,
                enabled=args.get("enabled")
                if isinstance(args.get("enabled"), bool)
                else None,
                target_id=_optional_str(args, "target_id"),
                chat_type=_optional_str(args, "chat_type"),
            )
            return {
                "updated": task is not None,
                "task": asdict(task) if task else None,
            }
        if action == "delete":
            task = await schedules.delete(_required_str(args, "task_id"))
            return {
                "deleted": task is not None,
                "task": asdict(task) if task else None,
            }
        raise ValueError("action must be list, create, update, or delete")

    registry.register(
        Tool(
            name="manage_scheduled_tasks",
            description=(
                "读取、创建、修改或删除持久化定时任务。schedule.kind 支持 "
                "once、interval、daily、weekly、monthly、yearly。"
            ),
            input_schema=_object_schema(
                {
                    "action": {
                        "type": "string",
                        "enum": ["list", "create", "update", "delete"],
                    },
                    "task_id": {"type": "string"},
                    "title": {"type": "string"},
                    "content": {"type": "string"},
                    "schedule": {
                        "type": "object",
                        "description": (
                            "once:{kind,at}; interval:{kind,every_seconds|"
                            "every_minutes|every_hours|every_days}; daily:{kind,time}; "
                            "weekly:{kind,weekdays,time}; monthly:{kind,days_of_month,time}; "
                            "yearly:{kind,month,day,time}"
                        ),
                    },
                    "enabled": {"type": "boolean"},
                    "chat_type": {
                        "type": "string",
                        "enum": ["private", "group"],
                    },
                    "target_id": {"type": "string"},
                },
                ["action"],
            ),
            handler=manage_scheduled_tasks,
            contact_refs=True,
        )
    )

    async def current_time(_: dict[str, Any]) -> dict[str, Any]:
        now = schedules.now()
        return {
            "iso": now.isoformat(),
            "timezone": config.timezone,
            "weekday": now.strftime("%A"),
        }

    if config.time_tool:
        registry.register(
            Tool(
                name="get_current_time",
                description="获取配置时区下的当前时间。",
                input_schema=_object_schema({}),
                handler=current_time,
            )
        )

    async def get_member_identity(args: dict[str, Any]) -> dict[str, Any]:
        identifiers = [key for key in ("contact_id", "qq", "subject") if key in args]
        if len(identifiers) != 1:
            raise ValueError("provide exactly one of contact_id, qq or subject")
        identifier = identifiers[0]
        value = args[identifier]
        if identifier == "subject":
            if not isinstance(value, str) or not value.strip():
                raise ValueError("subject must be a non-empty string")
        elif not isinstance(value, str) or not re.fullmatch(
            r"[1-9][0-9]{4,19}", value.strip()
        ):
            raise ValueError(
                "QQ must be a string of 5–20 ASCII digits without leading zero"
            )
        if identities is None:
            return {"status": "disabled", "source": "keycloak"}
        try:
            user = await identities.lookup_user(
                **{"subject" if identifier == "subject" else "qq": value.strip()}
            )
            if user is None:
                return {"status": "not_found", "source": "keycloak"}
            roles = await identities.realm_roles(user.subject)
        except AmbiguousQQBinding:
            return {"status": "ambiguous", "source": "keycloak"}
        except httpx.HTTPError:
            LOGGER.warning("member identity lookup unavailable")
            return {"status": "unavailable", "source": "keycloak"}
        return {
            "status": "found",
            "source": "keycloak",
            "contact_id": user.qq,
            "subject": user.subject,
            "name": user.name,
            "realm_roles": roles,
            "positions": [
                {
                    "code": position.code,
                    "label": position.label,
                    "scope": position.scope.value,
                    "department": position.department.role_key
                    if position.department is not None
                    else None,
                }
                for position in positions_from_roles(roles)
            ],
        }

    identity_schema = _object_schema(
        {
            "contact_id": {
                "type": "string",
                "description": "已有用户引用，如 sender_id 或 mentions 中的 contact_id",
            },
            "qq": {
                "type": "string",
                "pattern": "^[1-9][0-9]{4,19}$",
                "description": "仅用于用户已明确提供的 QQ 号",
            },
            "subject": {
                "type": "string",
                "minLength": 1,
                "description": "已知的 Keycloak subject（OIDC sub），可来自身份或 guild 查询结果",
            },
        }
    )
    identity_schema["oneOf"] = [
        {"required": [key]} for key in ("contact_id", "qq", "subject")
    ]
    registry.register(
        Tool(
            "get_member_identity",
            "只读查询 Keycloak 成员姓名、subject（sub）、有效领域角色和职务。"
            "contact_id、qq、subject 三选一；从 QQ 查询时优先使用发送者或 @成员的用户引用，"
            "已知 subject 时可直接查询，无需 QQ 绑定。"
            "返回 found、not_found（无匹配的启用成员）、ambiguous（QQ 绑定冲突）、"
            "unavailable 或 disabled 状态。本地称呼和备注不是身份依据；"
            "领域角色不授予 Bot 管理员权限。",
            identity_schema,
            get_member_identity,
            contact_refs=True,
        )
    )

    if guild_service is not None:
        pagination = {
            "page": {"type": "integer", "minimum": 1},
            "page_size": {"type": "integer", "minimum": 1, "maximum": 50},
        }
        node_id_schema = {"type": "integer", "minimum": 1}
        parent_schema = {"type": ["integer", "null"], "minimum": 1}
        subject_list = {"type": "array", "items": {"type": "string"}, "maxItems": 100}
        for name, description, schema, handler in (
            (
                "list_guild_nodes",
                (
                    "查询任务和待办，默认当前发送者直接参与的未完成节点。scope=mine/user/all；"
                    "user 必须提供已知 subject，all 查询系统待办。relation=leader/member/any；"
                    "include_inherited=true 包含向下负责、向上参与的继承关系。"
                    "返回精简摘要：path 为根到自身的 ID/标题路径，child_count 为直接子节点数，"
                    "leaders 为直接负责人，member_count 为普通成员数；完整描述和成员请查详情。"
                    "选创建位置时用 scope=all,status=all,parent_id=null 从根查看，"
                    "独立活动通常在根层；确有从属关系时再按 parent_id 查看相关子节点，或用 query 搜索并核对详情。"
                    "不要用默认的个人待办代表全站结构；has_more=true 表示还有下一页。"
                    "overdue=true 仅查逾期；默认 sort=due 按截止时间排序，sort=position 按看板同级顺序。"
                ),
                _object_schema(
                    {
                        **pagination,
                        "scope": {"type": "string", "enum": ["mine", "user", "all"]},
                        "subject": {"type": "string"},
                        "relation": {
                            "type": "string",
                            "enum": ["leader", "member", "any"],
                        },
                        "status": {
                            "type": "string",
                            "enum": ["open", "completed", "all"],
                        },
                        "include_inherited": {"type": "boolean"},
                        "overdue": {"type": "boolean"},
                        "parent_id": parent_schema,
                        "query": {"type": "string", "description": "标题包含的文字"},
                        "sort": {"type": "string", "enum": ["due", "position"]},
                    }
                ),
                guild_service.list_nodes,
            ),
            (
                "get_guild_node",
                "查询 guild 节点完整描述、path、child_count、创建者和全部直接负责人/成员。选择父节点时核对其职责和路径。node_id 必须来自已知节点或查询结果。",
                _object_schema({"node_id": node_id_schema}, ["node_id"]),
                guild_service.get_node,
            ),
            (
                "list_guild_activity",
                (
                    "查询全站或指定节点最近动态：新建节点、评论、描述变更。"
                    "按时间倒序分页；since 为 ISO 8601 时间。它不是完整的操作审计记录。"
                ),
                _object_schema(
                    {
                        **pagination,
                        "node_id": node_id_schema,
                        "since": {"type": "string"},
                    }
                ),
                guild_service.list_activity,
            ),
            (
                "create_guild_node",
                (
                    "根据当前发送者的自然语言创建请求及相关上下文创建节点。"
                    "创建者由程序绑定当前发送者，不能指定或代替别人。"
                    "parent_id 必填：根节点传 null，子节点传已核实的父节点 ID。"
                    "优先遵循用户明确归属；未指定时，独立活动、独立项目和并列事项通常建在根层，"
                    "无需用户特意要求根节点；只有明确属于现有事项的组成部分、阶段或子任务才建子节点。"
                    "主题相关、名称相近、负责人相同不足以建立父子关系，不能为了归类而嵌套。"
                    "未指定负责人时由创建者负责。"
                    "负责人和成员使用已验证的 Keycloak subject，不能重叠；不会自动继承父节点直接成员。"
                    "考虑子节点时先查询并核实父节点职责、路径和实际从属关系；"
                    "任务信息不足或位置有实质歧义时先追问，清楚的独立活动无需追问父节点。"
                    "默认先向用户确认标题、完整位置及已指定的任务信息；"
                    "只有当前发送者自己在当前消息或前文已明确、完整地描述任务和归属，"
                    "或已确认具体方案时才可直接创建。模型自己的总结不等于用户授权。"
                    "同一消息只创建一个节点，重复调用返回原结果；定时任务不能创建。"
                ),
                _object_schema(
                    {
                        "parent_id": {
                            **parent_schema,
                            "description": "根节点为 null（独立活动/项目通常使用）；明确属于现有事项的子节点使用已核实父节点 ID，不能省略",
                        },
                        "title": {"type": "string", "minLength": 1, "maxLength": 200},
                        "description": {"type": "string", "maxLength": 20000},
                        "due_at": {
                            "type": ["string", "null"],
                            "description": "ISO 8601；无时区时使用配置时区",
                        },
                        "tags": {
                            "type": "array",
                            "maxItems": 10,
                            "items": {"type": "string", "maxLength": 32},
                        },
                        "leader_subjects": subject_list,
                        "member_subjects": subject_list,
                    },
                    ["title", "parent_id"],
                ),
                guild_service.create_node,
            ),
        ):
            registry.register(
                Tool(name, description, schema, handler, task_context=True)
            )

    async def get_contact_profile(args: dict[str, Any]) -> dict[str, Any]:
        return contacts.get(_required_str(args, "contact_id"))

    async def list_contact_profiles(args: dict[str, Any]) -> dict[str, Any]:
        return contacts.page(
            page=int(args.get("page", 1)),
            page_size=min(
                max(int(args.get("page_size", config.directory_default_page_size)), 1),
                config.directory_max_page_size,
            ),
        )

    async def search_contact_profiles(args: dict[str, Any]) -> dict[str, Any]:
        return contacts.page(
            query=_required_str(args, "query"),
            page=int(args.get("page", 1)),
            page_size=min(
                max(int(args.get("page_size", config.directory_default_page_size)), 1),
                config.directory_max_page_size,
            ),
        )

    async def update_contact_profile(args: dict[str, Any]) -> dict[str, Any]:
        contact_id = _required_str(args, "contact_id")
        display_name = _optional_str(args, "display_name")
        notes = _optional_str(args, "notes")
        if display_name is None and notes is None:
            raise ValueError("display_name or notes is required")
        return contacts.update(contact_id, display_name, notes)

    for name, description, schema, handler in (
        (
            "list_contact_profiles",
            (
                "分页列出机器人保存的本地称呼、昵称和备注，不查询 Keycloak 身份。资料独立于 QQ 好友列表，"
                "包含群聊中见过或人工预置的非好友同事。"
            ),
            _object_schema(
                {
                    "page": {"type": "integer", "minimum": 1},
                    "page_size": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": config.directory_max_page_size,
                    },
                }
            ),
            list_contact_profiles,
        ),
        (
            "search_contact_profiles",
            "按本地自定义称呼、QQ 昵称或备注搜索联系人，并支持分页；不搜索 Keycloak。",
            _object_schema(
                {
                    "query": {"type": "string"},
                    "page": {"type": "integer", "minimum": 1},
                    "page_size": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": config.directory_max_page_size,
                    },
                },
                ["query"],
            ),
            search_contact_profiles,
        ),
        (
            "get_contact_profile",
            "读取本地自定义称呼（display_name）、QQ 昵称和备注；姓名和职务请用 get_member_identity。该账号不要求是机器人 QQ 好友。",
            _object_schema({"contact_id": {"type": "string"}}, ["contact_id"]),
            get_contact_profile,
        ),
        (
            "update_contact_profile",
            (
                "新建或更新本地自定义称呼（display_name）和备注；QQ 昵称由消息观察所得。"
                "该账号不要求是机器人 QQ 好友。"
                "此工具不能修改 Keycloak 姓名、职务、QQ 绑定或 Bot admin 权限。"
            ),
            _object_schema(
                {
                    "contact_id": {"type": "string"},
                    "display_name": {
                        "type": "string",
                        "description": "本地自定义称呼，不是 Keycloak 姓名",
                    },
                    "notes": {"type": "string"},
                },
                ["contact_id"],
            ),
            update_contact_profile,
        ),
    ):
        registry.register(Tool(name, description, schema, handler, contact_refs=True))

    async def list_knowledge_files(args: dict[str, Any]) -> dict[str, Any]:
        return {
            "files": knowledge.list_files(
                str(args.get("scope", "all")),  # type: ignore[arg-type]
                str(args.get("prefix", "")),
            )
        }

    async def read_knowledge_lines(args: dict[str, Any]) -> dict[str, Any]:
        return knowledge.read_lines(
            str(args.get("scope", "knowledge")),  # type: ignore[arg-type]
            _required_str(args, "path"),
            int(args.get("start_line", 1)),
            int(args.get("end_line", 80)),
        )

    async def search_knowledge(args: dict[str, Any]) -> dict[str, Any]:
        return {
            "matches": knowledge.search(
                _required_str(args, "query"),
                scope=str(args.get("scope", "all")),  # type: ignore[arg-type]
                path=str(args.get("path", "")),
                max_results=int(args.get("max_results", 20)),
            )
        }

    async def write_notebook(args: dict[str, Any]) -> dict[str, Any]:
        return knowledge.write(
            _required_str(args, "path"),
            _required_str(args, "content"),
            mode=str(args.get("mode", "replace")),  # type: ignore[arg-type]
        )

    async def delete_notebook(args: dict[str, Any]) -> dict[str, Any]:
        return knowledge.delete(_required_str(args, "path"))

    tools = (
        (
            "list_knowledge_files",
            "列出知识库或记事本中的文件，先调用它再读取文件。",
            _object_schema(
                {
                    "scope": {
                        "type": "string",
                        "enum": ["all", "knowledge", "notebook"],
                    },
                    "prefix": {"type": "string"},
                }
            ),
            list_knowledge_files,
        ),
        (
            "read_knowledge_lines",
            "读取一个知识库或记事本文件的指定行范围。",
            _object_schema(
                {
                    "scope": {
                        "type": "string",
                        "enum": ["knowledge", "notebook"],
                    },
                    "path": {"type": "string"},
                    "start_line": {"type": "integer", "minimum": 1},
                    "end_line": {"type": "integer", "minimum": 1},
                },
                ["scope", "path"],
            ),
            read_knowledge_lines,
        ),
        (
            "search_knowledge",
            "在知识库和记事本中按字面内容搜索。",
            _object_schema(
                {
                    "query": {"type": "string"},
                    "scope": {
                        "type": "string",
                        "enum": ["all", "knowledge", "notebook"],
                    },
                    "path": {"type": "string"},
                    "max_results": {"type": "integer", "minimum": 1},
                },
                ["query"],
            ),
            search_knowledge,
        ),
        (
            "write_notebook",
            "新建、替换或追加记事本中的 Markdown/文本文件。",
            _object_schema(
                {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "mode": {
                        "type": "string",
                        "enum": ["replace", "append"],
                    },
                },
                ["path", "content"],
            ),
            write_notebook,
        ),
        (
            "delete_notebook",
            "删除记事本中的一个文件。只允许操作 notebook 目录。",
            _object_schema({"path": {"type": "string"}}, ["path"]),
            delete_notebook,
        ),
    )
    for name, description, schema, handler in tools:
        registry.register(Tool(name, description, schema, handler))

    for remote_tool in mcp.tools:

        async def call_remote(
            args: dict[str, Any],
            exposed_name: str = remote_tool.exposed_name,
        ) -> dict[str, Any]:
            return await mcp.call(exposed_name, args)

        registry.register(
            Tool(
                name=remote_tool.exposed_name,
                description=remote_tool.description,
                input_schema=remote_tool.input_schema
                or {"type": "object", "properties": {}},
                handler=call_remote,
            )
        )
    return registry


def _object_schema(
    properties: dict[str, Any],
    required: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


def _required_str(args: dict[str, Any], key: str) -> str:
    value = str(args.get(key, "")).strip()
    if not value:
        raise ValueError(f"{key} is required")
    return value


def _optional_str(args: dict[str, Any], key: str) -> str | None:
    if key not in args:
        return None
    return str(args.get(key, ""))


def _required_object(args: dict[str, Any], key: str) -> dict[str, Any]:
    value = args.get(key)
    if not isinstance(value, dict) or not value:
        raise ValueError(f"{key} must be a non-empty object")
    return value


def _parse_time_arg(value: Any, timezone: ZoneInfo) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    try:
        return float(text)
    except ValueError:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone)
        return parsed.timestamp()


def _optional_page_size(args: dict[str, Any]) -> int | None:
    if "page_size" not in args:
        return None
    return int(args["page_size"])


def _optional_int(value: Any) -> int | None:
    if value in {None, ""}:
        return None
    return int(value)


def json_tool_result(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
