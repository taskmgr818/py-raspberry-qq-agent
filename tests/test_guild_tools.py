from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import httpx
import pytest

from py_raspberry_qq_agent.agent import AgentRunner
from py_raspberry_qq_agent.guild import GuildConfig, GuildService, guild_command
from py_raspberry_qq_agent.keycloak import AmbiguousQQBinding, User
from py_raspberry_qq_agent.llm import AssistantReply
from py_raspberry_qq_agent.tasks import AgentTask


def command(sender="12345", action="create", source="incoming_message"):
    return AgentTask(
        "处理当前消息",
        source=source,
        sender_id=sender,
        chat_type="group",
        target_id="99999",
        metadata={"guild_command": action, "message_id": sender},
    )


def service_for(repository=None):
    people = {
        "alice": User("alice", "Alice", "12345"),
        "bob": User("bob", "Bob", None),
        "carol": User("carol", "Carol", "67890"),
    }
    directory = AsyncMock()

    async def lookup(*, qq=None, subject=None, refresh=False):
        return (
            people.get(subject)
            if subject is not None
            else next((user for user in people.values() if user.qq == qq), None)
        )

    directory.lookup_user.side_effect = lookup
    directory.realm_roles.return_value = ["permission-techdept-admin"]
    repository = repository or AsyncMock()
    return (
        GuildService(
            GuildConfig(public_url="https://guild.example.com"),
            repository,
            directory,
            ZoneInfo("Asia/Shanghai"),
        ),
        repository,
        directory,
    )


@pytest.mark.parametrize(
    ("text", "action"),
    [
        ("/guild 创建 任务", "create"),
        (" /guild create Task ", "create"),
        ("/guild 待办", "read"),
        ("/guild", "read"),
        ("请参考历史消息 /guild create Task", None),
        ("/guild创建 任务", None),
    ],
)
def test_guild_commands_require_explicit_prefix(text, action):
    assert guild_command(text) == action


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "task",
    [
        None,
        command(source="scheduler"),
        command(sender=""),
        command(action="read"),
        command(action=None),
    ],
)
async def test_creation_requires_current_sender_and_create_command(task):
    service, repository, directory = service_for()
    with pytest.raises(PermissionError):
        await service.create_node({"title": "Task"}, task)
    directory.lookup_user.assert_not_awaited()
    repository.create_node.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (None, PermissionError),
        (AmbiguousQQBinding(), PermissionError),
        (httpx.ConnectError("secret-bearing-url"), RuntimeError),
    ],
)
async def test_actor_identity_failures_never_reach_database(failure, expected):
    service, repository, directory = service_for()
    directory.lookup_user.side_effect = failure
    directory.lookup_user.return_value = None
    with pytest.raises(expected) as error:
        await service.create_node({"title": "Task"}, command())
    assert "secret-bearing-url" not in str(error.value)
    repository.create_node.assert_not_awaited()


@pytest.mark.asyncio
async def test_position_and_bot_admin_do_not_grant_guild_admission():
    service, repository, directory = service_for()
    directory.realm_roles.return_value = ["position-techdept-head"]
    with pytest.raises(PermissionError, match="准入"):
        await service.list_nodes({}, command())
    repository.nodes.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_normalizes_payload_and_uses_fresh_identity_for_unbound_targets():
    service, repository, directory = service_for()
    repository.create_node.return_value = {
        "id": 7,
        "created_by": "alice",
        "members": [{"subject": "bob", "role": "leader"}],
        "created_at": datetime(2026, 11, 9, tzinfo=UTC),
    }
    result = await service.create_node(
        {
            "title": " Task ",
            "description": " Detail ",
            "leader_subjects": ["bob", "bob"],
            "tags": [" xx ", "xx"],
            "due_at": "2026-11-10T12:00:00",
        },
        command(),
    )
    subject, permissions, payload, request_key = repository.create_node.call_args.args
    assert subject == "alice" and permissions.manager
    assert payload == {
        "title": "Task",
        "description": "Detail",
        "tags": ["xx"],
        "due_at": "2026-11-10T04:00:00+00:00",
        "parent_id": None,
        "leader_subjects": ["bob"],
        "member_subjects": [],
    }
    assert request_key == "group:99999:12345"
    assert result["creator"] == {"subject": "alice", "name": "Alice"}
    assert result["members"][0]["name"] == "Bob"
    assert result["url"] == "https://guild.example.com/nodes/7"
    json.dumps(result)
    assert any(
        call.kwargs == {"qq": "12345", "refresh": True}
        for call in directory.lookup_user.await_args_list
    )
    assert any(
        call.kwargs == {"subject": "bob", "refresh": True}
        for call in directory.lookup_user.await_args_list
    )
    assert all(
        call.kwargs == {"refresh": True}
        for call in directory.realm_roles.await_args_list
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [
        {"title": ""},
        {"title": "x" * 201},
        {"title": "T", "description": "x" * 20001},
        {"title": "T", "parent_id": True},
        {"title": "T", "parent_id": 0},
        {"title": "T", "due_at": 123},
        {"title": "T", "due_at": "not a date"},
        {"title": "T", "leader_subjects": "bob"},
        {"title": "T", "leader_subjects": [""]},
        {"title": "T", "leader_subjects": ["bob"], "member_subjects": ["bob"]},
        {"title": "T", "tags": ["x" * 33]},
        {"title": "T", "created_by": "bob"},
        {"title": "T", "sender_id": "67890"},
        {"title": "T", "request_key": "forged"},
    ],
)
async def test_invalid_create_arguments_do_not_write(args):
    service, repository, _ = service_for()
    with pytest.raises(ValueError):
        await service.create_node(args, command())
    repository.create_node.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [
        {"scope": "user"},
        {"subject": "bob"},
        {"scope": "all", "subject": "bob"},
        {"page": 0},
        {"page_size": 51},
        {"include_inherited": "true"},
        {"overdue": 1},
        {"status": "unknown"},
        {"relation": "unknown"},
        {"parent_id": False},
    ],
)
async def test_invalid_list_arguments_do_not_query(args):
    service, repository, _ = service_for()
    with pytest.raises(ValueError):
        await service.list_nodes(args, command())
    repository.nodes.assert_not_awaited()


