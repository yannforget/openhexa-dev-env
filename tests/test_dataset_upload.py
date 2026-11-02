"""Offline dataset writes, exact-version progress and installed entry-point coverage."""

import importlib.util
import json
import os
import subprocess
from email.message import Message
from http.client import IncompleteRead
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from hexa.api import API, APIError
from hexa.api.client import NoRedirect
from hexa.cli import main
from hexa.config import Credentials, SetupError

FIXTURE = Path(__file__).parent / "fixtures" / "dataset_upload.py"
spec = importlib.util.spec_from_file_location("dataset_upload", FIXTURE)
assert spec is not None and spec.loader is not None
fake = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fake)
CREDENTIALS = Credentials("https://example.invalid", "selected", fake.TOKEN)


@pytest.fixture
def remote(monkeypatch):
    server = fake.DatasetRemote()
    monkeypatch.setattr(
        "hexa.api.client.build_opener",
        lambda *_: SimpleNamespace(open=server.open),
    )
    return server


@pytest.fixture
def files(tmp_path):
    directory = tmp_path / ".hexa"
    directory.mkdir()
    first = directory / "sales.csv"
    first.write_bytes(b"name,amount\r\nCaf\xc3\xa9,23\r\n")
    second = directory / "opaque.unknown-extension"
    second.write_bytes(b"\x00\xff" * 100000)
    return [first, second]


def test_creation_uses_workspace_and_returns_server_identity_without_assuming_version(
    remote,
):
    client = API(CREDENTIALS)
    for initial in (None, {"id": "server-initial", "name": "v1"}):
        remote.initial_version = initial
        result = client.create_dataset("Requested name", "Description")
        dataset = result["dataset"]
        assert dataset["id"] == fake.DATASET_ID and dataset["slug"] == "sales"
        assert dataset["name"] == remote.dataset["name"]
        assert dataset["workspace"] == {"slug": "selected"}
        assert dataset["latestVersion"] == initial
        query, values = remote.calls[-1]
        assert "latestVersion" in query
        assert values["input"] == {
            "workspaceSlug": "selected",
            "name": "Requested name",
            "description": "Description",
        }
    assert not remote.uploads
    remote.create_allowed = False
    count = len(remote.calls)
    with pytest.raises(APIError, match="permission"):
        client.create_dataset("Denied")
    assert len(remote.calls) == count + 1


@pytest.mark.parametrize("selector", ["sales", "source/sales", fake.DATASET_ID])
@pytest.mark.parametrize("provider_headers", [True, False])
def test_multifile_upload_is_scoped_sequential_and_pinned(
    remote, files, selector, provider_headers
):
    if not provider_headers:
        remote.storage_headers = None
    result = API(CREDENTIALS).upload_dataset_version(selector, "June", files, "Update")
    assert result["dataset"]["workspace"] == {"slug": "source"}
    assert result["version"] == {
        "id": fake.VERSION_ID,
        "name": "June",
        "dataset_id": fake.DATASET_ID,
    }
    assert result["uploaded_file_count"] == 2 and result["verified"]
    assert [item["path"] for item in result["files"]] == [path.name for path in files]
    assert remote.uploads == [path.read_bytes() for path in files]
    mutations = [
        (query, values["input"])
        for query, values in remote.calls
        if query.startswith("mutation")
    ]
    assert len(mutations) == 5
    assert mutations[0][1] == {
        "datasetId": fake.DATASET_ID,
        "name": "June",
        "changelog": "Update",
    }
    for index, path in enumerate(files):
        prepare, register = mutations[1 + index * 2 : 3 + index * 2]
        assert "generateDatasetUploadUrl(" in prepare[0]
        assert "createDatasetVersionFile(" in register[0]
        assert (
            prepare[1]
            == register[1]
            == {
                "versionId": fake.VERSION_ID,
                "uri": path.name,
                "contentType": "text/csv" if index == 0 else "application/octet-stream",
            }
        )
    assert all("versions(" not in query for query, _ in remote.calls)
    assert fake.SIGNED_URL not in json.dumps(result)


