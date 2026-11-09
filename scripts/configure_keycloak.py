"""Preview/apply the QQ attribute and a dedicated read-only Bot service account.

uv run --env-file ../guild-go/.env --env-file ../keycloak.env python scripts/configure_keycloak.py
Append --apply to write. Existing QQ profile metadata and client secrets are preserved.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import httpx


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--client-id", default="qq-bot")
    parser.add_argument("--output", type=Path, default=Path("data/keycloak-bot.env"))
    args = parser.parse_args()
    issuer = os.environ["OIDC_ISSUER"].rstrip("/")
    base, realm = issuer.split("/realms/", 1)
    with httpx.Client(timeout=15) as http:
        response = http.post(
            base + "/realms/master/protocol/openid-connect/token",
            data={
                "grant_type": "password",
                "client_id": "admin-cli",
                "username": os.environ["user"],
                "password": os.environ["password"],
            },
        )
        response.raise_for_status()
        http.headers["Authorization"] = "Bearer " + response.json()["access_token"]
        admin = base + "/admin/realms/" + realm

        def get(path: str):
            response = http.get(admin + path)
            response.raise_for_status()
            return response.json()

        profile = get("/users/profile")
        attribute = {
            "name": "qq_number",
            "displayName": "QQ",
            "multivalued": False,
            "permissions": {"view": ["admin"], "edit": ["admin"]},
            "validations": {"pattern": {"pattern": "[1-9][0-9]{4,19}"}},
        }
        existing = next(
            (a for a in profile["attributes"] if a["name"] == "qq_number"), None
        )
        if existing and (
            existing.get("multivalued", False)
            or existing.get("permissions", {}).get("edit") != ["admin"]
            or existing.get("validations", {}).get("pattern")
            != attribute["validations"]["pattern"]
        ):
            raise SystemExit(
                "qq_number must be single-valued, admin-editable only, and use the QQ pattern; "
                "inspect it before applying"
            )
        clients = get("/clients")
        current = next((c for c in clients if c["clientId"] == args.client_id), None)
        management = next(
            (c for c in clients if c["clientId"] == "realm-management"), None
        )
        if management is None:
            management = next(
                (c for c in clients if c["clientId"] == realm + "-realm"), None
            )
        if management is None:
            raise SystemExit("Cannot find the realm user-management client")
        role = get("/clients/" + management["id"] + "/roles/view-users")
        client = {
            "clientId": args.client_id,
            "enabled": True,
            "protocol": "openid-connect",
            "publicClient": False,
            "serviceAccountsEnabled": True,
            "standardFlowEnabled": False,
            "directAccessGrantsEnabled": False,
            "fullScopeAllowed": False,
            "clientAuthenticatorType": "client-secret",
        }
        if current:
            current = get("/clients/" + current["id"])
        changes = {
            key: value
            for key, value in client.items()
            if current is None or current.get(key) != value
        }
        scope_path = "/scope-mappings/clients/" + management["id"]
        account_path = "/role-mappings/clients/" + management["id"]
        scope_roles = get("/clients/" + current["id"] + scope_path) if current else []
        account = (
            get("/clients/" + current["id"] + "/service-account-user")
            if current and current.get("serviceAccountsEnabled")
            else None
        )
        account_roles = get("/users/" + account["id"] + account_path) if account else []
        needs_scope = not any(r["id"] == role["id"] for r in scope_roles)
        needs_role = not any(r["id"] == role["id"] for r in account_roles)
        secret = (
            get("/clients/" + current["id"] + "/client-secret")["value"]
            if current and current.get("clientAuthenticatorType") == "client-secret"
            else None
        )

        def credentials(value: str) -> str:
            return (
                "BOT_KEYCLOAK_CLIENT_ID="
                + args.client_id
                + "\nBOT_KEYCLOAK_CLIENT_SECRET="
                + value
                + "\n"
            )

        print(
            json.dumps(
                {
                    "realm": realm,
                    "attribute": existing or attribute,
                    "attribute_action": "preserve" if existing else "create",
                    "client_action": ("update" if changes else "preserve")
                    if current
                    else "create",
                    "client_changes": changes,
                    "service_account_role": management["clientId"] + ":view-users",
                    "add_service_account_role": needs_role,
                    "add_client_scope": needs_scope,
                    "credentials_file": str(args.output),
                    "apply": args.apply,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        if not args.apply:
            return
        if args.output.exists() and (
            secret is None or args.output.read_text() != credentials(secret)
        ):
            raise SystemExit(
                "Credential output exists with different contents; use a new --output path"
            )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        if not existing:
            profile["attributes"].append(attribute)
            response = http.put(admin + "/users/profile", json=profile)
            response.raise_for_status()
        if current is None:
            response = http.post(admin + "/clients", json=client)
            response.raise_for_status()
            client_id = get("/clients?clientId=" + args.client_id)[0]["id"]
        else:
            client_id = current["id"]
            if changes:
                response = http.put(admin + "/clients/" + client_id, json=changes)
                response.raise_for_status()
        if account is None:
            account = get("/clients/" + client_id + "/service-account-user")
        for needed, path in [
            (needs_scope, "/clients/" + client_id + scope_path),
            (needs_role, "/users/" + account["id"] + account_path),
        ]:
            if needed:
                response = http.post(admin + path, json=[role])
                response.raise_for_status()
        if secret is None:
            secret = get("/clients/" + client_id + "/client-secret")["value"]
        if not args.output.exists():
            descriptor = os.open(
                args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            with os.fdopen(descriptor, "w") as output:
                # Secrets are written locally, never printed or regenerated.
                output.write(credentials(secret))
        args.output.chmod(0o600)
        print(
            "Applied. Credentials saved with mode 0600; no user QQ bindings were changed."
        )


if __name__ == "__main__":
    main()
