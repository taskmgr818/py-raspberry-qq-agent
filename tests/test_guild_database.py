from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
import pytest_asyncio

from py_raspberry_qq_agent.guild import GuildRepository


@pytest_asyncio.fixture
async def database():
    dsn = os.environ.get("TEST_GUILD_DATABASE_URL")
    if not dsn:
        pytest.skip(
            "TEST_GUILD_DATABASE_URL is not set (requires a migrated test database)"
        )
    subject = "bot-test-" + uuid.uuid4().hex
    repository = GuildRepository(dsn)
    await repository.initialize()
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        await conn.execute("INSERT INTO users(subject) VALUES (%s)", (subject,))
        try:
            yield conn, repository, subject
        finally:
            await conn.execute(
                "DELETE FROM qq_agent_guild_commands WHERE starts_with(subject,%s)",
                (subject,),
            )
            await conn.execute(
                "DELETE FROM nodes WHERE starts_with(created_by,%s)", (subject,)
            )
            await conn.execute(
                "DELETE FROM users WHERE starts_with(subject,%s)", (subject,)
            )


@pytest.mark.asyncio
async def test_due_query_is_half_open_and_excludes_completed_members_and_overdue(
    database,
) -> None:
    conn, repository, subject = database
    start = datetime(2026, 10, 25, 12, tzinfo=UTC)
    end = start + timedelta(minutes=1)
    advance = timedelta(days=1)
    ids = []
    for index, (due, completed, role) in enumerate(
        [
            (start + advance, None, "leader"),
            (end + advance, None, "leader"),
            (start + advance, start, "leader"),
            (start + advance, None, "member"),
            (start - timedelta(days=1), None, "leader"),
        ]
    ):
        cursor = await conn.execute(
            "INSERT INTO nodes(title,due_at,completed_at,created_by) VALUES (%s,%s,%s,%s) RETURNING id",
            ("Window test " + str(index), due, completed, subject),
        )
        node_id = (await cursor.fetchone())[0]
        ids.append(node_id)
        await conn.execute(
            "INSERT INTO node_members(node_id,subject,role) VALUES (%s,%s,%s)",
            (node_id, subject, role),
        )
    first = await repository.due(start, end, advance)
    second = await repository.due(end, end + timedelta(minutes=1), advance)
    assert [row["id"] for row in first if row["subject"] == subject] == [ids[0]]
    assert [row["id"] for row in second if row["subject"] == subject] == [ids[1]]
    # Even a delayed query never turns into an overdue reminder.
    delayed = await repository.due(start - timedelta(days=3), end, advance)
    assert ids[4] not in [row["id"] for row in delayed]


@pytest.mark.asyncio
async def test_assignment_windows_include_only_current_role_changes(database) -> None:
    from unittest.mock import AsyncMock, Mock

    from py_raspberry_qq_agent.assignments import AssignmentNotifications
    from py_raspberry_qq_agent.guild import GuildConfig
    from py_raspberry_qq_agent.keycloak import User

    conn, repository, subject = database
    start = datetime(2026, 10, 25, 12, tzinfo=UTC)
    end = start + timedelta(minutes=1)
    cursor = await conn.execute(
        "INSERT INTO nodes(title,created_by) VALUES ('Assignment boundary',%s) RETURNING id",
        (subject,),
    )
    node_id = (await cursor.fetchone())[0]
    await conn.execute(
        "INSERT INTO node_members(node_id,subject,role,created_at) VALUES (%s,%s,'member',%s)",
        (node_id, subject, end),
    )
    directory = AsyncMock()
    directory.lookup_user.return_value = User(subject, "Test", "12345")
    outbound = Mock()
    service = AssignmentNotifications(GuildConfig(), repository, directory, outbound)
    await service.check(start, end)
    outbound.enqueue_notification.assert_not_called()
    await service.check(end, end + timedelta(minutes=1))
    outbound.enqueue_notification.assert_called_once()
    assert "成员" in outbound.enqueue_notification.call_args.args[2]
    outbound.reset_mock()
    # A role promotion moves the assignment to the next window; unchanged saves do not.
    await conn.execute("DELETE FROM node_members WHERE node_id=%s", (node_id,))
    await conn.execute(
        "INSERT INTO node_members(node_id,subject,role,created_at) VALUES (%s,%s,'leader',%s)",
        (node_id, subject, end + timedelta(minutes=1)),
    )
    await service.check(end + timedelta(minutes=1), end + timedelta(minutes=2))
    outbound.enqueue_notification.assert_called_once()
    assert "负责人" in outbound.enqueue_notification.call_args.args[2]


