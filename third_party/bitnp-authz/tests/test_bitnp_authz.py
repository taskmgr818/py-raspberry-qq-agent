"""``bitnp_authz`` 的单元测试（只依赖标准库）。

从仓库根目录运行：

    PYTHONPATH=python python -m unittest discover -s python/tests -v
"""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError
from typing import Any

import bitnp_authz
from bitnp_authz import (
    Department,
    POSITIONS,
    SYSTEM_ADMIN_ROLE,
    PositionScope,
    SitePermissions,
    access_role,
    admin_role,
    permission_roles,
    position_for_role,
    positions_from_roles,
    recognized_roles,
    realm_roles_from_claims,
    unrecognized_permission_roles,
)


class RealmRolesTest(unittest.TestCase):
    def test_reads_roles_from_realm_access(self) -> None:
        claims = {"realm_access": {"roles": ["permission-techdept-access", "position-techdept-head"]}}
        self.assertEqual(
            ["permission-techdept-access", "position-techdept-head"],
            realm_roles_from_claims(claims),
        )

    def test_missing_realm_access_is_empty(self) -> None:
        self.assertEqual([], realm_roles_from_claims({}))

    def test_wrong_shape_is_empty(self) -> None:
        self.assertEqual([], realm_roles_from_claims({"realm_access": {"roles": "x"}}))
        self.assertEqual([], realm_roles_from_claims({"realm_access": "x"}))
        self.assertEqual([], realm_roles_from_claims({"realm_access": {"roles": 5}}))

    def test_blank_and_non_string_roles_are_dropped(self) -> None:
        claims = {"realm_access": {"roles": ["permission-techdept-access", "", "  ", 7]}}
        self.assertEqual(["permission-techdept-access"], realm_roles_from_claims(claims))


class SitePermissionsTest(unittest.TestCase):
    def test_no_roles_is_not_admitted(self) -> None:
        permissions = SitePermissions.from_roles([])
        self.assertFalse(permissions.admitted)
        self.assertFalse(permissions.manager)

    def test_unknown_roles_are_ignored(self) -> None:
        permissions = SitePermissions.from_roles(["admin", "offline_access", "guild-member"])
        self.assertFalse(permissions.admitted)

    def test_keycloak_admin_role_is_not_system_admin(self) -> None:
        permissions = SitePermissions.from_roles(["admin"])
        self.assertFalse(permissions.system_admin)
        self.assertFalse(permissions.admitted)

    def test_system_admin(self) -> None:
        permissions = SitePermissions.from_roles(["permission-system-admin"])
        self.assertTrue(permissions.admitted)
        self.assertTrue(permissions.system_admin)
        self.assertTrue(permissions.manager)

    def test_department_access_is_member_only(self) -> None:
        permissions = SitePermissions.from_roles([access_role(Department.CLINIC)])
        self.assertTrue(permissions.admitted)
        self.assertFalse(permissions.manager)
        self.assertEqual((Department.CLINIC,), permissions.access_departments_ordered)
        self.assertEqual((), permissions.managed_departments_ordered)

    def test_department_admin_manages_but_does_not_grant_access(self) -> None:
        permissions = SitePermissions.from_roles([admin_role(Department.TECH)])
        self.assertTrue(permissions.admitted)
        self.assertTrue(permissions.manager)
        self.assertEqual((Department.TECH,), permissions.managed_departments_ordered)
        self.assertEqual((), permissions.access_departments_ordered)

    def test_departments_are_ordered(self) -> None:
        permissions = SitePermissions.from_roles(
            [
                access_role(Department.TECHSVC),
                access_role(Department.TECH),
                access_role(Department.MEDIA),
            ]
        )
        self.assertEqual(
            (Department.TECH, Department.MEDIA, Department.TECHSVC),
            permissions.access_departments_ordered,
        )

    def test_from_claims(self) -> None:
        claims = {"realm_access": {"roles": ["permission-techdept-admin"]}}
        permissions = SitePermissions.from_claims(claims)
        self.assertTrue(permissions.admitted)
        self.assertEqual((Department.TECH,), permissions.managed_departments_ordered)

    def test_from_claims_without_roles_is_not_admitted(self) -> None:
        self.assertFalse(SitePermissions.from_claims({}).admitted)


class PositionsTest(unittest.TestCase):
    def test_association_position_is_kept(self) -> None:
        positions = positions_from_roles(["position-association-chair"])
        self.assertEqual(1, len(positions))
        self.assertEqual("协会主席", positions[0].label)
        self.assertIs(PositionScope.ASSOCIATION, positions[0].scope)
        self.assertIsNone(positions[0].department)

    def test_highest_association_position_wins(self) -> None:
        positions = positions_from_roles(
            ["position-association-vice-chair-clinic", "position-association-chair"]
        )
        self.assertEqual(["position-association-chair"], [p.code for p in positions])

    def test_highest_position_per_department_wins(self) -> None:
        positions = positions_from_roles(
            ["position-techdept-member", "position-techdept-head", "position-clinic-deputy-head"]
        )
        self.assertEqual(
            ["position-techdept-head", "position-clinic-deputy-head"],
            [p.code for p in positions],
        )

    def test_association_is_listed_before_departments(self) -> None:
        positions = positions_from_roles(
            ["position-clinic-head", "position-association-chair", "position-techdept-head"]
        )
        self.assertEqual(
            ["position-association-chair", "position-techdept-head", "position-clinic-head"],
            [p.code for p in positions],
        )

    def test_unknown_and_generic_roles_are_ignored(self) -> None:
        positions = positions_from_roles(
            ["position-association-vice-chair", "position-unknown", "position-president"]
        )
        self.assertEqual((), positions)

    def test_manager_does_not_grant_position(self) -> None:
        permissions = SitePermissions.from_roles(["permission-techdept-admin"])
        self.assertEqual((), permissions.positions)


