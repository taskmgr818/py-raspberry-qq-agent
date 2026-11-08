"""由显式权限角色推导授权事实，职务仅用于展示。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .claims import realm_roles_from_claims
from .positions import PositionView, positions_from_roles
from .roles import SYSTEM_ADMIN_ROLE, Department, access_role, admin_role


@dataclass(frozen=True)
class SitePermissions:
    """权限快照；系统管理员标志不隐含填充部门集合。

    建议通过 from_roles 或 from_claims 构造，避免手工创建不一致的授权事实。
    """

    admitted: bool
    system_admin: bool
    manager: bool
    access_departments: frozenset[Department]
    managed_departments: frozenset[Department]
    positions: tuple[PositionView, ...]

    @property
    def access_departments_ordered(self) -> tuple[Department, ...]:
        """按部门规范顺序返回访问权限。"""
        return tuple(d for d in Department if d in self.access_departments)

    @property
    def managed_departments_ordered(self) -> tuple[Department, ...]:
        """按部门规范顺序返回管理权限。"""
        return tuple(d for d in Department if d in self.managed_departments)

    @classmethod
    def from_roles(cls, roles: Iterable[str]) -> SitePermissions:
        """精确匹配 realm 角色；管理角色不隐含访问角色。

        输入可为一次性迭代器。未知角色与非字符串元素被忽略；不改变输入。
        复合角色应由身份提供方展开，本库不推导任何隐含角色。
        """
        role_list = tuple(role for role in roles if isinstance(role, str))
        role_set = frozenset(role_list)
        system_admin = SYSTEM_ADMIN_ROLE in role_set
        access = frozenset(d for d in Department if access_role(d) in role_set)
        managed = frozenset(d for d in Department if admin_role(d) in role_set)
        return cls(
            admitted=system_admin or bool(access) or bool(managed),
            system_admin=system_admin,
            manager=system_admin or bool(managed),
            access_departments=access,
            managed_departments=managed,
            positions=positions_from_roles(role_list),
        )

    @classmethod
    def from_claims(cls, claims: Mapping[str, object]) -> SitePermissions:
        """从已验证的 claims 推导权限；无可用角色时返回未准入快照。"""
        return cls.from_roles(realm_roles_from_claims(claims))
