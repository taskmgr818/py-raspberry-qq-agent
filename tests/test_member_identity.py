from __future__ import annotations

import json
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from py_raspberry_qq_agent.config import AppConfig, ConfigError
from py_raspberry_qq_agent.contacts import ContactDirectory, RoleStore
from py_raspberry_qq_agent.keycloak import (
    AmbiguousQQBinding,
    KeycloakConfig,
    KeycloakDirectory,
    User,
)
from py_raspberry_qq_agent.toolset import build_tool_registry


def registry_for(config: AppConfig, directory=None):
    return build_tool_registry(
        config=config,
        schedules=Mock(),
        knowledge=Mock(),
        contacts=ContactDirectory(config.data_dir / "profiles.json"),
        outbound=Mock(),
        history=Mock(),
        chat_directory=Mock(),
        mcp=Mock(tools=[]),
        identities=directory,
    )


async def mock_directory(handler) -> KeycloakDirectory:
    directory = KeycloakDirectory(
        KeycloakConfig(
            issuer="https://id.example/realms/test",
            admin_url="https://id.example/admin/realms/test",
        )
    )
    await directory._http.aclose()

    def dispatch(request):
        if request.url.path.endswith("/token"):
            return httpx.Response(
                200, json={"access_token": "token", "expires_in": 300}
            )
        return handler(request)

    directory._http = httpx.AsyncClient(transport=httpx.MockTransport(dispatch))
    return directory


def member(subject="alice", qq=None, enabled=True):
    return {
        "id": subject,
        "firstName": "Alice",
        "lastName": "Example",
        "enabled": enabled,
        "attributes": {"qq_number": qq if qq is not None else ["12345"]},
    }


@pytest.mark.asyncio
async def test_qq_lookup_filters_exact_single_enabled_binding_and_reuses_cache():
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.params["q"] == "qq_number:12345"
        assert request.url.params["enabled"] == "true"
        assert request.url.params["briefRepresentation"] == "false"
        return httpx.Response(
            200,
            json=[
                member("disabled", enabled=False),
                member("multi", ["12345", "67890"]),
                member("different", ["123456"]),
                member(),
            ],
        )

    directory = await mock_directory(handler)
    try:
        user = await directory.lookup_user(qq="12345")
        assert user == User("alice", "Alice Example", "12345")
        assert await directory.lookup_user(qq="12345") == user
        assert await directory.lookup_user(subject="alice") == user
        assert len(calls) == 1
    finally:
        await directory.close()


@pytest.mark.asyncio
async def test_qq_lookup_checks_later_pages_for_conflicts():
    pages = []

    def handler(request):
        first = int(request.url.params["first"])
        pages.append(first)
        return httpx.Response(
            200,
            json=(
                [member()] + [member(str(i), ["99999"]) for i in range(99)]
                if first == 0
                else [member("second")]
            ),
        )

    directory = await mock_directory(handler)
    try:
        with pytest.raises(AmbiguousQQBinding):
            await directory.lookup_user(qq="12345")
        assert pages == [0, 100]
    finally:
        await directory.close()


@pytest.mark.asyncio
async def test_missing_binding_is_cached_but_server_failure_is_not():
    statuses = [503, 200]
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(statuses.pop(0), json=[])

    directory = await mock_directory(handler)
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await directory.lookup_user(qq="12345")
        assert await directory.lookup_user(qq="12345") is None
        assert await directory.lookup_user(qq="12345") is None
        assert len(calls) == 2
    finally:
        await directory.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [
        {},
        {"qq": "12345", "subject": "alice"},
        {"qq": 12345},
        {"qq": "１２３４５"},
        {"qq": "012345"},
        {"qq": "1234"},
        {"qq": "12345 other:value"},
        {"subject": ""},
        {"subject": "  "},
        {"subject": 12345},
    ],
)
async def test_directory_rejects_invalid_lookup_before_http_request(args):
    directory = await mock_directory(lambda request: pytest.fail("unexpected request"))
    try:
        with pytest.raises(ValueError):
            await directory.lookup_user(**args)
    finally:
        await directory.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("disabled", [False, True])
async def test_subject_missing_or_disabled_is_cached_but_server_failure_is_not(disabled):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.path.endswith("/users/alice")
        if len(calls) == 1:
            return httpx.Response(503)
        if disabled:
            return httpx.Response(200, json=member(enabled=False))
        return httpx.Response(404)

    directory = await mock_directory(handler)
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await directory.lookup_user(subject="alice")
        assert await directory.lookup_user(subject="alice") is None
        assert await directory.lookup_user(subject="alice") is None
        assert len(calls) == 2
    finally:
        await directory.close()


