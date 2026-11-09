from __future__ import annotations

import json
from unittest.mock import AsyncMock, Mock, call

import pytest

from py_raspberry_qq_agent.agent import AgentRunner
from py_raspberry_qq_agent.contacts import ContactDirectory, RoleStore
from py_raspberry_qq_agent.incoming import IncomingService
from py_raspberry_qq_agent.keycloak import User
from py_raspberry_qq_agent.llm import AssistantReply
from py_raspberry_qq_agent.messages import MessageStore
from py_raspberry_qq_agent.napcat import NapCatClient, parse_message_content
from py_raspberry_qq_agent.priority import ImportanceDecision
from py_raspberry_qq_agent.tasks import TaskQueue
from py_raspberry_qq_agent.toolset import HistoryService, build_tool_registry


@pytest.mark.parametrize(
    "body",
    [
        "[CQ:at,qq=90001] 查询[CQ:at,qq=12345]和[CQ:at,qq=67890]的身份[CQ:at,qq=all]",
        [
            {"type": "at", "data": {"qq": "90001"}},
            {"type": "text", "data": {"text": " 查询"}},
            {"type": "at", "data": {"qq": "12345"}},
            {"type": "text", "data": {"text": "和"}},
            {"type": "at", "data": {"qq": "67890"}},
            {"type": "text", "data": {"text": "的身份"}},
            {"type": "at", "data": {"qq": "all"}},
        ],
    ],
)
def test_array_and_cq_messages_preserve_order_without_raw_targets(body):
    text, mentions, mentioned_bot = parse_message_content(body, "90001")
    assert text == "查询@1和@2的身份@全体成员"
    assert mentions == [
        {"contact_id": "12345", "marker": "@1"},
        {"contact_id": "67890", "marker": "@2"},
    ]
    assert mentioned_bot


@pytest.mark.asyncio
async def test_mention_survives_incoming_history_and_agent_member_lookup(config):
    contacts = ContactDirectory(config.data_dir / "profiles.json")
    store = MessageStore(config.data_dir / "messages.jsonl")
    tasks = TaskQueue()
    incoming = IncomingService(
        contacts=contacts,
        messages=store,
        tasks=tasks,
        gate=AsyncMock(
            decide=AsyncMock(return_value=ImportanceDecision(True, "help", "test"))
        ),
    )
    napcat = NapCatClient(config, incoming.handle)
    payload = {
        "self_id": 90001,
        "message_type": "group",
        "group_id": 88888,
        "user_id": 77777,
        "message_id": 1,
        "time": 1700000000,
        "message": "[CQ:at,qq=90001] 查[CQ:at,qq=12345]的身份",
        "sender": {"nickname": "Sender"},
    }
    message = napcat._parse_message(payload)
    assert message is not None
    await incoming.handle(message)
    task = await tasks.get()
    assert task.metadata["mentions"] == [{"contact_id": "12345", "marker": "@1"}]
    directory = AsyncMock()
    directory.lookup_user.return_value = User("alice-sub", "Alice", "12345")
    directory.realm_roles.return_value = ["position-clinic-member"]
    napcat.fetch_history = AsyncMock(return_value=[payload])
    history = HistoryService(config, store, napcat)
    registry = build_tool_registry(
        config=config,
        schedules=Mock(),
        knowledge=Mock(),
        contacts=contacts,
        outbound=Mock(),
        history=history,
        chat_directory=Mock(),
        mcp=Mock(tools=[]),
        identities=directory,
    )

    class Llm:
        calls = 0

        async def complete(self, messages, **kwargs):
            for raw in ["12345", "90001", "77777", "88888"]:
                assert raw not in json.dumps(messages)
            self.calls += 1
            if self.calls == 1:
                encoded = json.loads(messages[1]["content"].split("TASK=", 1)[1])
                mention_ref = encoded["metadata"]["mentions"][0]["contact_id"]
                self.mention_ref = mention_ref
                return AssistantReply(
                    "",
                    [
                        {
                            "function": {
                                "name": "get_member_identity",
                                "arguments": json.dumps({"contact_id": mention_ref}),
                            }
                        }
                    ],
                    {},
                )
            result = json.loads(messages[-1]["content"])
            assert result["ok"]
            assert result["result"]["subject"] == "alice-sub"
            assert result["result"]["contact_id"] == self.mention_ref
            if self.calls == 2:
                return AssistantReply(
                    "",
                    [
                        {
                            "function": {
                                "name": "get_member_identity",
                                "arguments": json.dumps(
                                    {"subject": result["result"]["subject"]}
                                ),
                            }
                        }
                    ],
                    {},
                )
            return AssistantReply("", [], {})

    llm = Llm()
    await AgentRunner(config, llm, registry).run_task(task)
    assert llm.calls == 3
    assert directory.lookup_user.await_args_list == [
        call(qq="12345"),
        call(subject="alice-sub"),
    ]
    group_ref = registry.ids.encode({"chat_type": "group", "target_id": "88888"})[
        "target_id"
    ]
    result = await registry.execute(
        "get_qq_message_history", {"chat_type": "group", "target_id": group_ref}
    )
    assert result["messages"][0]["content"] == "查@1的身份"
    assert result["messages"][0]["mentions"] == [
        {"contact_id": llm.mention_ref, "marker": "@1"}
    ]
    assert "12345" not in json.dumps(result)
    assert (await store.history("group", "88888"))[0]["mentions"] == message.mentions
    assert not RoleStore(config.data_dir / "roles.json").is_admin("12345")
