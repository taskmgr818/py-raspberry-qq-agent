"""职务目录与展示聚合；职务角色不参与授权。"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Final

from .roles import Department


class PositionScope(Enum):
    """职务属于协会层面还是部门层面。"""

    ASSOCIATION = "association"
    DEPARTMENT = "department"


@dataclass(frozen=True)
class Position:
    """一个可由 Keycloak 角色映射的行政职务。"""

    role_name: str
    label: str
    scope: PositionScope
    department: Department | None
    rank: int


POSITIONS: Final[tuple[Position, ...]] = (
    Position("position-association-chair", "协会主席", PositionScope.ASSOCIATION, None, 100),
    Position(
        "position-association-vice-chair-techdept", "协会副主席（技术部分管）",
        PositionScope.ASSOCIATION, None, 92,
    ),
    Position(
        "position-association-vice-chair-clinic", "协会副主席（电脑诊所分管）",
        PositionScope.ASSOCIATION, None, 92,
    ),
    Position(
        "position-association-vice-chair-digimedia", "协会副主席（数字媒体中心分管）",
        PositionScope.ASSOCIATION, None, 92,
    ),
    Position("position-techdept-head", "技术部部长", PositionScope.DEPARTMENT, Department.TECH, 80),
    Position("position-techdept-deputy-head", "技术部副部长", PositionScope.DEPARTMENT, Department.TECH, 70),
    Position("position-techdept-advisor", "技术部顾问", PositionScope.DEPARTMENT, Department.TECH, 60),
    Position("position-techdept-member", "技术部成员", PositionScope.DEPARTMENT, Department.TECH, 10),
    Position("position-clinic-head", "电脑诊所部长", PositionScope.DEPARTMENT, Department.CLINIC, 80),
    Position("position-clinic-deputy-head", "电脑诊所副部长", PositionScope.DEPARTMENT, Department.CLINIC, 70),
    Position("position-clinic-advisor", "电脑诊所顾问", PositionScope.DEPARTMENT, Department.CLINIC, 60),
    Position("position-clinic-member", "电脑诊所成员", PositionScope.DEPARTMENT, Department.CLINIC, 10),
    Position("position-digimedia-head", "数字媒体中心部长", PositionScope.DEPARTMENT, Department.MEDIA, 80),
    Position("position-digimedia-deputy-head", "数字媒体中心副部长", PositionScope.DEPARTMENT, Department.MEDIA, 70),
    Position("position-digimedia-advisor", "数字媒体中心顾问", PositionScope.DEPARTMENT, Department.MEDIA, 60),
    Position("position-digimedia-member", "数字媒体中心成员", PositionScope.DEPARTMENT, Department.MEDIA, 10),
    Position("position-techsvc-director", "技术保障中心主任", PositionScope.DEPARTMENT, Department.TECHSVC, 80),
    Position("position-techsvc-deputy-director", "技术保障中心副主任", PositionScope.DEPARTMENT, Department.TECHSVC, 70),
    Position("position-techsvc-member", "技术保障中心成员", PositionScope.DEPARTMENT, Department.TECHSVC, 10),
)

_POSITION_BY_ROLE: Final = MappingProxyType({
    position.role_name: position for position in POSITIONS
})


def position_for_role(role: str) -> Position | None:
    """查询职务目录；未知角色返回 None。"""
    return _POSITION_BY_ROLE.get(role)


@dataclass(frozen=True)
class PositionView:
    """某个用户的一个职务，可直接展示。

    :param code: Keycloak 角色名
    :param label: 人类可读的名称
    :param scope: 协会层面或部门层面
    :param department: 部门层面的职务对应的部门，否则为 ``None``
    """

    code: str
    label: str
    scope: PositionScope
    department: Department | None


def positions_from_roles(roles: Iterable[str]) -> tuple[PositionView, ...]:
    """解析并去重角色中的职务。

    协会层面只保留级别最高的职务，每个部门只保留级别最高的职务；返回顺序为
    协会优先，然后按 TECH、CLINIC、MEDIA、TECHSVC 排列。同级别保留输入中
    最先出现的职务；未知角色与非字符串元素被忽略。
    """
    association: Position | None = None
    departments: dict[Department, Position] = {}

    for role in roles:
        if not isinstance(role, str) or not role:
            continue
        position = position_for_role(role)
        if position is None:
            continue
        if position.scope is PositionScope.ASSOCIATION:
            if association is None or position.rank > association.rank:
                association = position
            continue
        assert position.department is not None
        current = departments.get(position.department)
        if current is None or position.rank > current.rank:
            departments[position.department] = position

    result: list[PositionView] = []
    if association is not None:
        result.append(_to_view(association))
    for department in Department:
        position = departments.get(department)
        if position is not None:
            result.append(_to_view(position))
    return tuple(result)


def _to_view(position: Position) -> PositionView:
    return PositionView(position.role_name, position.label, position.scope, position.department)