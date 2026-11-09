"""提取已验证 claims 中的 realm 角色；不执行任何 token 校验。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence


def realm_roles_from_claims(claims: Mapping[str, object]) -> list[str]:
    """读取 realm_access.roles，忽略缺失或类型错误的字段。

    接受列表或元组等序列（不接受字符串、bytes）；丢弃非字符串和纯空白元素，
    保留其他字符串的原值、顺序和重复。不读取 resource_access 或 groups。
    调用方须先完成签名、issuer、audience、有效期及适用的 nonce 校验。
    """
    realm_access = claims.get("realm_access")
    if not isinstance(realm_access, Mapping):
        return []
    roles = realm_access.get("roles")
    if not isinstance(roles, Sequence) or isinstance(roles, (str, bytes, bytearray)):
        return []
    return [role for role in roles if isinstance(role, str) and role.strip()]