class DiagnosticsTest(unittest.TestCase):
    def test_permission_roles_filter_and_dedupe(self) -> None:
        roles = [
            "permission-techdept-admin",
            "permission-techdept-admin",
            "position-techdept-head",
            "permission-system-admin",
        ]
        self.assertEqual(
            ["permission-techdept-admin", "permission-system-admin"],
            permission_roles(roles),
        )

    def test_unrecognized_permission_roles_are_reported(self) -> None:
        typo = "permission-techdep-admin"
        self.assertEqual([typo], unrecognized_permission_roles([typo, "permission-clinic-access"]))
        self.assertFalse(SitePermissions.from_roles([typo]).admitted)

    def test_recognized_roles_count(self) -> None:
        # 4 个部门 × (access + admin) + system-admin
        self.assertEqual(9, len(recognized_roles()))


class BoundaryTests(unittest.TestCase):
    def test_claims_preserve_order_duplicates_and_nonblank_whitespace(self) -> None:
        roles = [
            "permission-techdept-access",
            " permission-techdept-admin ",
            "permission-techdept-access",
        ]
        claims = {"realm_access": {"roles": roles + ["", "\t", None, {}, []]}}
        self.assertEqual(roles, realm_roles_from_claims(claims))
        extracted = realm_roles_from_claims(claims)
        extracted[0] = SYSTEM_ADMIN_ROLE
        self.assertEqual("permission-techdept-access", roles[0])
        self.assertEqual("permission-techdept-access", claims["realm_access"]["roles"][0])

    def test_claims_accept_tuple_but_not_scalar_sequences(self) -> None:
        self.assertEqual([SYSTEM_ADMIN_ROLE], realm_roles_from_claims(
            {"realm_access": {"roles": (SYSTEM_ADMIN_ROLE,)}}
        ))
        for raw in (None, 1, "x", b"x", bytearray(b"x"), {"x": 1}):
            with self.subTest(raw=raw):
                self.assertEqual([], realm_roles_from_claims({"realm_access": {"roles": raw}}))

    def test_other_claims_do_not_grant_permissions(self) -> None:
        self.assertFalse(SitePermissions.from_claims({
            "groups": [SYSTEM_ADMIN_ROLE],
            "resource_access": {"client": {"roles": [SYSTEM_ADMIN_ROLE]}},
        }).admitted)

    def test_one_shot_iterable_keeps_positions(self) -> None:
        roles = iter([SYSTEM_ADMIN_ROLE, "position-techdept-head"])
        permissions = SitePermissions.from_roles(roles)
        self.assertTrue(permissions.system_admin)
        self.assertEqual("position-techdept-head", permissions.positions[0].code)
        self.assertEqual(frozenset(), permissions.access_departments)
        self.assertEqual(frozenset(), permissions.managed_departments)

    def test_non_string_elements_are_ignored(self) -> None:
        roles: list[Any] = [None, {}, [], 1, SYSTEM_ADMIN_ROLE, SYSTEM_ADMIN_ROLE]
        self.assertEqual([SYSTEM_ADMIN_ROLE], permission_roles(roles))
        self.assertEqual([], unrecognized_permission_roles(roles))
        self.assertTrue(SitePermissions.from_roles(roles).system_admin)
        self.assertEqual((), positions_from_roles(roles))

    def test_role_matching_is_exact(self) -> None:
        for role in ("PERMISSION-SYSTEM-ADMIN", " permission-system-admin", "permission-system-admin "):
            with self.subTest(role=role):
                self.assertFalse(SitePermissions.from_roles([role]).admitted)
                self.assertEqual([], permission_roles([role]))

    def test_diagnostics_include_bare_prefix_and_dedupe(self) -> None:
        roles = ["permission-", "permission-typo", "permission-", SYSTEM_ADMIN_ROLE]
        self.assertEqual(["permission-", "permission-typo"], unrecognized_permission_roles(roles))

    def test_equal_rank_keeps_first_input(self) -> None:
        roles = ["position-association-vice-chair-clinic", "position-association-vice-chair-techdept"]
        self.assertEqual(roles[0], positions_from_roles(roles)[0].code)
        self.assertEqual(roles[1], positions_from_roles(reversed(roles))[0].code)

    def test_positions_do_not_grant_authorization(self) -> None:
        permissions = SitePermissions.from_roles(p.role_name for p in POSITIONS)
        self.assertFalse(permissions.admitted)
        self.assertFalse(permissions.manager)
        self.assertFalse(permissions.system_admin)
        self.assertEqual(5, len(permissions.positions))

    def test_catalog_lookup_and_immutability(self) -> None:
        self.assertEqual(19, len(POSITIONS))
        self.assertEqual(19, len({p.role_name for p in POSITIONS}))
        for position in POSITIONS:
            self.assertIs(position, position_for_role(position.role_name))
            self.assertEqual(position.scope is PositionScope.ASSOCIATION, position.department is None)
        self.assertIsNone(position_for_role("position-unknown"))
        self.assertFalse(hasattr(bitnp_authz, "POSITION_BY_ROLE"))
        with self.assertRaises(FrozenInstanceError):
            setattr(POSITIONS[0], "rank", 0)
        with self.assertRaises(AttributeError):
            setattr(Department.TECH, "role_key", "other")
        with self.assertRaises(AttributeError):
            setattr(Department.TECH, "label", "other")

    def test_all_declares_existing_unique_exports(self) -> None:
        self.assertEqual(len(bitnp_authz.__all__), len(set(bitnp_authz.__all__)))
        for name in bitnp_authz.__all__:
            self.assertTrue(hasattr(bitnp_authz, name), name)


if __name__ == "__main__":
    unittest.main()