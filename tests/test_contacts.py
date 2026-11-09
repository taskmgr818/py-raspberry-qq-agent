from pathlib import Path

from py_raspberry_qq_agent.contacts import ContactDirectory, RoleStore


def test_profiles_and_roles_are_separate(tmp_path: Path) -> None:
    alias = "10001"
    profiles = ContactDirectory(tmp_path / "profiles.json")
    roles = RoleStore(tmp_path / "roles.json")

    profiles.update(alias, "Known user", "Helps with events")
    assert profiles.get(alias)["display_name"] == "Known user"
    assert roles.role_of(alias) == "user"

    roles.set_admin(alias, True)
    assert roles.is_admin(alias)
    assert profiles.get(alias)["notes"] == "Helps with events"


def test_profiles_support_non_friends_search_and_pagination(tmp_path: Path) -> None:
    profiles = ContactDirectory(tmp_path / "profiles.json")
    aliases = [str(30000 + index) for index in range(5)]

    profiles.update(aliases[0], "Alice", "Infrastructure colleague")
    profiles.update(aliases[1], "Bob", "Event photographer")
    profiles.update(aliases[2], "Carol", "Infrastructure colleague")
    profiles.update(aliases[3], "Dave", "Not a QQ friend")

    first = profiles.page(page=1, page_size=2)
    second = profiles.page(page=2, page_size=2)
    found = profiles.page(query="infrastructure", page=1, page_size=10)

    assert first["total"] == 4
    assert first["total_pages"] == 2
    assert first["has_more"] is True
    assert len(first["items"]) == 2
    assert len(second["items"]) == 2
    assert found["total"] == 2
    assert all("Infrastructure" in item["notes"] for item in found["items"])

    assert profiles.remove(aliases[3]) is True
    assert profiles.get(aliases[3])["found"] is False


def test_observed_nickname_does_not_become_or_replace_local_alias(tmp_path: Path):
    profiles = ContactDirectory(tmp_path / "profiles.json")
    profiles.observe_nickname("10001", "QQ Nickname")
    assert profiles.get("10001")["display_name"] == ""
    assert profiles.get("10001")["nickname"] == "QQ Nickname"
    profiles.update("10001", "Local alias", "Local note")
    profiles.observe_nickname("10001", "Changed nickname")
    assert profiles.get("10001")["display_name"] == "Local alias"
    assert profiles.get("10001")["nickname"] == "Changed nickname"
    assert profiles.page(query="Changed nickname")["total"] == 1
    assert (
        ContactDirectory(tmp_path / "profiles.json").get("10001")["notes"]
        == "Local note"
    )
