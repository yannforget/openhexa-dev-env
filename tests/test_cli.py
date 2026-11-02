"""Focused public-interface regressions; no credentials or network required."""

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from rich.text import Text

from hexa.api import API, APIError
from hexa.cli import main
from hexa.config import (
    PACKAGE_DIR,
    Config,
    Credentials,
    Project,
    SetupError,
    load_credentials,
)
from hexa.security import Redactor


def config_for(tmp_path: Path) -> Path:
    """Create an isolated configuration with a synthetic credential source."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "pipeline.py").write_text("raise RuntimeError('unused')\n")
    (tmp_path / ".env").write_text(
        "OPENHEXA_URL=https://example.invalid/graphql/\nOPENHEXA_WORKSPACE=test\nOPENHEXA_TOKEN=synthetic-token\n"
    )
    data = {
        "credential_file": ".env",
        "source": {"path": "source", "relabel": False},
        "parameters": {},
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data))
    return path


@pytest.mark.parametrize("linked", [False, True])
def test_hexa_entrypoint_from_another_directory(tmp_path, monkeypatch, linked):
    installed = Path(sys.executable).with_name("hexa")
    launcher = str(installed)
    if linked:
        local_bin = tmp_path / "bin"
        local_bin.mkdir()
        (local_bin / "hexa").symlink_to(installed)
        monkeypatch.setenv("PATH", str(local_bin) + os.pathsep + os.environ["PATH"])
        launcher = "hexa"
    result = subprocess.run(
        [launcher, "--help"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.stdout.startswith("Usage: hexa ")
    assert "run" in result.stdout and "datasets" in result.stdout
    assert "\x1b[" not in result.stdout and "╭" not in result.stdout
    with pytest.raises(subprocess.CalledProcessError) as error:
        subprocess.run(
            [launcher, "workspace", "show", "--config", "missing.json", "--json"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    assert error.value.returncode == 2
    assert json.loads(error.value.stdout)["exit_code"] == 2


@pytest.mark.parametrize("json_mode", [False, True])
@pytest.mark.parametrize("selection", ["default", "discovered", "relative", "absolute"])
def test_missing_credentials_explain_setup_for_the_selected_file(
    json_mode, selection, tmp_path, monkeypatch, capsys
):
    caller = tmp_path / "caller"
    caller.mkdir()
    monkeypatch.chdir(caller)
    args = ["workspace", "show"]
    credential = caller / ".env"
    if selection == "discovered":
        (caller / "hexa.json").write_text("{}")
    elif selection in ("relative", "absolute"):
        directory = tmp_path / "settings"
        directory.mkdir()
        credential = directory / "workspace.env"
        config = directory / "project.json"
        config.write_text(
            json.dumps(
                {
                    "credential_file": credential.name
                    if selection == "relative"
                    else str(credential)
                }
            )
        )
        args += ["--config", str(config)]
    monkeypatch.setenv("OPENHEXA_TOKEN", "ambient-secret")
    files_before = set(tmp_path.rglob("*"))
    if json_mode:
        args.append("--json")
    assert main(args) == 2
    captured = capsys.readouterr()
    if json_mode:
        detail = json.loads(captured.out)
        assert detail["code"] == "INVALID_INPUT" and detail["exit_code"] == 2
        message = detail["message"]
        assert captured.err == ""
    else:
        assert captured.out == ""
        assert captured.err.startswith("Error: ")
        message = captured.err.removeprefix("Error: ").rstrip("\n")
    assert message == (
        f"Credential file not found: {credential}\n\n"
        "Create this file with your OpenHEXA workspace credentials:\n\n"
        "  OPENHEXA_URL=<your-openhexa-https-url>\n"
        "  OPENHEXA_WORKSPACE=my-workspace\n"
        "  OPENHEXA_TOKEN=your-workspace-token\n\n"
        "Keep this file private and out of version control.\n"
        "To use an existing credential file, set credential_file in hexa.json."
    )
    assert set(tmp_path.rglob("*")) == files_before


@pytest.mark.parametrize("json_mode", [False, True])
@pytest.mark.parametrize("resource", ["credentials", "project"])
def test_unreadable_setup_files_identify_the_file_without_exposing_the_exception(
    json_mode, resource, tmp_path, monkeypatch, capsys
):
    config = config_for(tmp_path)
    denied = tmp_path / ".env" if resource == "credentials" else config
    read_text = Path.read_text

    def unreadable(path, *args, **kwargs):
        if path == denied:
            raise PermissionError("synthetic-secret")
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    args = ["workspace", "show", "--config", str(config)]
    if json_mode:
        args.append("--json")
    assert main(args) == 2
    captured = capsys.readouterr()
    if json_mode:
        detail = json.loads(captured.out)
        assert detail["code"] == "INVALID_INPUT" and detail["exit_code"] == 2
        message = detail["message"]
        assert captured.err == ""
    else:
        assert captured.out == ""
        message = captured.err.removeprefix("Error: ").rstrip("\n")
    label = "credential" if resource == "credentials" else "project configuration"
    assert message == (
        f"Cannot read {label} file: {denied}\n\n"
        "Check that you have permission to read this file."
    )
    assert "synthetic-secret" not in captured.out + captured.err


@pytest.mark.parametrize("json_mode", [False, True])
def test_missing_explicit_config_identifies_project_setup_not_credentials(
    json_mode, tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    args = ["workspace", "show", "--config", "missing.json"]
    if json_mode:
        args.append("--json")
    assert main(args) == 2
    captured = capsys.readouterr()
    if json_mode:
        detail = json.loads(captured.out)
        assert detail["code"] == "INVALID_INPUT" and detail["exit_code"] == 2
        message = detail["message"]
        assert captured.err == ""
    else:
        assert captured.out == ""
        message = captured.err.removeprefix("Error: ").rstrip("\n")
    assert message == (
        f"Project configuration file not found: {tmp_path / 'missing.json'}\n\n"
        "Check the --config path, or omit --config to use ./hexa.json when present."
    )


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        (["datasets", "show", "--json"], "MISSING_ARGUMENT"),
        (["datasets", "files", "--json"], "MISSING_ARGUMENT"),
        (["datasets", "download", "sample", "--json"], "MISSING_ARGUMENT"),
        (["datasets", "list", "--limit", "invalid", "--json"], "INVALID_USAGE"),
        (["datasets", "list", "--limit", "0", "--json"], "INVALID_USAGE"),
        (["datasets", "list", "--unknown", "--json"], "INVALID_USAGE"),
        (["unknown", "--json"], "INVALID_USAGE"),
        (["datasets", "list", "--json", "--config"], "INVALID_USAGE"),
    ],
)
def test_agent_parser_errors_are_json_without_loading_credentials(
    arguments, code, monkeypatch, capsys
):
    def unexpected_load(path):
        pytest.fail("Parser errors must not read configuration or credentials")

    monkeypatch.setattr(Project, "load", unexpected_load)
    assert main(arguments) == 2
    captured = capsys.readouterr()
    error = json.loads(captured.out)
    assert error["exit_code"] == 2 and error["code"] == code
    assert error["message"] and captured.err == ""
    assert "\x1b[" not in captured.out


def test_human_and_json_rendering_keep_redaction_and_literal_markup(
    tmp_path, monkeypatch, capsys
):
    config = config_for(tmp_path)
    monkeypatch.setattr(
        API,
        "workspace",
        lambda self: {
            "name": "[bold]literal[/bold]",
            "note": "synthetic-token https://storage.invalid/?signature=secret",
        },
    )
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    assert main(["workspace", "show", "--config", str(config)]) == 0
    captured = capsys.readouterr()
    assert "[bold]literal[/bold]" in Text.from_ansi(captured.out).plain
    assert "synthetic-token" not in captured.out
    assert main(["workspace", "show", "--config", str(config), "--json"]) == 0
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["name"] == "[bold]literal[/bold]"
    assert result["note"].startswith("[TOKEN withheld]")
    assert "\x1b[" not in captured.out and captured.err == ""


@pytest.mark.parametrize("terminal", [False, True])
def test_detail_layout_keeps_metadata_scannable_and_description_last(
    terminal, tmp_path, monkeypatch, capsys
):
    config = config_for(tmp_path)
    description = "# Notes\n\n[bold]literal[/bold]\n[Files](/files)\n\n- item"
    workspace = {
        "origin": "https://example.invalid",
        "slug": "test",
        "name": "Test workspace",
        "description": description,
        "updatedAt": "2026-10-13T17:32:04.816Z",
        "dockerImage": None,
        "tags": [],
        "permissions": {"update": True, "manageTags": False},
    }
    monkeypatch.setattr(sys.stdout, "isatty", lambda: terminal)
    monkeypatch.setattr(API, "workspace", lambda self: workspace)
    assert main(["workspace", "show", "--config", str(config)]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    lines = captured.out.splitlines()
    assert lines[0].startswith("Name ")
    metadata = lines[: lines.index("")]
    assert metadata[0].index("Test workspace") == metadata[1].index("test")
    assert "Docker image  Not configured" in captured.out
    assert "Tags          None" in captured.out
    assert "  Update       Yes\n  Manage tags  No" in captured.out
    assert captured.out.endswith("Description\n───────────\n" + description + "\n")
    assert "\x1b[" not in captured.out
    assert main(["workspace", "show", "--config", str(config), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == workspace


@pytest.mark.parametrize("terminal", [False, True])
def test_connection_fields_are_aligned_records_not_yaml_fragments(
    terminal, tmp_path, monkeypatch, capsys
):
    config = config_for(tmp_path)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: terminal)
    monkeypatch.setattr(
        API,
        "connections",
        lambda self: {
            "items": [
                {
                    "id": "connection-id",
                    "slug": "test-connection",
                    "name": "Test connection",
                    "description": "Connection notes",
                    "type": "CUSTOM",
                    "fields": [
                        {"code": "url", "secret": False},
                        {"code": "password", "secret": True},
                    ],
                    "permissions": {"update": True, "delete": False},
                }
            ],
            "complete": True,
        },
    )
    assert (
        main(["connections", "show", "test-connection", "--config", str(config)]) == 0
    )
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "Fields\n  Code      Secret\n  url       No\n  password  Yes" in captured.out
    assert "Permissions\n  Update  Yes\n  Delete  No" in captured.out
    assert captured.out.endswith("Description\n───────────\nConnection notes\n")
    assert "\n  -\n" not in captured.out


def test_context_notice_precedes_literal_documents_and_absence_is_concise(
    tmp_path, monkeypatch, capsys
):
    config = config_for(tmp_path)
    text = "# Remote notes\n\n[bold]literal[/bold] synthetic-token\n"
    monkeypatch.setattr(
        API,
        "workspace",
        lambda self: {
            "slug": "test",
            "description": text,
            "permissions": {"downloadObject": True},
        },
    )
    monkeypatch.setattr(
        API, "graphql", lambda *a: {"workspace": {"bucket": {"object": None}}}
    )
    assert main(["workspace", "context", "--config", str(config)]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.index("Notice:") < captured.out.index("workspace.description")
    assert text.replace("synthetic-token", "[TOKEN withheld]") in captured.out
    assert captured.out.endswith("bucket:AGENTS.md  Not present\n")
    assert "None" not in captured.out and "null" not in captured.out
    assert main(["workspace", "context", "--config", str(config), "--json"]) == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "content_notice" in data and "trust" not in data
    assert data["documents"][0]["identity"] == {
        "origin": "https://example.invalid",
        "workspace": "test",
        "slug": "test",
        "source_owner": "test",
    }
    assert data["documents"][1]["present"] is False
    assert data["documents"][1]["text"] is None
    assert captured.err == "" and "synthetic-token" not in captured.out


@pytest.mark.parametrize("json_mode", [False, True])
def test_usage_and_api_errors_share_a_public_contract(
    json_mode, tmp_path, monkeypatch, capsys
):
    config = config_for(tmp_path)
    options = ["--config", str(config), *(["--json"] if json_mode else [])]
    assert main(["connections", "show", *options]) == 2
    captured = capsys.readouterr()
    if json_mode:
        detail = json.loads(captured.out)
        assert detail["code"] == "MISSING_ARGUMENT" and detail["exit_code"] == 2
        assert detail["message"] == "Missing required argument CONNECTION."
        assert detail["parameter"] == "connection"
        assert detail["usage"].startswith("Usage: hexa connections show")
        assert detail["hint"] == 'Run "hexa connections show --help" for help.'
        assert "error" not in detail and captured.err == ""
    else:
        assert captured.out == ""
        assert captured.err.startswith(
            "Error: Missing required argument CONNECTION.\nUsage:"
        )
        assert 'Hint: Run "hexa connections show --help" for help.' in captured.err
    monkeypatch.setattr(
        API, "connections", lambda self: {"items": [], "complete": True}
    )
    assert main(["connections", "show", "missing", *options]) == 3
    captured = capsys.readouterr()
    message = 'Connection "missing" was not found or is not accessible.'
    if json_mode:
        detail = json.loads(captured.out)
        assert (
            detail["code"] == "NOT_FOUND_OR_INACCESSIBLE" and detail["exit_code"] == 3
        )
        assert detail["message"] == message
        assert detail["target"] == {"resource": "connection", "selector": "missing"}
        assert (
            detail["hint"]
            == 'Run "hexa connections list" to see available connections.'
        )
        assert "error" not in detail and captured.err == ""
    else:
        assert captured.out == ""
        assert (
            captured.err
            == f'Error: {message}\nHint: Run "hexa connections list" to see available connections.\n'
        )


def test_credentials_allow_unrelated_assignments_without_evaluation(tmp_path):
    env = tmp_path / ".env"
    sentinel = tmp_path / "sentinel"
    env.write_text(
        "# Shared project configuration\n\n"
        "DATABASE_URL=postgresql://user:secret@localhost/db\n"
        "OPENHEXA_URL=https://example.invalid/graphql/\n"
        'OTHER_SECRET="spaces and ${INTERPOLATION}"\n'
        "OPENHEXA_WORKSPACE='test'\n"
        f"OTHER_SECRET=$(touch {sentinel})\n"
        "OPENHEXA_TOKEN=synthetic-token\n"
        "OPENHEXA_OTHER=value\n"
    )

    credentials = load_credentials(env)

    assert credentials == Credentials(
        "https://example.invalid", "test", "synthetic-token"
    )
    assert not sentinel.exists()


@pytest.mark.parametrize(
    ("assignment", "message"),
    [
        (
            "OPENHEXA_TOKEN=synthetic-token\nOPENHEXA_TOKEN=another-token",
            "Duplicate credential key: OPENHEXA_TOKEN",
        ),
        ("OPENHEXA_TOKEN=${OTHER_SECRET}", "unsupported literal"),
        ("OPENHEXA_TOKEN='unterminated", "Unsupported quoting"),
        ("OPENHEXA_TOKEN", "Unsupported credential assignment at line 4"),
    ],
)
def test_shared_credentials_keep_strict_openhexa_validation(
    tmp_path, assignment, message
):
    env = tmp_path / ".env"
    env.write_text(
        "OTHER_SECRET=unrelated-secret\n"
        "OPENHEXA_URL=https://example.invalid\n"
        "OPENHEXA_WORKSPACE=test\n"
        f"{assignment}\n"
    )
    with pytest.raises(SetupError, match=message) as error:
        load_credentials(env)
    assert "unrelated-secret" not in str(error.value)
    assert "synthetic-token" not in str(error.value)


def test_no_ambient_credentials_or_evaluation(tmp_path, monkeypatch, capsys):
    config = config_for(tmp_path)
    monkeypatch.setenv("OPENHEXA_TOKEN", "ambient-secret-must-not-be-used")
    env = tmp_path / ".env"
    env.write_text("OPENHEXA_URL=https://example.invalid\nOPENHEXA_WORKSPACE=test\n")
    assert main(["workspace", "show", "--config", str(config), "--json"]) == 2
    captured = capsys.readouterr()
    assert json.loads(captured.out)["exit_code"] == 2
    assert "OPENHEXA_TOKEN" in json.loads(captured.out)["message"]
    assert "ambient-secret" not in captured.err
    env.write_text("OPENHEXA_TOKEN=$(touch sentinel)\n")
    with pytest.raises(SetupError, match="literal"):
        load_credentials(env)
    assert not (tmp_path / "sentinel").exists()


def test_narrow_mounts_and_stored_relabel_consent(tmp_path, capsys, monkeypatch):
    path = config_for(tmp_path)
    (tmp_path / "source" / ".env").write_text("PRIVATE=value")
    with pytest.raises(SetupError, match="dotenv"):
        Config.load(path)
    (tmp_path / "source" / ".env").unlink()
    data = json.loads(path.read_text())
    data["source"]["relabel"] = True
    path.write_text(json.dumps(data))

    def execute(config, credentials):
        assert config.source.relabel is True
        assert credentials.workspace == "test"
        return {"exit_code": 0}

    monkeypatch.setattr("hexa.cli.run", execute)
    assert main(["run", "--config", str(path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["exit_code"] == 0


def test_split_output_final_line_and_oversized_line_redaction():
    token = "synthetic-token"
    text = (
        "worker: "
        + token
        + " https://storage.invalid/file?signature=secret\nValueError: bad\nhttps://final.invalid/?key=final"
    )
    result = io.StringIO()
    Redactor(token).stream(io.StringIO(text), result)
    assert token not in result.getvalue()
    assert "signature" not in result.getvalue() and "key=final" not in result.getvalue()
    assert "ValueError: bad" in result.getvalue()
    result = io.StringIO()
    Redactor(token).stream(io.StringIO("a" * 65530 + token + "\n"), result)
    assert result.getvalue() == "[oversized output line withheld]\n"


def test_workspace_scoped_pagination_and_metadata_only(monkeypatch):
    requests = []

    def graphql(self, query, variables):
        requests.append((query, variables))
        page = len(requests)
        return {
            "workspace": {
                "datasets": {
                    "pageNumber": page,
                    "totalPages": 2,
                    "totalItems": 2,
                    "items": [
                        {"id": str(page), "dataset": {"workspace": {"slug": "owner"}}}
                    ],
                }
            }
        }

    monkeypatch.setattr(API, "_graphql", graphql)
    api = API(
        Credentials("https://example.invalid", "selected", "synthetic"), max_pages=1
    )
    limited = api.datasets()
    assert limited["complete"] is False and limited["pages_read"] == 1
    assert limited["incomplete_reason"] == "page_ceiling"
    requests.clear()
    api.max_pages = 2
    complete = api.datasets()
    assert complete["complete"] is True and len(complete["items"]) == 2
    assert "incomplete_reason" not in complete
    assert [variables for _, variables in requests] == [
        {"ws": "selected", "query": None, "page": number, "size": 50}
        for number in (1, 2)
    ]
    requests.clear()
    limited = api.datasets(limit=1)
    assert limited["complete"] is False and limited["pages_read"] == 1
    assert limited["incomplete_reason"] == "limit"
    requests.clear()
    api.max_pages = 1
    assert api.datasets(limit=1)["incomplete_reason"] == "page_ceiling"

    def connection_query(self, query, variables):
        assert "value" not in query and "fields { code secret }" in query
        return {"workspace": {"connections": []}}

    monkeypatch.setattr(API, "_graphql", connection_query)
    assert api.connections()["complete"] is True


def test_wrong_version_never_downloads(monkeypatch, tmp_path):
    queries = []

    def graphql(self, query, variables):
        queries.append(query)
        if "Workspace" in query:
            return {
                "workspace": {
                    "datasets": {
                        "pageNumber": 1,
                        "totalPages": 1,
                        "totalItems": 1,
                        "items": [
                            {
                                "id": "link",
                                "dataset": {
                                    "id": "selected",
                                    "slug": "dataset",
                                    "workspace": {"slug": "selected"},
                                },
                            }
                        ],
                    }
                }
            }
        return {
            "dataset": {
                "versions": {
                    "pageNumber": 1,
                    "totalPages": 1,
                    "totalItems": 1,
                    "items": [{"id": "other", "name": "v1"}],
                }
            }
        }

    monkeypatch.setattr(API, "_graphql", graphql)
    api = API(Credentials("https://example.invalid", "selected", "synthetic"))
    with pytest.raises(APIError, match="not found or is not accessible"):
        api.download("dataset", "wrong", "file", tmp_path / "output")
    assert len(queries) == 2 and all("mutation" not in query for query in queries)
    assert not (tmp_path / "output").exists()


def test_startup_adapter_gate_and_fail_closed():
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("HEXA_", "OPENHEXA_"))
    }
    env.update(
        PYTHONPATH=str(PACKAGE_DIR / "runtime"),
        HEXA_CONNECTED_LOCAL="1",
        HEXA_ENVIRONMENT="LOCAL_PIPELINE",
        HEXA_SERVER_URL="https://example.invalid",
    )
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import os
import sitecustomize
from openhexa.sdk.pipelines.run import CurrentRun
assert sitecustomize.adapter_ready
assert not CurrentRun()._connected
os.environ['HEXA_ENVIRONMENT'] = 'CLOUD_PIPELINE'
assert CurrentRun()._connected
os.environ['HEXA_ENVIRONMENT'] = 'LOCAL_PIPELINE'
os.environ.pop('HEXA_CONNECTED_LOCAL')
assert CurrentRun()._connected
""",
        ],
        env=env,
        check=True,
        capture_output=True,
        timeout=30,
    )
    other_sdk = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import importlib.metadata as m; real = m.version; "
                "m.version = lambda name: '2.22.6' if name == 'openhexa.sdk' "
                "else real(name); "
                f"import sys; sys.path.insert(0, {str(PACKAGE_DIR / 'runtime')!r}); "
                "import sitecustomize; assert sitecustomize.adapter_ready"
            ),
        ],
        env={**env, "PYTHONPATH": ""},
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert "openhexa.sdk 2.22.6" in other_sdk.stderr
    env["HEXA_ENVIRONMENT"] = "CLOUD_PIPELINE"
    with pytest.raises(subprocess.CalledProcessError) as result:
        subprocess.run(
            [sys.executable, "-c", "print('must not execute')"],
            env=env,
            check=True,
            capture_output=True,
            timeout=30,
        )
    assert result.value.returncode == 78
    assert b"must not execute" not in result.value.stdout
    assert result.value.stderr == (
        b"hexa: startup adapter refused: "
        b"connected-local startup requires LOCAL_PIPELINE\n"
    )