def guild_service(repository, subject):
    from unittest.mock import AsyncMock
    from zoneinfo import ZoneInfo

    from py_raspberry_qq_agent.guild import GuildConfig, GuildService
    from py_raspberry_qq_agent.keycloak import User

    directory = AsyncMock()
    people = {
        subject: User(subject, "Creator", "12345"),
        subject + "-alice": User(subject + "-alice", "Alice", "23456"),
        subject + "-bob": User(subject + "-bob", "Bob", None),
        subject + "-manager": User(subject + "-manager", "Manager", "34567"),
    }

    async def lookup(*, qq=None, subject=None, refresh=False):
        return (
            people.get(subject)
            if subject is not None
            else next((user for user in people.values() if user.qq == qq), None)
        )

    directory.lookup_user.side_effect = lookup
    directory.realm_roles.side_effect = lambda sub, **kw: [
        "permission-system-admin"
        if sub == subject
        else "permission-techdept-admin"
        if sub.endswith("-manager")
        else "permission-techdept-access"
    ]
    service = GuildService(
        GuildConfig(public_url="https://guild.example.com"),
        repository,
        directory,
        ZoneInfo("Asia/Shanghai"),
    )
    return service


def guild_message_task(sender="12345", message_id=None):
    from py_raspberry_qq_agent.tasks import AgentTask

    return AgentTask(
        "Create",
        source="incoming_message",
        sender_id=sender,
        chat_type="group",
        target_id="99999",
        metadata={
            "message_id": message_id or uuid.uuid4().hex,
        },
    )


@pytest.mark.asyncio
async def test_creation_provisions_all_users_records_history_and_replays_across_instances(
    database,
):
    conn, repository, subject = database
    await conn.execute("DELETE FROM users WHERE subject=%s", (subject,))
    service = guild_service(repository, subject)
    task = guild_message_task()
    payload = {
        "parent_id": None,
        "title": "Root",
        "description": "Initial description",
        "tags": ["tag"],
        "due_at": "2026-12-01T12:00:00+08:00",
        "leader_subjects": [subject + "-bob"],
        "member_subjects": [subject + "-alice"],
    }
    node = await service.create_node(payload, task)
    assert node["created_by"] == subject
    assert node["creator"]["name"] == "Creator"
    assert node["members"] == [
        {"subject": subject + "-bob", "role": "leader", "name": "Bob"},
        {"subject": subject + "-alice", "role": "member", "name": "Alice"},
    ]
    assert node["due_at"] == "2026-12-01T04:00:00+00:00"
    assert (
        await (
            await conn.execute(
                "SELECT count(*) FROM users WHERE starts_with(subject,%s)", (subject,)
            )
        ).fetchone()
    )[0] == 3
    history = await (
        await conn.execute(
            "SELECT author_subject,old_description,new_description,created_at FROM node_description_changes WHERE node_id=%s",
            (node["id"],),
        )
    ).fetchone()
    assert history[:3] == (subject, "", "Initial description")
    assert history[3].isoformat() == node["created_at"]
    restarted = guild_service(GuildRepository(repository._dsn), subject)
    replay = await restarted.create_node(payload, task)
    assert replay["id"] == node["id"] and replay["replayed"] is True
    assert (
        await (
            await conn.execute(
                "SELECT count(*) FROM nodes WHERE created_by=%s", (subject,)
            )
        ).fetchone()
    )[0] == 1
    with pytest.raises(ValueError, match="新消息"):
        await restarted.create_node({**payload, "title": "Changed"}, task)
    # A lost response must not recreate a node even if it was subsequently deleted.
    await conn.execute("DELETE FROM nodes WHERE id=%s", (node["id"],))
    assert (await restarted.create_node(payload, task))["replayed"]
    assert (
        await (
            await conn.execute(
                "SELECT count(*) FROM nodes WHERE created_by=%s", (subject,)
            )
        ).fetchone()
    )[0] == 0


@pytest.mark.asyncio
async def test_simultaneous_retries_commit_one_node(database):
    import asyncio

    conn, repository, subject = database
    service = guild_service(repository, subject)
    task = guild_message_task()
    first, second = await asyncio.gather(
        service.create_node({"parent_id": None, "title": "Once"}, task),
        service.create_node({"parent_id": None, "title": "Once"}, task),
    )
    assert first["id"] == second["id"]
    assert {first["replayed"], second["replayed"]} == {False, True}
    assert (
        await (
            await conn.execute(
                "SELECT count(*) FROM nodes WHERE created_by=%s", (subject,)
            )
        ).fetchone()
    )[0] == 1


