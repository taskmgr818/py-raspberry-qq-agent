from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest

from py_raspberry_qq_agent.assignments import AssignmentNotifications
from py_raspberry_qq_agent.guild import GuildConfig
from py_raspberry_qq_agent.keycloak import User


@pytest.mark.asyncio
async def test_assignment_notifications_are_independent_of_due_reminders() -> None:
    repository = AsyncMock()
    repository.query.return_value = [
        {"id": 1, "title": "First task", "subject": "alice", "role": "leader"},
        {"id": 2, "title": "Second task", "subject": "bob", "role": "member"},
    ]
    directory = AsyncMock()
    directory.user.side_effect = [
        User("alice", "Alice", "12345"),
        User("bob", "Bob", "54321"),
    ]
    outbound = Mock()
    service = AssignmentNotifications(
        GuildConfig(due_enabled=False), repository, directory, outbound
    )
    start = datetime(2026, 10, 25, tzinfo=UTC)
    end = start + timedelta(minutes=1)
    await service.check(start, end)
    assert repository.query.call_args.args[1] == (start, end)
    assert outbound.enqueue_notification.call_count == 2
    first, second = outbound.enqueue_notification.call_args_list
    assert first.args == ("private", "12345", "你已被指定为任务的负责人：First task")
    assert second.args == ("private", "54321", "你已被指定为任务的成员：Second task")
    repository.due.assert_not_awaited()
