from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from .storage import atomic_write_json, read_json


class ContactDirectory:
    """Local aliases and notes, plus observed QQ nicknames; never member identity."""

    def __init__(self, path: Path) -> None:
        self._path = path
        raw = read_json(path, {"format": 1, "profiles": {}})
        if not isinstance(raw, dict) or not isinstance(raw.get("profiles"), dict):
            raise ValueError(f"invalid contact profile store: {path}")  # noqa: TRY004
        self._profiles: dict[str, dict[str, object]] = raw["profiles"]

    def get(self, contact_id: str) -> dict[str, object]:
        profile = self._profiles.get(contact_id, {})
        return {
            "found": contact_id in self._profiles,
            "contact_id": contact_id,
            "display_name": profile.get("display_name", ""),
            "nickname": profile.get("nickname", ""),
            "notes": profile.get("notes", ""),
            "updated_at": profile.get("updated_at", ""),
        }

    def observe_nickname(self, contact_id: str, nickname: str) -> None:
        if not nickname.strip():
            return
        profile = self._profiles.setdefault(contact_id, {})
        nickname = nickname.strip()[:128]
        if profile.get("nickname") != nickname:
            profile["nickname"] = nickname
            profile["updated_at"] = datetime.now(UTC).isoformat()
            self._save()

    def update(
        self,
        contact_id: str,
        display_name: str | None,
        notes: str | None,
    ) -> dict[str, object]:
        profile = self._profiles.setdefault(contact_id, {})
        if display_name is not None:
            profile["display_name"] = display_name.strip()[:128]
        if notes is not None:
            profile["notes"] = notes.strip()[:4000]
        profile["updated_at"] = datetime.now(UTC).isoformat()
        self._save()
        return self.get(contact_id)

    def page(
        self,
        *,
        query: str = "",
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, object]:
        if page < 1:
            raise ValueError("page must be at least 1")
        if page_size < 1:
            raise ValueError("page_size must be at least 1")
        needle = query.strip().casefold()
        items = [self.get(contact_id) for contact_id in self._profiles]
        if needle:
            items = [
                item
                for item in items
                if needle
                in " ".join(
                    (
                        str(item["contact_id"]),
                        str(item["display_name"]),
                        str(item["nickname"]),
                        str(item["notes"]),
                    )
                ).casefold()
            ]
        items.sort(
            key=lambda item: (
                str(item["display_name"] or item["nickname"]).casefold(),
                str(item["contact_id"]),
            )
        )
        total = len(items)
        total_pages = (total + page_size - 1) // page_size if total else 0
        start = (page - 1) * page_size
        end = start + page_size
        return {
            "kind": "profile",
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
            "has_more": end < total,
            "items": items[start:end],
        }

    def remove(self, contact_id: str) -> bool:
        if contact_id not in self._profiles:
            return False
        del self._profiles[contact_id]
        self._save()
        return True

    def _save(self) -> None:
        atomic_write_json(
            self._path,
            {"format": 1, "profiles": self._profiles},
        )


class RoleStore:
    """Authorization roles. Only the operator CLI may modify this file."""

    def __init__(self, path: Path) -> None:
        self._path = path
        raw = read_json(path, {"format": 1, "roles": {}})
        if not isinstance(raw, dict) or not isinstance(raw.get("roles"), dict):
            raise ValueError(f"invalid role store: {path}")  # noqa: TRY004
        self._roles: dict[str, dict[str, str]] = raw["roles"]

    def role_of(self, contact_id: str) -> str:
        record = self._roles.get(contact_id)
        return str(record.get("role", "user")) if record else "user"

    def is_admin(self, contact_id: str) -> bool:
        return self.role_of(contact_id) == "admin"

    def set_admin(self, contact_id: str, enabled: bool) -> None:
        if enabled:
            self._roles[contact_id] = {
                "role": "admin",
                "updated_at": datetime.now(UTC).isoformat(),
            }
        else:
            self._roles.pop(contact_id, None)
        self._save()

    def list_roles(self) -> list[dict[str, str]]:
        return [
            {
                "contact_id": contact_id,
                "role": str(record.get("role", "user")),
                "updated_at": str(record.get("updated_at", "")),
            }
            for contact_id, record in sorted(self._roles.items())
        ]

    def _save(self) -> None:
        atomic_write_json(self._path, {"format": 1, "roles": self._roles})
