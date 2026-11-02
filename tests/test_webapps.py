"""Offline publisher behavior through client and CLI interfaces."""

import base64
import copy
import json
import os
from pathlib import Path
from typing import Any

import pytest

from hexa.api import API, APIError
from hexa.cli import main
from hexa.config import Credentials, SetupError
from hexa.webapp_bundle import PAYLOAD_LIMIT

CREDENTIALS = Credentials("https://example.invalid", "selected", "synthetic-token")


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    directory = tmp_path / ".hexa" / "dist"
    directory.mkdir(parents=True)
    (directory / "index.html").write_bytes(
        b"<!doctype html>\r\n<title>Caf\xc3\xa9</title>"
    )
    (directory / "asset.bin").write_bytes(b"\x89PNG\x00\xff")
    return directory


@pytest.fixture
def remote(monkeypatch):
    app = {
        "id": "f4972d28-0b80-44ae-81a3-cf7d866627ee",
        "slug": "dashboard",
        "name": "Dashboard",
        "type": "STATIC",
        "serveUrl": "https://dashboard.example.invalid/",
        "workspace": {"slug": "selected"},
        "isPublic": True,
        "allowedOperations": ["USER_READ"],
        "permissions": {"update": True, "delete": False},
        "source": {"__typename": "GitSource", "publishedVersion": "old-sha"},
    }
    apps = [app]
    calls: list[tuple[str, dict[str, Any]]] = []

    def graphql(self, query, variables):
        calls.append((query, copy.deepcopy(variables)))
        if query.startswith("query Webapps"):
            assert variables["ws"] == "selected"
            return {
                "webapps": {
                    "items": copy.deepcopy(apps),
                    "pageNumber": variables["page"],
                    "totalPages": 1,
                    "totalItems": len(apps),
                }
            }
        if query.startswith("query Workspace"):
            assert variables == {"ws": "selected"}
            return {"workspace": {"slug": "selected", "name": "Selected"}}
        assert query.startswith("mutation Change")
        field = "createWebapp" if "createWebapp(" in query else "updateWebapp"
        return {
            field: {
                "success": True,
                "errors": [],
                "webapp": {
                    **app,
                    "source": {
                        "__typename": "GitSource",
                        "publishedVersion": "new-sha",
                    },
                },
            }
        }

    monkeypatch.setattr(API, "graphql", graphql)
    return apps, calls


def test_scoped_selection_and_single_content_only_publication(bundle, remote):
    apps, calls = remote
    client = API(CREDENTIALS)
    for selector in (apps[0]["id"], "dashboard", "Dashboard"):
        assert client.webapp(selector) == apps[0]
    created = client.create_webapp("Private dashboard", bundle)
    query, variables = calls[-1]
    values = variables["input"]
    assert values.keys() == {
        "name",
        "workspaceSlug",
        "isPublic",
        "allowedOperations",
        "source",
    }
    assert values["workspaceSlug"] == "selected" and values["isPublic"] is False
    assert values["allowedOperations"] == []
    assert created["webapp"]["source"]["publishedVersion"] == "new-sha"
    assert created["payload_bytes"] == len(client.graphql_body(query, variables))
    files = values["source"]["static"]
    assert [item["path"] for item in files] == ["asset.bin", "index.html"]
    assert files[0]["encoding"] == "BASE64"
    assert base64.b64decode(files[0]["content"]) == (bundle / "asset.bin").read_bytes()
    assert files[1]["encoding"] == "TEXT"
    assert files[1]["content"].encode() == (bundle / "index.html").read_bytes()
    before = copy.deepcopy(apps[0])
    calls.clear()
    deployed = client.deploy_webapp("dashboard", bundle)
    assert (
        len(calls) == 2
    )  # One scoped preflight and one publication, no remote inventory/readback.
    query, variables = calls[-1]
    assert variables["input"] == {"id": before["id"], "files": files}
    assert deployed["target"] == before and apps[0] == before
    assert deployed["webapp"]["serveUrl"] == before["serveUrl"]
    assert deployed["payload_bytes"] == len(client.graphql_body(query, variables))
    assert "remote-only files remain" in deployed["warning"]
    assert all(
        "previewUrl" not in query and " files(" not in query for query, _ in calls
    )


@pytest.mark.parametrize(
    "failure", ["ambiguous", "incomplete", "outside", "iframe", "permission"]
)
def test_write_target_rejections_never_dispatch(bundle, remote, monkeypatch, failure):
    apps, calls = remote
    client = API(CREDENTIALS)
    if failure == "ambiguous":
        apps.append({**apps[0], "id": "another", "slug": "another"})
    elif failure == "incomplete":
        monkeypatch.setattr(client, "max_pages", 1)
        graphql = client.graphql

        def incomplete(query, variables):
            result = graphql(query, variables)
            result["webapps"]["totalPages"] = 2
            return result

        monkeypatch.setattr(client, "graphql", incomplete)
    elif failure == "outside":
        apps[0]["workspace"] = {"slug": "other"}
    elif failure == "iframe":
        apps[0]["type"] = "IFRAME"
    else:
        apps[0]["permissions"]["update"] = False
    with pytest.raises(APIError):
        client.deploy_webapp("Dashboard", bundle, dry_run=True)
    assert calls and all(query.startswith("query ") for query, _ in calls)