@pytest.mark.parametrize(
    "invalid",
    ["empty", "missing", "directory", "fifo", "duplicate", "unreadable", "blank-name"],
)
def test_local_validation_precedes_remote_mutations(
    remote, files, invalid, monkeypatch
):
    selected = files.copy()
    if invalid == "empty":
        selected = []
    elif invalid == "missing":
        selected.append(files[0].parent / "missing")
    elif invalid == "directory":
        selected.append(files[0].parent)
    elif invalid == "fifo":
        pipe = files[0].parent / "pipe"
        os.mkfifo(pipe)
        selected.append(pipe)
    elif invalid == "duplicate":
        directory = files[0].parent / "other"
        directory.mkdir()
        duplicate = directory / files[0].name
        duplicate.write_text("different bytes")
        selected.append(duplicate)
    elif invalid == "unreadable":
        original = Path.open

        def open_file(path, *args, **kwargs):
            if path == files[1]:
                raise PermissionError("not readable")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", open_file)
    with pytest.raises((SetupError, OSError)):
        API(CREDENTIALS).upload_dataset_version(
            "sales", " " if invalid == "blank-name" else "June", selected
        )
    assert not remote.calls and not remote.uploads


@pytest.mark.parametrize(
    "invalid", ["missing", "ambiguous", "incomplete", "owner", "permission"]
)
def test_scope_and_permission_fail_before_version_creation(remote, files, invalid):
    selector = "sales"
    client = API(CREDENTIALS, max_pages=1)
    if invalid == "missing":
        selector = "not-present"
    elif invalid == "ambiguous":
        remote.extra_dataset = {
            **remote.dataset,
            "id": "other-id",
            "workspace": {"slug": "other"},
        }
    elif invalid == "incomplete":
        remote.incomplete = True
    elif invalid == "owner":
        selector = "other/sales"
    else:
        remote.dataset["permissions"]["createVersion"] = False
    with pytest.raises(APIError):
        client.upload_dataset_version(selector, "June", files)
    assert remote.calls and all(query.startswith("query") for query, _ in remote.calls)


@pytest.mark.parametrize(
    ("stage", "failure", "registered", "rejected"),
    [
        ("create_version", "createDatasetVersion", 0, True),
        ("create_version", "createDatasetVersion", 0, False),
        ("prepare_upload", "generateDatasetUploadUrl", 0, True),
        ("prepare_upload", "generateDatasetUploadUrl", 0, False),
        ("storage_put", "storage_put", 0, False),
        ("register_file", "createDatasetVersionFile", 0, True),
        ("register_file", "createDatasetVersionFile", 0, False),
        ("register_file", "createDatasetVersionFile", 1, True),
        ("verify", "verify", 2, False),
    ],
)
def test_failures_report_exact_progress_without_retries_or_rollback(
    remote, files, stage, failure, registered, rejected
):
    remote.failure = failure
    remote.fail_at_file = registered
    remote.rejection = rejected
    with pytest.raises(APIError) as caught:
        API(CREDENTIALS).upload_dataset_version("source/sales", "June", files)
    error = caught.value
    assert error.code == ("WRITE_REJECTED" if rejected else "UNCERTAIN_WRITE")
    progress = error.details
    assert progress is not None
    assert progress["stage"] == stage
    assert progress["dataset"]["id"] == fake.DATASET_ID
    assert progress["source_owner"] == "source"
    assert len(progress["registered_files"]) == registered
    if stage == "create_version":
        assert "version" not in progress
    else:
        assert progress["version"]["id"] == fake.VERSION_ID
    if stage == "register_file":
        assert progress["storage_put"] == "succeeded"
        assert progress["registration"] == ("rejected" if rejected else "uncertain")
    if stage not in ("create_version", "verify"):
        assert progress["file"] == files[registered].name
    assert "No retry or rollback" in str(error) and "another new version" in str(error)
    assert fake.SIGNED_URL not in str(error) + json.dumps(progress)
    mutation_fields = [
        query.split("{", 1)[1].split("(", 1)[0]
        for query, _ in remote.calls
        if query.startswith("mutation")
    ]
    assert mutation_fields.count("createDatasetVersion") == 1
    assert not any("delete" in field.lower() for field in mutation_fields)
    if stage not in ("verify", "storage_put"):
        assert mutation_fields.count(failure) == registered + 1