@pytest.mark.asyncio
async def test_tasks_and_todos_share_one_bounded_query_without_provisioning():
    service, repository, _ = service_for()
    repository.nodes.return_value = [{"id": 1}, {"id": 2}]
    result = await service.list_nodes({"page_size": 1}, command())
    filters = repository.nodes.call_args.args[0]
    assert filters["subject"] == "alice" and filters["status"] == "open"
    assert filters["include_inherited"] is False
    assert result == {
        "items": [{"id": 1, "url": "https://guild.example.com/nodes/1"}],
        "page": 1,
        "has_more": True,
        "next_page": 2,
    }
    await service.list_nodes(
        {"scope": "user", "subject": "not-yet-in-guild"}, command()
    )
    assert repository.nodes.call_args.args[0]["subject"] == "not-yet-in-guild"
    await service.list_nodes({"scope": "all", "parent_id": None}, command())
    filters = repository.nodes.call_args.args[0]
    assert filters["subject"] is None and filters["filter_parent"]
    repository.create_node.assert_not_awaited()


@pytest.mark.asyncio
async def test_agent_binds_context_per_task_and_does_not_expose_actor_parameters(
    config,
):
    service, repository, _ = service_for()

    async def create(subject, permissions, payload, request_key):
        await asyncio.sleep(0)
        return {"id": 1, "created_by": subject, "title": payload["title"]}

    repository.create_node.side_effect = create
    # Use the normal registry builder so definitions and execution are both exercised.
    from py_raspberry_qq_agent.toolset import build_tool_registry

    registry = build_tool_registry(
        config=config,
        schedules=Mock(),
        knowledge=Mock(),
        contacts=Mock(),
        outbound=Mock(),
        history=Mock(),
        chat_directory=Mock(),
        mcp=Mock(tools=[]),
        guild_service=service,
    )

    class Llm:
        async def complete(self, messages, **kwargs):
            if messages[-1]["role"] == "tool":
                assert json.loads(messages[-1]["content"])["ok"]
                return AssistantReply("", [], {})
            schema = next(
                item["function"]["parameters"]
                for item in kwargs["tools"]
                if item["function"]["name"] == "create_guild_node"
            )
            assert (
                not {"sender_id", "subject", "created_by", "request_key"}
                & schema["properties"].keys()
            )
            return AssistantReply(
                "",
                [
                    {
                        "function": {
                            "name": "create_guild_node",
                            "arguments": '{"title":"Task"}',
                        }
                    }
                ],
                {},
            )

    runner = AgentRunner(config, Llm(), registry)
    await asyncio.gather(
        runner.run_task(command()), runner.run_task(command(sender="67890"))
    )
    assert {call.args[0] for call in repository.create_node.await_args_list} == {
        "alice",
        "carol",
    }
    assert len({call.args[3] for call in repository.create_node.await_args_list}) == 2
    with pytest.raises(PermissionError):
        await registry.execute("create_guild_node", {"title": "Task"})


@pytest.mark.asyncio
async def test_explicit_guild_command_bypasses_classifier_but_obeys_routing(config):
    from test_priority_and_incoming import FakeClassifier, _message, _services

    config.guild.enabled = True
    classifier = FakeClassifier(important=False)
    incoming, tasks, _ = _services(config, classifier)
    message = _message("12345", "/guild 创建 Task", "one")
    await incoming.handle(message)
    task = await tasks.get()
    assert task.sender_id == "12345" and task.metadata["guild_command"] == "create"
    assert classifier.calls == 0
    message.chat_type, message.raw_chat_id, message.message_id = "group", "99999", "two"
    await incoming.handle(message)
    assert tasks.empty()  # Group commands must still @ the Bot.
    message.mentioned_bot, message.message_id = True, "three"
    await incoming.handle(message)
    assert tasks.qsize() == 1
    config.allowlist_enabled = True
    config.allowlist_group_ids = []
    incoming, tasks, _ = _services(config, classifier)
    await incoming.handle(message)
    assert tasks.empty()
