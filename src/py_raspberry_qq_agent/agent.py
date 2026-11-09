from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import asdict
from typing import Any

from .config import AppConfig
from .llm import AssistantReply, OpenAICompatibleClient
from .tasks import AgentTask, TaskQueue
from .toolset import ToolRegistry, json_tool_result

LOGGER = logging.getLogger(__name__)


class AgentRunner:
    def __init__(
        self,
        config: AppConfig,
        llm: OpenAICompatibleClient,
        tools: ToolRegistry,
    ) -> None:
        self._config = config
        self._llm = llm
        self._tools = tools

    async def run_task(self, task: AgentTask) -> None:
        activation = _activation_prompt(self._tools.ids.encode(asdict(task)))
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._config.system_prompt},
            {"role": "user", "content": activation},
        ]
        definitions = self._tools.definitions()

        for round_number in range(self._config.max_tool_rounds):
            reply = await self._llm.complete(messages, tools=definitions)
            if not reply.tool_calls:
                if reply.content:
                    LOGGER.debug(
                        "agent returned text without QQ delivery (length=%d)",
                        len(reply.content),
                    )
                return

            normalized_calls = [
                _normalize_tool_call(call, index)
                for index, call in enumerate(reply.tool_calls)
            ]
            messages.append(_assistant_message(reply, normalized_calls))
            for call in normalized_calls:
                await self._execute_call(call, messages, task)
            LOGGER.info(
                "agent tool round=%d calls=%d",
                round_number + 1,
                len(reply.tool_calls),
            )

        raise RuntimeError("agent reached max_tool_rounds")

    async def _execute_call(
        self,
        call: dict[str, Any],
        messages: list[dict[str, Any]],
        task: AgentTask,
    ) -> None:
        function = call.get("function") or {}
        tool_name = str(function.get("name", ""))
        tool_call_id = str(call.get("id") or f"call-{uuid.uuid4().hex[:12]}")
        raw_arguments = function.get("arguments") or "{}"
        try:
            if isinstance(raw_arguments, str):
                arguments = json.loads(raw_arguments)
            elif isinstance(raw_arguments, dict):
                arguments = raw_arguments
            else:
                raise TypeError("tool arguments must be JSON")
            if not isinstance(arguments, dict):
                raise TypeError("tool arguments must be a JSON object")
            result = await asyncio.wait_for(
                self._tools.execute(tool_name, arguments, task=task),
                timeout=self._config.tool_timeout_seconds,
            )
            payload = {"ok": True, "result": result}
            LOGGER.info("tool succeeded name=%s", tool_name)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - tool failures are returned to the model
            payload = {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
            LOGGER.warning(
                "tool failed name=%s error=%s", tool_name, type(exc).__name__
            )

        messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": json_tool_result(payload),
            }
        )


class AgentWorker:
    def __init__(self, runner: AgentRunner, tasks: TaskQueue) -> None:
        self._runner = runner
        self._tasks = tasks
        self._worker: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._worker = asyncio.create_task(self._run(), name="agent-worker")

    async def stop(self) -> None:
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None

    async def _run(self) -> None:
        while True:
            task = await self._tasks.get()
            try:
                await self._runner.run_task(task)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception(
                    "agent task failed id=%s source=%s", task.id, task.source
                )
            finally:
                self._tasks.task_done()


def _assistant_message(
    reply: AssistantReply,
    normalized_calls: list[dict[str, Any]],
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "role": "assistant",
        "content": reply.content or None,
        "tool_calls": normalized_calls,
    }
    return message


def _normalize_tool_call(call: dict[str, Any], index: int) -> dict[str, Any]:
    function = call.get("function") or {}
    arguments = function.get("arguments") or "{}"
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments, ensure_ascii=False)
    return {
        "id": str(call.get("id") or f"call-{uuid.uuid4().hex[:12]}-{index}"),
        "type": "function",
        "function": {
            "name": str(function.get("name", "")),
            "arguments": arguments,
        },
    }


def _activation_prompt(task: dict[str, Any]) -> str:
    return (
        "处理当前任务。需要回复 QQ 时调用 send_qq_message，使用上下文中的 "
        "chat_type 和 target_id。没有目标聊天的任务不要求发送 QQ。\n"
        f"TASK={json.dumps(task, ensure_ascii=False)}"
    )