@pytest.mark.asyncio
async def test_creation_follows_root_and_inherited_leader_permissions(database):
    conn, repository, subject = database
    service = guild_service(repository, subject)
    root = await service.create_node(
        {
            "parent_id": None,
            "title": "Root",
            "leader_subjects": [subject + "-alice"],
            "member_subjects": [subject + "-manager"],
        },
        guild_message_task(),
    )
    parent = await service.create_node(
        {"title": "Parent", "parent_id": root["id"]}, guild_message_task()
    )
    await conn.execute(
        "UPDATE nodes SET updated_at='2020-01-01' WHERE id=%s", (parent["id"],)
    )
    child = await service.create_node(
        {"title": "Inherited", "parent_id": parent["id"]}, guild_message_task("23456")
    )
    assert child["created_by"] == subject + "-alice"
    assert child["members"][0]["subject"] == subject + "-alice"
    assert (
        await (
            await conn.execute(
                "SELECT updated_at > '2020-01-01' FROM nodes WHERE id=%s",
                (parent["id"],),
            )
        ).fetchone()
    )[0]
    with pytest.raises(PermissionError, match="manager"):
        await service.create_node(
            {"parent_id": None, "title": "Forbidden root"}, guild_message_task("23456")
        )
    # A department manager who is only a member of the parent cannot add a child.
    with pytest.raises(PermissionError, match="有效负责人"):
        await service.create_node(
            {"title": "Forbidden child", "parent_id": parent["id"]},
            guild_message_task("34567"),
        )
    manager_root = await service.create_node(
        {"parent_id": None, "title": "Manager root"}, guild_message_task("34567")
    )
    assert manager_root["members"][0]["subject"] == subject + "-manager"
    with pytest.raises(LookupError):
        await service.create_node(
            {"title": "Missing parent", "parent_id": 9223372036854775807},
            guild_message_task(),
        )


@pytest.mark.asyncio
async def test_failed_transaction_rolls_back_users_nodes_and_receipts(database):
    from bitnp_authz import SitePermissions

    conn, repository, subject = database
    # Force a late SQL failure after both the user and node inserts succeeded.
    payload = {
        "title": "Rollback",
        "description": "",
        "due_at": None,
        "parent_id": None,
        "tags": [],
        "leader_subjects": [subject, subject + "-bob"],
        "member_subjects": [subject + "-bob"],
    }
    permissions = SitePermissions.from_roles(["permission-system-admin"])
    with pytest.raises(psycopg.errors.UniqueViolation):
        await repository.create_node(
            subject, permissions, payload, "rollback-" + subject
        )
    for table, column in (
        ("nodes", "created_by"),
        ("qq_agent_guild_commands", "subject"),
    ):
        assert (
            await (
                await conn.execute(
                    f"SELECT count(*) FROM {table} WHERE {column}=%s", (subject,)
                )
            ).fetchone()
        )[0] == 0
    assert (
        await (
            await conn.execute(
                "SELECT count(*) FROM users WHERE subject=%s", (subject + "-bob",)
            )
        ).fetchone()
    )[0] == 0
    payload["member_subjects"] = []
    node = await repository.create_node(
        subject, permissions, payload, "rollback-" + subject
    )
    assert not node["replayed"]


@pytest.mark.asyncio
async def test_node_queries_distinguish_direct_inherited_and_system_todos(database):
    conn, repository, subject = database
    service = guild_service(repository, subject)
    root = await service.create_node(
        {"parent_id": None, "title": "Root"}, guild_message_task()
    )
    child = await service.create_node(
        {
            "title": "Child",
            "parent_id": root["id"],
            "leader_subjects": [subject + "-alice"],
            "member_subjects": [subject + "-manager"],
        },
        guild_message_task(),
    )
    leaf = await service.create_node(
        {"title": "Leaf", "parent_id": child["id"]}, guild_message_task()
    )
    overdue = await service.create_node(
        {
            "parent_id": None,
            "title": "Overdue",
            "due_at": "2020-01-01T00:00:00Z",
            "leader_subjects": [subject + "-alice"],
        },
        guild_message_task(),
    )
    completed = await service.create_node(
        {"parent_id": None, "title": "Completed"}, guild_message_task()
    )
    await conn.execute(
        "UPDATE nodes SET completed_at=now() WHERE id=%s", (completed["id"],)
    )
    task = guild_message_task("23456")
    assert {n["id"] for n in (await service.list_nodes({}, task))["items"]} == {
        child["id"],
        overdue["id"],
    }
    assert {
        n["id"]
        for n in (await service.list_nodes({"include_inherited": True}, task))["items"]
    } == {root["id"], child["id"], leaf["id"], overdue["id"]}
    # An ordinary parent member gains membership upward, not downward.
    manager = guild_message_task("34567")
    assert {
        n["id"]
        for n in (await service.list_nodes({"include_inherited": True}, manager))[
            "items"
        ]
    } == {root["id"], child["id"]}
    global_todos = await service.list_nodes({"scope": "all", "page_size": 1}, task)
    assert global_todos["items"][0]["id"] == overdue["id"]
    assert global_todos["has_more"] and global_todos["next_page"] == 2
    assert (await service.list_nodes({"overdue": True}, task))["items"][0][
        "id"
    ] == overdue["id"]
    roots = await service.list_nodes(
        {"scope": "all", "parent_id": None, "status": "all"}, task
    )
    assert {n["id"] for n in roots["items"]} == {
        root["id"],
        overdue["id"],
        completed["id"],
    }
    assert (await service.list_nodes({"scope": "all", "status": "completed"}, task))[
        "items"
    ][0]["id"] == completed["id"]
    assert (
        await service.list_nodes({"scope": "user", "subject": subject + "-bob"}, task)
    )["items"] == []
    assert (await service.list_nodes({"query": "' OR true --"}, task))["items"] == []
    detail = await service.get_node({"node_id": child["id"]}, task)
    assert detail["members"][0]["name"] == "Alice"