@pytest.mark.parametrize(
    "bad",
    [
        "index",
        "symlink",
        "special",
        "env",
        "git",
        "dependencies",
        "credential",
        "token",
    ],
)
def test_bundle_safety_rejects_before_mutation(bundle, remote, tmp_path, bad):
    _, calls = remote
    credential = tmp_path / ".hexa" / "chosen.credentials"
    credential.write_text("not a token")
    if bad == "index":
        (bundle / "index.html").write_bytes(b"")
    elif bad == "symlink":
        (bundle / "linked").symlink_to(credential)
    elif bad == "special":
        os.mkfifo(bundle / "pipe")
    elif bad == "env":
        (bundle / ".env.production").write_text("secret")
    elif bad in ("git", "dependencies"):
        (bundle / (".git" if bad == "git" else "node_modules")).mkdir()
    elif bad == "credential":
        (bundle / "innocent.txt").hardlink_to(credential)
    else:
        (bundle / "app.js").write_text(CREDENTIALS.token)
    with pytest.raises(SetupError):
        API(CREDENTIALS).create_webapp("Test", bundle, credential_file=credential)
    assert all(query.startswith("query ") for query, _ in calls)


@pytest.mark.parametrize("encoding", ["TEXT", "BASE64"])
def test_serialized_budget_includes_json_and_base64_overhead(bundle, remote, encoding):
    _, calls = remote
    client = API(CREDENTIALS)
    asset = bundle / "asset.bin"
    asset.write_bytes(b"" if encoding == "TEXT" else b"\xff")
    baseline = client.create_webapp("Test", bundle, dry_run=True)["payload_bytes"]
    if encoding == "TEXT":
        # Each quote costs two serialized bytes, even though the source is smaller.
        count = (PAYLOAD_LIMIT - baseline) // 2
        asset.write_bytes(b'"' * count)
        increment = b'"'
    else:
        # Every three raw bytes cost four BASE64 bytes.
        count = (PAYLOAD_LIMIT - baseline + 4) // 4 * 3
        asset.write_bytes(b"\xff" * count)
        increment = b"\xff" * 3
    result = client.create_webapp("Test", bundle, dry_run=True)
    assert PAYLOAD_LIMIT - 4 <= result["payload_bytes"] <= PAYLOAD_LIMIT
    with asset.open("ab") as output:
        output.write(increment)
    with pytest.raises(SetupError, match="5 MiB"):
        client.create_webapp("Test", bundle)
    assert all(query.startswith("query ") for query, _ in calls)


def test_cli_consent_dry_run_json_and_single_dispatch(
    bundle, remote, capsys, json_shape
):
    apps, calls = remote
    project = bundle.parent
    (project / ".env").write_text(
        f"OPENHEXA_URL={CREDENTIALS.origin}\nOPENHEXA_WORKSPACE=selected\nOPENHEXA_TOKEN={CREDENTIALS.token}\n"
    )
    config = project / "hexa.json"
    config.write_text("{}")
    for command, selector in (("create", "New dashboard"), ("deploy", "dashboard")):
        args = [
            "webapps",
            command,
            selector,
            "--directory",
            str(bundle),
            "--config",
            str(config),
        ]
        assert main(["--json", *args]) == 2
        assert "--dry-run" in json.loads(capsys.readouterr().out)["message"]
        assert not calls
        assert main([*args, "--json", "--dry-run", "--yes"]) == 2
        capsys.readouterr()
        assert not calls
        assert main(["--json", *args, "--dry-run"]) == 0
        captured = capsys.readouterr()
        summary = json.loads(captured.out)
        assert summary["dry_run"] and summary["paths"] == ["asset.bin", "index.html"]
        assert summary["target"]["name"] == (
            selector if command == "create" else "Dashboard"
        )
        assert CREDENTIALS.token not in captured.out + captured.err
        assert all(query.startswith("query ") for query, _ in calls)
        calls.clear()
        assert main([*args, "--json", "--yes"]) == 0
        captured = capsys.readouterr()
        published = json.loads(captured.out)
        assert published["webapp"]["source"]["publishedVersion"] == "new-sha"
        assert main(["webapps", command, "--help"]) == 0
        json_shape(capsys.readouterr().out, published)
        assert sum(query.startswith("mutation ") for query, _ in calls) == 1
        calls.clear()
    args = [
        "webapps",
        "create",
        "New",
        "--directory",
        str(bundle),
        "--config",
        str(config),
        "--json",
        "--dry-run",
    ]
    assert main([*args, "--scope", "FILES_WRITE"]) == 2
    capsys.readouterr()
    assert not calls
    assert main([*args, "--scope", "USER_READ", "--scope", "PIPELINES_RUN"]) == 0
    assert json.loads(capsys.readouterr().out)["requested_scopes"] == [
        "USER_READ",
        "PIPELINES_RUN",
    ]
    apps[0]["name"] = "Visible " + CREDENTIALS.token
    assert main(["webapps", "list", "--config", str(config), "--json"]) == 0
    listing = json.loads(capsys.readouterr().out)
    assert main(["webapps", "list", "--help"]) == 0
    json_shape(capsys.readouterr().out, listing)
    for command in (["show", "dashboard"], ["list", "--all"]):
        assert main(["webapps", *command, "--config", str(config), "--json"]) == 0
        captured = capsys.readouterr()
        assert "[TOKEN withheld]" in captured.out
        assert CREDENTIALS.token not in captured.out + captured.err
    assert all(
        "previewUrl" not in query and "files(" not in query for query, _ in calls
    )