def test_missing_version_id_and_verification_mismatch_are_uncertain(remote, files):
    client = API(CREDENTIALS)
    remote.missing_version = True
    with pytest.raises(APIError) as caught:
        client.upload_dataset_version("sales", "June", files)
    assert caught.value.code == "UNCERTAIN_WRITE"
    assert caught.value.details is not None and "version" not in caught.value.details
    assert not remote.uploads
    remote.missing_version = False
    original = remote.graphql

    def changed(query, variables):
        result = original(query, variables)
        if query.startswith("query Version"):
            result["datasetVersion"]["files"]["items"][0]["size"] = 123456
        return result

    remote.graphql = changed
    with pytest.raises(APIError) as caught:
        client.upload_dataset_version("sales", "June", files)
    assert caught.value.code == "UNCERTAIN_WRITE"
    assert caught.value.details is not None
    assert caught.value.details["stage"] == "verify"
    assert len(caught.value.details["registered_files"]) == 2


@pytest.mark.parametrize("status", [403, 408, 500])
def test_storage_rejection_or_uncertainty_never_registers(
    remote, files, monkeypatch, status
):
    client = API(CREDENTIALS)
    original = remote.open

    def rejected(request, timeout):
        if not request.full_url.endswith("/graphql/"):
            raise HTTPError(fake.SIGNED_URL, status, "private", Message(), None)
        return original(request, timeout)

    monkeypatch.setattr(client.opener, "open", rejected)
    with pytest.raises(APIError) as caught:
        client.upload_dataset_version("sales", "June", files)
    assert caught.value.code == (
        "STORAGE_REJECTED" if status == 403 else "UNCERTAIN_WRITE"
    )
    assert caught.value.details is not None
    assert caught.value.details["storage_put"] == (
        "rejected" if status == 403 else "uncertain"
    )
    assert not remote.uploads and not remote.registered


@pytest.mark.parametrize("unsafe", ["headers", "http", "malformed", "redirect"])
def test_storage_safety_refuses_unsafe_requests_without_registration(
    remote, files, monkeypatch, unsafe
):
    client = API(CREDENTIALS)
    if unsafe == "headers":
        remote.storage_headers["Authorization"] = "Bearer " + fake.TOKEN
    elif unsafe in {"http", "malformed"}:
        original_graphql = remote.graphql

        def insecure_url(query, variables):
            result = original_graphql(query, variables)
            if "generateDatasetUploadUrl(" in query:
                result["generateDatasetUploadUrl"]["uploadUrl"] = (
                    "http://storage.invalid/unsafe" if unsafe == "http" else "https://["
                )
            return result

        monkeypatch.setattr(remote, "graphql", insecure_url)
    else:
        original = remote.open

        def refused(request, timeout):
            if not request.full_url.endswith("/graphql/"):
                NoRedirect().redirect_request(
                    request, None, 307, "redirect", Message(), "https://other.invalid"
                )
            return original(request, timeout)

        monkeypatch.setattr(client.opener, "open", refused)
    with pytest.raises(APIError) as caught:
        client.upload_dataset_version("sales", "June", files)
    assert caught.value.details is not None
    assert caught.value.details["version"]["id"] == fake.VERSION_ID
    assert caught.value.details["registration"] == "not_started"
    assert not remote.uploads and not remote.registered


@pytest.mark.parametrize("stage", ["create_version", "storage_put"])
def test_truncated_http_response_is_uncertain_and_preserves_known_identity(
    remote, files, monkeypatch, stage
):
    original = remote.open

    def truncated(request, timeout):
        if (stage == "storage_put" and not request.full_url.endswith("/graphql/")) or (
            stage == "create_version" and b"createDatasetVersion(" in request.data
        ):
            raise IncompleteRead(b"private " + fake.SIGNED_URL.encode())
        return original(request, timeout)

    client = API(CREDENTIALS)
    monkeypatch.setattr(client.opener, "open", truncated)
    with pytest.raises(APIError) as caught:
        client.upload_dataset_version("sales", "June", files)
    assert caught.value.code == "UNCERTAIN_WRITE"
    assert caught.value.details is not None
    assert caught.value.details["stage"] == stage
    assert ("version" in caught.value.details) == (stage == "storage_put")
    assert "private" not in str(caught.value)
    assert caught.value.details["registered_files"] == []