@pytest.mark.asyncio
async def test_subject_lookup_does_not_bypass_qq_binding_conflict_checks():
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path.endswith("/users/alice"):
            return httpx.Response(200, json=member())
        assert request.url.path.endswith("/users")
        return httpx.Response(200, json=[member(), member("second")])

    directory = await mock_directory(handler)
    try:
        assert await directory.lookup_user(subject=" alice ") == User(
            "alice", "Alice Example", "12345"
        )
        with pytest.raises(AmbiguousQQBinding):
            await directory.lookup_user(qq=" 12345 ")
        assert len(calls) == 2
    finally:
        await directory.close()


@pytest.mark.asyncio
async def test_effective_roles_refresh_token_and_cache_without_exposing_mutation():
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.path.endswith("/users/alice/role-mappings/realm/composite")
        if len(calls) == 1:
            return httpx.Response(401)
        return httpx.Response(
            200,
            json=[{"name": "position-clinic-head"}, {"name": "position-clinic-member"}],
        )

    directory = await mock_directory(handler)
    try:
        roles = await directory.realm_roles("alice")
        roles.clear()
        assert await directory.realm_roles("alice") == [
            "position-clinic-head",
            "position-clinic-member",
        ]
        assert len(calls) == 2
    finally:
        await directory.close()


@pytest.mark.asyncio
async def test_member_tool_keeps_local_alias_and_bot_authorization_separate(config):
    contacts = ContactDirectory(config.data_dir / "profiles.json")
    contacts.update("12345", "阿甲", "permission-system-admin is just a note")
    directory = AsyncMock()
    directory.lookup_user.return_value = User("alice-sub", "Alice Example", "12345")
    directory.realm_roles.return_value = [
        "position-techdept-member",
        "position-techdept-head",
        "permission-system-admin",
    ]
    registry = registry_for(config, directory)
    ref = registry.ids.encode({"sender_id": "12345"})["sender_id"]
    result = await registry.execute("get_member_identity", {"contact_id": ref})
    assert result["status"] == "found"
    assert result["contact_id"] == ref
    assert result["name"] == "Alice Example"
    assert result["subject"] == "alice-sub"
    assert result["positions"] == [
        {
            "code": "position-techdept-head",
            "label": "技术部部长",
            "scope": "department",
            "department": "techdept",
        }
    ]
    assert "12345" not in json.dumps(result)
    assert await registry.execute("get_member_identity", {"qq": "12345"}) == result
    assert await registry.execute(
        "get_member_identity", {"subject": result["subject"]}
    ) == result
    local = await registry.execute("get_contact_profile", {"contact_id": ref})
    assert local["display_name"] == "阿甲"
    assert "subject" not in local and "realm_roles" not in local
    assert not RoleStore(config.data_dir / "roles.json").is_admin("12345")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("identifier", "status"),
    [
        ("qq", "not_found"),
        ("qq", "ambiguous"),
        ("qq", "unavailable"),
        ("qq", "disabled"),
        ("subject", "not_found"),
        ("subject", "unavailable"),
        ("subject", "disabled"),
    ],
)
async def test_member_tool_distinguishes_lookup_outcomes(config, identifier, status):
    directory = AsyncMock()
    directory.lookup_user.return_value = None
    if status == "ambiguous":
        directory.lookup_user.side_effect = AmbiguousQQBinding()
    elif status == "unavailable":
        directory.lookup_user.side_effect = httpx.ConnectError("secret-bearing URL")
    registry = registry_for(config, None if status == "disabled" else directory)
    value = "alice" if identifier == "subject" else "12345"
    result = await registry.execute("get_member_identity", {identifier: value})
    assert result == {"status": status, "source": "keycloak"}
    directory.realm_roles.assert_not_awaited()
    if status == "disabled":
        directory.lookup_user.assert_not_awaited()
    else:
        directory.lookup_user.assert_awaited_once_with(**{identifier: value})


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [{"qq": "12345"}, {"subject": "alice"}])
async def test_role_lookup_failure_does_not_claim_empty_positions(config, args):
    directory = AsyncMock()
    directory.lookup_user.return_value = User("alice", "Alice", "12345")
    directory.realm_roles.side_effect = httpx.ConnectError("offline")
    result = await registry_for(config, directory).execute(
        "get_member_identity", args
    )
    assert result["status"] == "unavailable"
    assert "positions" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("qq", [[], ["67890"], ["67890", "54321"]])
