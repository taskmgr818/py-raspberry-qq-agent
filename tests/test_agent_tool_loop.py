from __future__ import annotations

import asyncio
import json
from unittest.mock import Mock

import pytest

from py_raspberry_qq_agent.agent import AgentRunner, AgentWorker
from py_raspberry_qq_agent.allowlist import ChatAllowlist
from py_raspberry_qq_agent.config import AppConfig
from py_raspberry_qq_agent.llm import AssistantReply
from py_raspberry_qq_agent.napcat import OutboundQueue
from py_raspberry_qq_agent.tasks import AgentTask, TaskQueue
from py_raspberry_qq_agent.toolset import Tool, ToolRegistry, build_tool_registry


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["incoming_message", "scheduler"])
async def test_model_cannot_bypass_allowlist_for_notifications(
    config: AppConfig, source: str
) -> None:
    outbound = OutboundQueue(
        config,
        Mock(),
        ChatAllowlist(enabled=True, private_qq_ids=[], group_ids=[]),
    )
    registry = build_tool_registry(
        config=config,
        schedules=Mock(),
        knowledge=Mock(),
        contacts=Mock(),
        outbound=outbound,
        history=Mock(),
        chat_directory=Mock(),
        mcp=Mock(tools=[]),
    )

    class Llm:
        calls = 0

        async def complete(self, messages, **kwargs):
            self.calls += 1
            names = [tool["function"]["name"] for tool in kwargs["tools"]]
            assert "enqueue_notification" not in names
            if self.calls == 1:
                task = json.loads(messages[1]["content"].split("TASK=", 1)[1])
                return AssistantReply(
                    "",
                    [
                        {
                            "function": {
                                "name": "send_qq_message",
                                "arguments": json.dumps(
                                    {
                                        "chat_type": "private",
                                        "target_id": task["target_id"],
                                        "content": "model message",
                                        "enforce_allowlist": False,
                                    }
                                ),
                            }
                        }
                    ],
                    {},
                )
            result = json.loads(messages[-1]["content"])
            assert result["ok"] is False
            assert "PermissionError" in result["error"]
            return AssistantReply("", [], {})

    llm = Llm()
    await AgentRunner(config, llm, registry).run_task(
        AgentTask("send", source=source, chat_type="private", target_id="54321")
    )
    assert llm.calls == 2
    assert outbound._queue.empty()


@pytest.mark.asyncio
async def test_model_uses_reference_but_transport_receives_plain_qq(
    config: AppConfig,
) -> None:
    sent = []
    registry = ToolRegistry()

    async def send(args):
        sent.append(args)
        return {"queued": True}

    registry.register(
        Tool("send_qq_message", "send", {"type": "object"}, send, contact_refs=True)
    )

    class Llm:
        calls = 0

        async def complete(self, messages, **kwargs):
            assert "123456789" not in json.dumps(messages)
            self.calls += 1
            if self.calls == 1:
                task = json.loads(messages[1]["content"].split("TASK=", 1)[1])
                return AssistantReply(
                    "",
                    [
                        {
                            "id": "send-1",
                            "function": {
                                "name": "send_qq_message",
                                "arguments": json.dumps(
                                    {
                                        "chat_type": "private",
                                        "target_id": task["target_id"],
                                        "content": "ok",
                                    }
                                ),
                            },
                        }
                    ],
                    {},
                )
            return AssistantReply("done", [], {})

    runner = AgentRunner(config, Llm(), registry)
    await runner.run_task(
        AgentTask("reply", chat_type="private", target_id="123456789")
    )
    assert sent == [{"chat_type": "private", "target_id": "123456789", "content": "ok"}]


@pytest.mark.asyncio
async def test_worker_drops_failed_task_and_continues_without_model_cleanup() -> None:
    queue = TaskQueue()
    await queue.add("fails")
    await queue.add("works without a chat target")
    calls = []

    class Runner:
        async def run_task(self, task: AgentTask) -> None:
            calls.append(task.content)
            if task.content == "fails":
                raise RuntimeError("expected")

    worker = AgentWorker(Runner(), queue)
    worker.start()
    try:
        await asyncio.wait_for(queue.join(), timeout=1)
    finally:
        await worker.stop()
    assert calls == ["fails", "works without a chat target"]
    assert queue.empty()


@pytest.mark.asyncio
async def test_tool_rejects_invented_or_wrong_kind_references() -> None:
    registry = ToolRegistry()
    called = []

    async def handler(args):
        called.append(args)

    registry.register(Tool("send", "", {}, handler, contact_refs=True))
    group = registry.ids.encode({"chat_type": "group", "target_id": "99887766"})[
        "target_id"
    ]
    for target in ["99887766", "user:invented", group]:
        with pytest.raises(ValueError):
            await registry.execute(
                "send", {"chat_type": "private", "target_id": target}
            )
    assert called == []