def test_local_failure_after_version_creation_keeps_progress(
    remote, files, monkeypatch
):
    client = API(CREDENTIALS)
    original = remote.graphql

    def removed(query, variables):
        result = original(query, variables)
        if "createDatasetVersion(" in query:
            files[0].unlink()
        return result

    monkeypatch.setattr(remote, "graphql", removed)
    with pytest.raises(APIError) as caught:
        client.upload_dataset_version("sales", "June", files)
    assert caught.value.code == "LOCAL_IO_FAILURE"
    assert caught.value.details is not None
    assert caught.value.details["version"]["id"] == fake.VERSION_ID
    assert caught.value.details["registered_files"] == []
    assert caught.value.details["storage_put"] == "not_started"
    assert not remote.uploads


@pytest.mark.parametrize("rejected", [True, False])
def test_dataset_creation_failure_retains_workspace_without_inventing_ids(
    remote, rejected
):
    remote.failure = "createDataset"
    remote.rejection = rejected
    with pytest.raises(APIError) as caught:
        API(CREDENTIALS).create_dataset("Requested")
    assert caught.value.code == ("WRITE_REJECTED" if rejected else "UNCERTAIN_WRITE")
    assert caught.value.details is not None
    assert caught.value.details["workspace"] == "selected"
    assert caught.value.details["name"] == "Requested"
    assert "id" not in caught.value.details
    assert sum(query.startswith("mutation") for query, _ in remote.calls) == 1


def test_cli_literal_success_and_redacted_partial_failure(remote, files, capsys):
    directory = files[0].parent
    (directory / ".env").write_text(
        f"OPENHEXA_URL={CREDENTIALS.origin}\nOPENHEXA_WORKSPACE=selected\nOPENHEXA_TOKEN={fake.TOKEN}\n"
    )
    config = directory / "hexa.json"
    config.write_text("{}")
    for json_mode in (False, True):
        options = ["--config", str(config), *(["--json"] if json_mode else [])]
        assert main(["datasets", "create", "Requested", *options]) == 0
        captured = capsys.readouterr()
        assert (
            "[bold]" in captured.out and fake.TOKEN not in captured.out + captured.err
        )
        assert '"origin": "https://example.invalid"' in captured.err
        assert "selected" in captured.err
        if json_mode:
            assert json.loads(captured.out)["dataset"]["latestVersion"] is None
        else:
            assert fake.DATASET_ID in captured.out and "Dataset created" in captured.out
        remote.registered.clear()
        args = [
            "datasets",
            "upload",
            "source/sales",
            *map(str, files),
            "--name",
            "June",
            *options,
        ]
        assert main(args) == 0
        captured = capsys.readouterr()
        assert "source" in captured.err and fake.DATASET_ID in captured.err
        assert fake.VERSION_ID in captured.out
        if json_mode:
            assert json.loads(captured.out)["uploaded_file_count"] == 2
        else:
            assert "Uploaded files: 2" in captured.out
        remote.registered.clear()
        remote.failure = "createDatasetVersionFile"
        assert main(args) == 3
        captured = capsys.readouterr()
        output = captured.out + captured.err
        assert fake.TOKEN not in output and "signed=private" not in output
        assert fake.DATASET_ID in output and fake.VERSION_ID in output
        assert "register_file" in output and "No retry or rollback" in output
        if json_mode:
            detail = json.loads(captured.out)
            assert detail["target"]["registered_files"] == []
            assert detail["target"]["storage_put"] == "succeeded"
        else:
            assert captured.out == "" and "Target:" in captured.err
        remote.failure = None