async def test_member_tool_queries_subject_with_or_without_qq_binding(config, qq):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        if request.url.path.endswith("/role-mappings/realm/composite"):
            return httpx.Response(200, json=[{"name": "position-techdept-member"}])
        assert request.url.path.endswith("/users/12345")
        return httpx.Response(200, json=member("12345", qq))

    directory = await mock_directory(handler)
    try:
        registry = registry_for(config, directory)
        result = await registry.execute("get_member_identity", {"subject": "12345"})
        assert result["status"] == "found"
        assert result["subject"] == "12345"  # Even numeric subjects are not QQ refs.
        assert result["name"] == "Alice Example"
        assert result["realm_roles"] == ["position-techdept-member"]
        assert result["positions"][0]["code"] == "position-techdept-member"
        if len(qq) == 1:
            assert registry.ids.decode(result)["contact_id"] == "67890"
            assert "67890" not in json.dumps(result)
        else:
            assert result["contact_id"] is None
        assert await registry.execute(
            "get_member_identity", {"subject": "12345"}
        ) == result
        assert len(calls) == 2  # User and roles both reuse the directory's cache.
    finally:
        await directory.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [
        {},
        {"qq": "12345", "contact_id": "user:fake"},
        {"qq": 12345},
        {"qq": "１２３４５"},
        {"qq": "012345"},
        {"qq": "1234"},
        {"qq": "12345 other:value"},
        {"contact_id": "12345"},
        {"subject": ""},
        {"subject": "  "},
        {"subject": None},
        {"subject": 12345},
        {"subject": "alice", "qq": "12345"},
        {"subject": "alice", "contact_id": ""},
        {"subject": "alice", "qq": "12345", "contact_id": ""},
    ],
)
async def test_member_tool_rejects_invalid_or_invented_identifiers(config, args):
    directory = AsyncMock()
    with pytest.raises(ValueError):
        await registry_for(config, directory).execute("get_member_identity", args)
    directory.lookup_user.assert_not_awaited()


def test_keycloak_configuration_is_independent_of_guild(config, monkeypatch):
    monkeypatch.setenv("DIRECTORY_ENABLED", "true")
    with config.config_path.open("a") as output:
        output.write("""
keycloak:
  enabled: ${DIRECTORY_ENABLED}
  issuer: https://id.example/realms/test
  admin_url: https://id.example/admin/realms/test
  client_id: qq-bot
  client_secret: test-secret
""")
    loaded = AppConfig.load(config.config_path)
    assert loaded.keycloak.enabled and not loaded.guild.enabled
    loaded.keycloak.client_secret = ""
    with pytest.raises(ConfigError, match="keycloak"):
        loaded.validate()
    loaded.keycloak.enabled = False
    loaded.validate()


@pytest.mark.asyncio
@pytest.mark.parametrize("guild_enabled", [False, True])
async def test_application_shares_one_directory_with_tools_and_guild(
    config, monkeypatch, guild_enabled
):
    from py_raspberry_qq_agent import app as app_module

    config.keycloak.enabled = True
    config.guild.enabled = guild_enabled
    config.guild.weekly.enabled = False
    config.guild.due_enabled = False
    config.guild.assignments_enabled = False
    application = app_module.Application.build(config)
    directory = AsyncMock()
    factory = Mock(return_value=directory)
    registry_builder = Mock(wraps=app_module.build_tool_registry)
    weekly_factory = Mock()
    monkeypatch.setattr(app_module, "KeycloakDirectory", factory)
    monkeypatch.setattr(app_module, "build_tool_registry", registry_builder)
    monkeypatch.setattr(app_module, "WeeklyReports", weekly_factory)
    weekly_factory.return_value.stop = AsyncMock()
    monkeypatch.setattr(application.napcat.__class__, "start", Mock())
    try:
        await application.start()
        factory.assert_called_once_with(config.keycloak)
        assert registry_builder.call_args.kwargs["identities"] is directory
        if guild_enabled:
            assert weekly_factory.call_args.args[2] is directory
    finally:
        await application.stop()
    directory.close.assert_awaited_once()
