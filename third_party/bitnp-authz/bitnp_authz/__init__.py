"""realm 角色到授权事实与展示职务的纯计算 SDK。

本包根命名空间是稳定的公开 API；子模块是实现细节。
调用方负责 token 校验、登录流程、会话与权限刷新策略。

示例::

    from bitnp_authz import Department, SitePermissions

    permissions = SitePermissions.from_claims(verified_claims)
    if not permissions.admitted:
        raise PermissionError("not_member")
    if Department.TECH in permissions.managed_departments:
        ...
"""

from .claims import realm_roles_from_claims
from .permissions import SitePermissions
from .positions import (
    POSITIONS,
    Position,
    PositionScope,
    PositionView,
    position_for_role,
    positions_from_roles,
)
from .roles import (
    SYSTEM_ADMIN_ROLE,
    Department,
    access_role,
    admin_role,
    permission_roles,
    recognized_roles,
    unrecognized_permission_roles,
)

__version__ = "1.0.0"

# 按名称排序。类型、常量与函数分别遵循各自的 Python 命名约定。
__all__ = [
    "Department",
    "POSITIONS",
    "Position",
    "PositionScope",
    "PositionView",
    "SYSTEM_ADMIN_ROLE",
    "SitePermissions",
    "__version__",
    "access_role",
    "admin_role",
    "permission_roles",
    "position_for_role",
    "positions_from_roles",
    "realm_roles_from_claims",
    "recognized_roles",
    "unrecognized_permission_roles",
]