def test_installed_wheel_dataset_commands(installed_hexa, files):
    directory = files[0].parent
    (directory / ".env").write_text(
        f"OPENHEXA_URL={CREDENTIALS.origin}\nOPENHEXA_WORKSPACE=selected\nOPENHEXA_TOKEN={fake.TOKEN}\n"
    )
    (directory / "hexa.json").write_text("{}")
    for command in ("create", "upload"):
        result = subprocess.run(
            [str(installed_hexa), "datasets", command, "--help"],
            cwd=directory,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert ("--description" if command == "create" else "--name") in result.stdout
    python = installed_hexa.parent.parent / "tools" / "hexa" / "bin" / "python"
    script = (
        "import runpy,sys; "
        f"remote=runpy.run_path({str(FIXTURE)!r})['DatasetRemote'](); remote.install(); "
        "from hexa.cli import main; sys.exit(main(sys.argv[1:]))"
    )
    for args in (
        ["create", "Requested", "--description", "Monthly"],
        ["upload", "source/sales", *map(str, files), "--name", "June"],
    ):
        result = subprocess.run(
            [str(python), "-c", script, "datasets", *args, "--json"],
            cwd=directory,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        data = json.loads(result.stdout)
        assert data["dataset"]["id"] == fake.DATASET_ID
        assert fake.TOKEN not in result.stdout + result.stderr
        if args[0] == "upload":
            assert (
                data["version"]["id"] == fake.VERSION_ID
                and data["uploaded_file_count"] == 2
            )


def test_directory_upload_round_trips_byte_identical(remote, tmp_path):
    tree = tmp_path / "package"
    contents = {
        "source.json": b'{"done": true}',
        "data/events/2025.csv": b"a,b\n1,2\n",
        "data/meta.parquet": b"\x00PAR1\xff",
    }
    for relative, data in contents.items():
        (tree / relative).parent.mkdir(parents=True, exist_ok=True)
        (tree / relative).write_bytes(data)
    for skipped in (".git/config", "__pycache__/x.pyc", ".hexa/runs/log.txt"):
        (tree / skipped).parent.mkdir(parents=True, exist_ok=True)
        (tree / skipped).write_bytes(b"skip")
    api = API(CREDENTIALS)
    result = api.upload_dataset_directory(
        fake.DATASET_ID, "v3", tree, last="source.json"
    )
    assert remote.uris == ["data/events/2025.csv", "data/meta.parquet", "source.json"]
    assert sorted(item["path"] for item in result["files"]) == sorted(contents)
    remote.dataset["latestVersion"] = {
        "id": fake.VERSION_ID,
        "name": "v3",
        "permissions": {"download": True},
    }
    target = tmp_path / "downloaded"
    api.download_version(fake.DATASET_ID, None, target)
    downloaded = {
        path.relative_to(target).as_posix(): path.read_bytes()
        for path in target.rglob("*")
        if path.is_file()
    }
    assert downloaded == contents


@pytest.mark.parametrize("bad", ["dotenv", "nested-dotenv", "token", "escaping-link"])
def test_directory_upload_refuses_secrets_before_any_write(remote, tmp_path, bad):
    tree = tmp_path / "package"
    tree.mkdir()
    (tree / "ok.csv").write_text("x\n")
    if bad == "dotenv":
        (tree / ".env").write_text("A=1")
    elif bad == "nested-dotenv":
        (tree / "config").mkdir()
        (tree / "config" / ".env.local").write_text("A=1")
    elif bad == "token":
        (tree / "leak.txt").write_text("prefix " + fake.TOKEN)
    else:
        (tree / "link").symlink_to(tmp_path)
    with pytest.raises(SetupError):
        API(CREDENTIALS).upload_dataset_directory(fake.DATASET_ID, "v3", tree)
    assert remote.calls == []


def test_directory_upload_cli_options(remote, files, capsys):
    directory = files[0].parent
    (directory.parent / ".env").write_text(
        f"OPENHEXA_URL={CREDENTIALS.origin}\nOPENHEXA_WORKSPACE=selected\nOPENHEXA_TOKEN={fake.TOKEN}\n"
    )
    config = directory.parent / "hexa.json"
    config.write_text("{}")
    tree = directory.parent / "package"
    (tree / "nested").mkdir(parents=True)
    (tree / "nested" / "a.csv").write_text("x\n")
    base = ["datasets", "upload", "source/sales", "--name", "v3"]
    options = ["--config", str(config), "--json"]
    for extra in ([str(files[0]), "--dir", str(tree)], [str(files[0]), "--last", "a"]):
        assert main([*base, *extra, *options]) == 2
        assert json.loads(capsys.readouterr().out)["code"] == "INVALID_USAGE"
    assert remote.calls == []
    assert main([*base, "--dir", str(tree), "--last", "missing", *options]) == 2
    assert "--last" in json.loads(capsys.readouterr().out)["message"]
    assert main([*base, "--dir", str(tree), *options]) == 0
    assert json.loads(capsys.readouterr().out)["files"][0]["path"] == "nested/a.csv"
    assert remote.uris == ["nested/a.csv"]
