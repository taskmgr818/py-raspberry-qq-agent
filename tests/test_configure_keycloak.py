from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import httpx
import pytest


@pytest.fixture
def provisioner(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "configure_keycloak",
        Path(__file__).parents[1] / "scripts/configure_keycloak.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for key, value in {
        "OIDC_ISSUER": "https://id.example/realms/master",
        "user": "admin",
        "password": "test-password",
    }.items():
        monkeypatch.setenv(key, value)
    profile = {
        "attributes": [
            {
                "name": "qq_number",
                "displayName": "${profile.attributes.qq_number}",
                "multivalued": False,
                "permissions": {"view": ["admin", "user"], "edit": ["admin"]},
                "validations": {"pattern": {"pattern": "[1-9][0-9]{4,19}"}},
                "annotations": {},
            }
        ]
    }
    client = {
        "id": "bot-id",
        "clientId": "qq-bot",
        "enabled": True,
        "protocol": "openid-connect",
        "publicClient": False,
        "serviceAccountsEnabled": True,
        "standardFlowEnabled": False,
        "directAccessGrantsEnabled": False,
        "fullScopeAllowed": False,
        "clientAuthenticatorType": "client-secret",
        "attributes": {"keep": "value"},
    }
    role = {"id": "view-users-id", "name": "view-users"}
    state = {
        "client": client,
        "profile": profile,
        "scope": [role],
        "account_roles": [role],
    }
    writes = []

    def handler(request):
        path = request.url.path
        if path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "admin-token"})
        path = path.removeprefix("/admin/realms/master")
        if request.method != "GET":
            data = json.loads(request.content)
            writes.append((request.method, path, data))
            if path == "/clients":
                state["client"] = {"id": "bot-id", **data}
            elif path == "/clients/bot-id":
                state["client"].update(data)
            elif path == "/clients/bot-id/scope-mappings/clients/management-id":
                state["scope"] = data
            elif path == "/users/account-id/role-mappings/clients/management-id":
                state["account_roles"] = data
            elif path == "/users/profile":
                state["profile"] = data
            else:
                pytest.fail(f"unexpected write: {path}")
            return httpx.Response(204)
        if path == "/clients":
            data = [state["client"]] if state["client"] else []
            if "clientId" not in request.url.params:
                data += [{"id": "management-id", "clientId": "master-realm"}]
        else:
            data = {
                "/users/profile": state["profile"],
                "/clients/management-id/roles/view-users": role,
                "/clients/bot-id": state["client"],
                "/clients/bot-id/scope-mappings/clients/management-id": state["scope"],
                "/clients/bot-id/service-account-user": {"id": "account-id"},
                "/users/account-id/role-mappings/clients/management-id": state[
                    "account_roles"
                ],
                "/clients/bot-id/client-secret": {"value": "unchanged-secret"},
            }[path]
        return httpx.Response(200, json=data)

    real_client = httpx.Client
    monkeypatch.setattr(
        module.httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    return module, state, writes


def run(module, monkeypatch, output, apply=False):
    monkeypatch.setattr(
        "sys.argv",
        ["configure_keycloak.py", "--output", str(output)]
        + (["--apply"] if apply else []),
    )
    module.main()


def test_existing_profile_and_client_preview_are_read_only(
    provisioner, monkeypatch, tmp_path, capsys
):
    module, state, writes = provisioner
    output = tmp_path / "credentials.env"
    run(module, monkeypatch, output)
    plan = json.loads(capsys.readouterr().out)
    assert plan["attribute"] == state["profile"]["attributes"][0]
    assert plan["attribute_action"] == "preserve"
    assert plan["client_changes"] == {}
    assert not plan["add_client_scope"] and not plan["add_service_account_role"]
    assert writes == [] and not output.exists()


@pytest.mark.parametrize("create", [False, True])
def test_apply_repairs_only_needed_configuration_and_can_repeat(
    provisioner, monkeypatch, tmp_path, capsys, create
):
    module, state, writes = provisioner
    if create:
        state["client"] = None
        state["profile"] = {"attributes": []}
    else:
        state["client"]["standardFlowEnabled"] = True
    state["scope"] = []
    state["account_roles"] = []
    output = tmp_path / "credentials.env"
    run(module, monkeypatch, output, apply=True)
    assert state["client"]["standardFlowEnabled"] is False
    assert state["scope"] and state["account_roles"]
    assert output.stat().st_mode & 0o777 == 0o600
    assert (
        output.read_text()
        == "BOT_KEYCLOAK_CLIENT_ID=qq-bot\nBOT_KEYCLOAK_CLIENT_SECRET=unchanged-secret\n"
    )
    assert "unchanged-secret" not in capsys.readouterr().out
    if not create:
        assert state["client"]["attributes"] == {"keep": "value"}
        assert not any(path == "/users/profile" for _, path, _ in writes)
        assert state["profile"]["attributes"][0]["permissions"]["view"] == [
            "admin",
            "user",
        ]
    writes.clear()
    run(module, monkeypatch, output, apply=True)
    assert writes == []


def test_conflicting_credential_output_stops_before_server_changes(
    provisioner, monkeypatch, tmp_path
):
    module, state, writes = provisioner
    state["client"]["standardFlowEnabled"] = True
    output = tmp_path / "credentials.env"
    output.write_text("different credentials")
    with pytest.raises(SystemExit, match="different contents"):
        run(module, monkeypatch, output, apply=True)
    assert writes == []
