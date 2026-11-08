# Python SDK：bitnp-authz

`bitnp_authz` 将已验证的 Keycloak realm 角色转换成授权事实和展示职务。要求 Python 3.10+，无第三方运行时依赖，包含 `py.typed`。

## 快速安装

通过 HTTPS 从 Git 仓库安装：

```bash
python -m pip install "bitnp-authz @ git+https://git.bitnp.net/BITNP/bitnp-authz.git#subdirectory=python"
```

或先克隆，再从仓库根目录安装：

```bash
git clone https://git.bitnp.net/BITNP/bitnp-authz.git
cd bitnp-authz
python -m pip install -e ./python
```

建议在项目虚拟环境中安装。`-e` 为可编辑安装；仅使用库时可以省略。
远程命令安装默认分支的当前代码，需固定版本时请使用实际存在的标签或提交。
Go 安装和本地模块引用方式见[仓库说明](../README.md#快速安装)。

## 最小用法

```python
from bitnp_authz import Department, SitePermissions

# 实际应用先自行完成 token 验证，再传入 claims。
permissions = SitePermissions.from_claims({
    "realm_access": {"roles": [
        "permission-techdept-access",
        "permission-techdept-admin",
        "position-techdept-head",
    ]}
})

if not permissions.admitted:
    raise PermissionError("not_member")
may_access_tech = (
    permissions.system_admin
    or Department.TECH in permissions.access_departments
)
```

也可直接传入角色：`SitePermissions.from_roles(roles)`。本包不处理登录、token 验签、会话、网络、环境变量或报告排版。

## 公共 API

`bitnp_authz` 根入口是稳定的公共 API 导入入口；子模块路径属于实现组织，可能调整。

| API | 说明 |
| --- | --- |
| `Department` | `TECH`、`CLINIC`、`MEDIA`、`TECHSVC`；提供只读 `role_key` 和 `label` 属性 |
| `PositionScope` | `ASSOCIATION` 与 `DEPARTMENT` |
| `Position` | 不可变职务记录：`role_name`、`label`、`scope`、`department`、`rank` |
| `PositionView` | 不可变展示记录：`code`、`label`、`scope`、`department` |
| `SitePermissions` | 授权事实记录 |
| `SYSTEM_ADMIN_ROLE` | 系统管理员角色名 |
| `POSITIONS` | 包含契约职务的 tuple |
| `position_for_role(role)` | 查询职务，未知角色返回 `None` |
| `access_role()` / `admin_role()` | 构造部门角色名 |
| `recognized_roles()` | 返回授权角色的 `frozenset` |
| `permission_roles()` | 筛选已识别权限角色，保序去重 |
| `unrecognized_permission_roles()` | 筛选未知 `permission-*` 角色，保序去重 |
| `realm_roles_from_claims()` | 提取 `realm_access.roles` |
| `positions_from_roles()` | 聚合展示职务 |

角色匹配区分大小写且不 trim。claims 提取丢弃非字符串和空白字符串，但保留有效字符串原文、重复项与顺序。部门管理权不隐含访问权，职务不参与授权；同 rank 职务保留输入中先出现者。完整规则见[角色契约](../SPEC.md)。

## SitePermissions

- `admitted`：系统管理员或至少持有一个部门 access/admin 角色。
- `system_admin`：存在 `permission-system-admin`。
- `manager`：系统管理员或至少持有一个部门 admin 角色。
- `access_departments` / `managed_departments`：显式角色对应的 `frozenset[Department]`。
- `access_departments_ordered` / `managed_departments_ordered`：按规范顺序的 tuple。
- `positions`：聚合后的 `PositionView` tuple。

系统管理员不会自动填充部门集合或职务。调用方自行负责 token 验证、权限刷新和错误处理。

## 可选测试

从仓库根目录运行：

```bash
PYTHONPATH=python python -m unittest discover -s python/tests -v
go test ./...
```

## 模块

- [roles.py](bitnp_authz/roles.py)：部门、角色构造与诊断
- [positions.py](bitnp_authz/positions.py)：职务目录、查询与聚合
- [permissions.py](bitnp_authz/permissions.py)：授权事实
- [claims.py](bitnp_authz/claims.py)：claims 角色提取