@pytest.mark.asyncio
async def test_navigation_disambiguates_same_titles_and_keeps_lists_compact(database):
    conn, repository, subject = database
    service = guild_service(repository, subject)
    roots, children = [], []
    for title in ("机房设备", "活动设备"):
        root = await service.create_node(
            {"title": title, "parent_id": None}, guild_message_task()
        )
        roots.append(root)
        children.append(
            await service.create_node(
                {
                    "title": "日常维护",
                    "parent_id": root["id"],
                    "description": title + "的维护职责" + "x" * 10000,
                    "leader_subjects": [subject + "-alice"],
                    "member_subjects": [subject + "-manager", subject + "-bob"],
                },
                guild_message_task(),
            )
        )
    # The most urgent node is not necessarily first on the board.
    await conn.execute(
        "UPDATE nodes SET completed_at=now(), due_at='2020-01-01', sort_order=100 WHERE id=%s",
        (roots[0]["id"],),
    )
    task = guild_message_task("23456")  # Does not directly participate in the roots.
    args = {"scope": "all", "status": "all", "parent_id": None, "sort": "position", "page_size": 1}
    page = await service.list_nodes(args, task)
    assert page["items"][0]["id"] == roots[1]["id"]
    assert page["has_more"] and page["next_page"] == 2
    page = await service.list_nodes({**args, "page": 2}, task)
    assert page["items"][0]["id"] == roots[0]["id"]
    assert page["items"][0]["completed_at"] is not None
    assert page["items"][0]["child_count"] == 1
    assert not page["has_more"]

    found = await service.list_nodes(
        {"scope": "all", "status": "all", "query": "日常维护"}, task
    )
    for row, root, child in zip(found["items"], roots, children, strict=True):
        assert row["path"] == [
            {"id": root["id"], "title": root["title"]},
            {"id": child["id"], "title": "日常维护"},
        ]
        assert row["child_count"] == 0
        assert row["leaders"] == [{"subject": subject + "-alice", "name": "Alice"}]
        assert row["member_count"] == 2
        assert not {"description", "members", "created_by", "creator", "updated_at"} & row.keys()
        detail = await service.get_node({"node_id": child["id"]}, task)
        assert detail["path"] == row["path"]
        assert detail["child_count"] == 0
        assert len(detail["description"]) > 10000
        assert len(detail["members"]) == 3
        siblings = await service.list_nodes(
            {"scope": "all", "status": "all", "parent_id": root["id"]}, task
        )
        assert [n["id"] for n in siblings["items"]] == [child["id"]]


@pytest.mark.asyncio
async def test_recent_activity_combines_persistent_sources_with_pagination(database):
    import json

    conn, repository, subject = database
    service = guild_service(repository, subject)
    node = await service.create_node(
        {"parent_id": None, "title": "Task", "description": "Initial"}, guild_message_task()
    )
    await conn.execute(
        "INSERT INTO comments(node_id,author_subject,body) VALUES (%s,%s,%s)",
        (node["id"], subject, "x" * 1500),
    )
    task = guild_message_task()
    result = await service.list_activity({"node_id": node["id"]}, task)
    assert {item["kind"] for item in result["items"]} == {
        "node_created",
        "comment",
        "description_change",
    }
    assert all(item["author"]["name"] == "Creator" for item in result["items"])
    assert (
        len(next(item["body"] for item in result["items"] if item["kind"] == "comment"))
        == 1000
    )
    json.dumps(result)
    pages = [
        await service.list_activity({"page_size": 1, "page": i}, task)
        for i in range(1, 4)
    ]
    assert (
        len({(page["items"][0]["kind"], page["items"][0]["id"]) for page in pages}) == 3
    )
    assert not pages[-1]["has_more"]
    assert (await service.list_activity({"since": "2999-01-01T00:00:00Z"}, task))[
        "items"
    ] == []
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        await repository.query("INSERT INTO users(subject) VALUES ('must-not-write')")
