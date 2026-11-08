from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx


@dataclass(slots=True)
class KeycloakConfig:
    issuer: str = ""
    admin_url: str = ""
    client_id: str = ""
    client_secret: str = ""
    qq_attribute: str = "qq_number"
    enabled: bool = False


@dataclass(slots=True)
class User:
    subject: str
    name: str
    qq: str | None


class AmbiguousQQBinding(ValueError):
    """Multiple enabled members have the same QQ binding."""


class KeycloakDirectory:
    """Read-only identity lookup. QQ bindings are managed by administrators."""

    def __init__(self, config: KeycloakConfig) -> None:
        self._config = config
        self._http = httpx.AsyncClient(timeout=10)
        self._token = ""
        self._expires_at = 0.0
        self._token_lock = asyncio.Lock()
        self._cache: dict[tuple[str, str], tuple[float, Any]] = {}

    async def close(self) -> None:
        await self._http.aclose()

    async def _access_token(self) -> str:
        async with self._token_lock:
            return await self._refresh_token()

    async def _refresh_token(self) -> str:
        if self._token and time.monotonic() < self._expires_at:
            return self._token
        response = await self._http.post(
            self._config.issuer.rstrip("/") + "/protocol/openid-connect/token",
            auth=(self._config.client_id, self._config.client_secret),
            data={"grant_type": "client_credentials"},
        )
        response.raise_for_status()
        data = response.json()
        self._token = data["access_token"]
        ttl = float(data.get("expires_in", 60))
        self._expires_at = time.monotonic() + ttl - min(30, ttl / 2)
        return self._token

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = self._config.admin_url.rstrip("/") + path
        for attempt in range(2):
            token = await self._access_token()
            response = await self._http.get(
                url, params=params, headers={"Authorization": "Bearer " + token}
            )
            if response.status_code != 401 or attempt:
                break
            self._token = ""
        response.raise_for_status()
        return response.json()

    def _store(self, key: tuple[str, str], value: Any) -> None:
        now = time.monotonic()
        if len(self._cache) >= 2048:
            self._cache = {k: v for k, v in self._cache.items() if now < v[0]}
            if len(self._cache) >= 2048:
                self._cache.clear()
        self._cache[key] = (now + (30 if value is None else 300), value)

    def _parse_user(self, subject: str, data: dict[str, Any]) -> User | None:
        if not data.get("enabled", True):
            return None
        name = " ".join(
            part.strip()
            for part in [data.get("firstName"), data.get("lastName")]
            if part and part.strip()
        )
        qq = (data.get("attributes") or {}).get(self._config.qq_attribute, [])
        # Keycloak attributes are arrays; ambiguous bindings are not used.
        qq = str(qq[0]).strip() if isinstance(qq, list) and len(qq) == 1 else ""
        return User(
            subject,
            name or data.get("username") or subject,
            qq if re.fullmatch(r"[1-9][0-9]{4,19}", qq) else None,
        )

    async def user(self, subject: str) -> User | None:
        key = ("subject", subject)
        cached = self._cache.get(key)
        if cached and time.monotonic() < cached[0]:
            return cached[1]
        try:
            data = await self._get("/users/" + quote(subject, safe=""))
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
            user = None
        else:
            user = self._parse_user(subject, data)
        self._store(key, user)
        return user

    async def user_by_qq(self, qq: str) -> User | None:
        qq = qq.strip()
        if not re.fullmatch(r"[1-9][0-9]{4,19}", qq):
            raise ValueError(
                "QQ must be a string of 5–20 ASCII digits without leading zero"
            )
        key = ("qq", qq)
        cached = self._cache.get(key)
        if cached and time.monotonic() < cached[0]:
            return cached[1]
        matches: dict[str, User] = {}
        first = 0
        while True:
            data = await self._get(
                "/users",
                {
                    "q": f"{self._config.qq_attribute}:{qq}",
                    "enabled": "true",
                    "briefRepresentation": "false",
                    "first": first,
                    "max": 100,
                },
            )
            for item in data:
                user = self._parse_user(item["id"], item)
                if user is not None and user.qq == qq:
                    matches[user.subject] = user
                    if len(matches) > 1:
                        raise AmbiguousQQBinding(
                            "QQ is bound to multiple enabled members"
                        )
            if len(data) < 100:
                break
            first += len(data)
        user = next(iter(matches.values()), None)
        self._store(key, user)
        if user is not None:
            self._store(("subject", user.subject), user)
        return user

    async def realm_roles(self, subject: str) -> list[str]:
        key = ("roles", subject)
        cached = self._cache.get(key)
        if cached and time.monotonic() < cached[0]:
            return list(cached[1])
        data = await self._get(
            "/users/" + quote(subject, safe="") + "/role-mappings/realm/composite"
        )
        roles = sorted({role["name"] for role in data if role.get("name")})
        self._store(key, roles)
        return list(roles)
