"""部门定义、权限角色名与角色诊断。"""

from __future__ import annotations

from collections.abc import Iterable
from enum import Enum
from typing import Final

SYSTEM_ADMIN_ROLE: Final = "permission-system-admin"


class Department(Enum):
    """职能部门；枚举顺序也是部门结果的规范顺序。"""

    TECH = ("techdept", "技术部")
    CLINIC = ("clinic", "电脑诊所")
    MEDIA = ("digimedia", "数字媒体中心")
    TECHSVC = ("techsvc", "技术保障中心")

    @property
    def role_key(self) -> str:
        """权限角色名中的部门标识。"""
        return self.value[0]

    @property
    def label(self) -> str:
        """部门的展示名称。"""
        return self.value[1]


def access_role(department: Department) -> str:
    """返回部门访问角色名。"""
    return f"permission-{department.role_key}-access"


def admin_role(department: Department) -> str:
    """返回部门管理角色名；管理权不隐含访问权。"""
    return f"permission-{department.role_key}-admin"


_RECOGNIZED_ROLES: Final = frozenset(
    [SYSTEM_ADMIN_ROLE]
    + [role for department in Department for role in (access_role(department), admin_role(department))]
)


def recognized_roles() -> frozenset[str]:
    """返回全部可授予权限的角色名，不包含展示职务。"""
    return _RECOGNIZED_ROLES


def permission_roles(roles: Iterable[str]) -> list[str]:
    """筛选已识别的权限角色，按输入顺序去重；忽略非字符串元素。"""
    return list(
        dict.fromkeys(
            role for role in roles if isinstance(role, str) and role in _RECOGNIZED_ROLES
        )
    )


def unrecognized_permission_roles(roles: Iterable[str]) -> list[str]:
    """返回未知的 permission-* 角色，按输入顺序去重；不授予权限。"""
    return list(
        dict.fromkeys(
            role
            for role in roles
            if isinstance(role, str)
            and role.startswith("permission-")
            and role not in _RECOGNIZED_ROLES
        )
    )