@pytest.mark.parametrize(
    "command",
    sorted(
        path.stem
        for path in (Path(__file__).parent / "fixtures" / "json_shapes").glob("*.json")
    ),
)
def test_json_shape_in_help_matches_recorded_output(command, capsys, json_shape):
    group, name = command.split("_")
    assert main([group, name, "--help"]) == 0
    data = json.loads(
        (
            Path(__file__).parent / "fixtures" / "json_shapes" / f"{command}.json"
        ).read_text()
    )
    json_shape(capsys.readouterr().out, data)


def test_sdk_mutations_are_recorded_without_variables(tmp_path):
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("HEXA_", "OPENHEXA_"))
    }
    env.update(
        PYTHONPATH=str(PACKAGE_DIR / "runtime"),
        HEXA_CONNECTED_LOCAL="1",
        HEXA_ENVIRONMENT="LOCAL_PIPELINE",
        HEXA_SERVER_URL="https://example.invalid",
        HEXA_TOKEN="synthetic-token",
        HEXA_RECORD_DIR=str(tmp_path),
    )
    script = """
import json
import requests
from openhexa.sdk.utils import graphql

def send(adapter, request, **kwargs):
    query = json.loads(request.body)["query"]
    field = query.split("{", 2)[1].split("(")[0].strip()
    data = {"success": True, "errors": []}
    if field == "createDatasetVersion":
        data["version"] = {"id": "5b53aaaa", "name": "v2", "secret": "x"}
    response = requests.Response()
    response.status_code = 200
    response._content = json.dumps({"data": {field: data}}).encode()
    response.request = request
    return response

requests.adapters.HTTPAdapter.send = send
graphql("query { me { id } }")
graphql("mutation m($input: X!) { createDatasetVersion(input: $input) { success } }",
        {"input": {"name": "private-variable"}})
graphql("mutation { generateDatasetUploadUrl(input: {}) { success } }")
"""
    subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        check=True,
        capture_output=True,
        timeout=30,
    )
    ledger = (tmp_path / "writes.jsonl").read_text()
    assert [json.loads(line) for line in ledger.splitlines()] == [
        {
            "operation": "createDatasetVersion",
            "success": True,
            "version": {"id": "5b53aaaa", "name": "v2"},
        }
    ]
    assert "private-variable" not in ledger
