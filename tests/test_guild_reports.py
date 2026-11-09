from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from py_raspberry_qq_agent.guild_reports import (
    REPORT_TIMEZONE,
    WeeklyReports,
    previous_week,
)
from py_raspberry_qq_agent.keycloak import User
from py_raspberry_qq_agent.knowledge import KnowledgeBase
from py_raspberry_qq_agent.llm import AssistantReply


def test_previous_full_week_uses_monday_boundary_in_utc_plus_eight() -> None:
    before = previous_week(datetime(2026, 10, 25, 15, 59, tzinfo=UTC))
    after = previous_week(datetime(2026, 10, 25, 16, 0, tzinfo=UTC))
    assert before == (
        datetime(2026, 10, 12, tzinfo=REPORT_TIMEZONE),
        datetime(2026, 10, 19, tzinfo=REPORT_TIMEZONE),
    )
    assert after == (
        datetime(2026, 10, 19, tzinfo=REPORT_TIMEZONE),
        datetime(2026, 10, 26, tzinfo=REPORT_TIMEZONE),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("qq", ["123456789", None])
async def test_weekly_markdown_has_fixed_counts_names_and_no_qq_target(config, qq) -> None:
    at = datetime(2026, 10, 26, 10, tzinfo=REPORT_TIMEZONE)
    start, end = previous_week(at)
    repository = AsyncMock()
    repository.now.return_value = at
    repository.query.side_effect = [
        [
            {
                "id": 1,
                "title": "Created in period",
                "created_at": start,
                "completed_at": None,
                "due_at": at - timedelta(days=1),
            },
            {
                "id": 2,
                "title": "Completed in period",
                "created_at": start - timedelta(days=3),
                "completed_at": end - timedelta(seconds=1),
                "due_at": None,
            },
            {
                "id": 3,
                "title": "Created after period",
                "created_at": end,
                "completed_at": None,
                "due_at": None,
            },
        ],
        [{"node_id": 1, "subject": "alice", "role": "leader"}],
        [
            {
                "node_id": 1,
                "author_subject": "alice",
                "body": "Progress update",
                "created_at": start,
            }
        ],
    ]
    directory = AsyncMock()
    directory.lookup_user.return_value = User("alice", "Alice", qq)
    llm = AsyncMock()
    llm.complete.return_value = AssistantReply("## 进展\n已整理。", [], {})
    mcp = AsyncMock()
    report = WeeklyReports(
        config,
        repository,
        directory,
        llm,
        KnowledgeBase(config.knowledge_dir, config.notebook_dir),
        mcp,
    )
    report.start()
    assert report._task is None  # Empty schedule must not invent a generation time.
    path = await report.generate(at)
    text = (config.notebook_dir / path).read_text()
    assert path == "guild-reports/2026-10-19.md"
    assert "周内新增：1" in text and "周内完成：1" in text
    assert "当前未完成：2" in text and "当前逾期：1" in text
    prompt = str(llm.complete.call_args)
    assert "Alice" in prompt and "123456789" not in prompt
    assert "target_id" not in prompt
    directory.lookup_user.assert_awaited_once_with(subject="alice")
    directory.realm_roles.assert_not_awaited()
    assert repository.query.call_args.args[1] == (start, end)
    mcp.call.assert_not_awaited()
