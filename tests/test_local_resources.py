"""Local resource preflight, SDK compatibility and private transport, without containers."""

import copy
import io
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import requests
from openhexa.sdk import workspace
from openhexa.sdk.datasets.dataset import Dataset, DatasetFile, DatasetVersion
from openhexa.sdk.pipelines.parameter import Parameter
from openhexa.sdk.workspaces.connection import GCSConnection
from openhexa.sdk.workspaces.current_workspace import CurrentWorkspace

from hexa.cli import main
from hexa.config import PACKAGE_DIR, Config, SetupError, load_credentials
from hexa.resources import load_connections, load_resources
from hexa.runner import container_mounts, run
from hexa.runtime import local_connections, local_datasets
from hexa.runtime.file_server import serving
from hexa.runtime.resource_manifest import validate_connections, validate_datasets
from hexa.security import Redactor


@pytest.fixture
def selection(tmp_path):
    source = tmp_path / "project" / "source"
    source.mkdir(parents=True)
    (source / "pipeline.py").write_text(
        'from openhexa.sdk import pipeline\n@pipeline("Example")\ndef example(): pass\n'
    )
    (source.parent / ".env").write_text(
        "OPENHEXA_URL=https://example.invalid\nOPENHEXA_WORKSPACE=test\nOPENHEXA_TOKEN=synthetic-token\n"
    )
    (source.parent / "hexa.json").write_text(
        json.dumps({"source": {"path": "source", "relabel": False}})
    )
    dataset = tmp_path / "caller" / "population"
    for version in ("v10", "v2"):
        (dataset / version / "nested").mkdir(parents=True)
        (dataset / version / "data.csv").write_bytes(b"value\n42\n")
        (dataset / version / "nested" / "other.txt").write_bytes(b"other")
    return Config.load(source.parent / "hexa.json"), dataset


def test_cli_paths_preflight_and_writable_mount_scope(selection, monkeypatch, capsys):
    config, dataset = selection
    monkeypatch.chdir(dataset.parent)
    connection = dataset.parent / "connections.env"
    connection.write_text(
        "TEST=DHIS2\nTEST_URL=https://example.test\nTEST_USERNAME=user\nTEST_PASSWORD='private value'\n"
    )
    observed = []

    def execute(settings, credentials):
        observed.append(settings)
        return {"exit_code": 0}

    monkeypatch.setattr("hexa.cli.run", execute)
    assert (
        main(
            [
                "run",
                "--config",
                str(config.project_dir / "hexa.json"),
                "--local-dataset",
                "population=population",
                "--connections-file",
                "connections.env",
                "--json",
            ]
        )
        == 0
    )
    assert "private value" not in capsys.readouterr().out
    resources = observed[0].local_resources
    assert resources is not None
    assert resources.datasets[0].mount.path == dataset
    assert [v["name"] for v in resources.manifest()[0]["versions"]] == ["v2", "v10"]
    validate_datasets(resources.manifest())
    mounts = container_mounts(
        observed[0],
        {
            target: dataset.parent / target[1:]
            for target in ("/startup", "/output", "/scratch", "/record")
        },
    )
    assert mounts[-2:] == [
        ("/source", config.source.path, "ro"),
        ("/local-datasets/0", dataset, "rw"),
    ]
    assert "private value" not in json.dumps(resources.provenance())
    with pytest.raises(SetupError, match="private"):
        load_resources(config, (), config.source.path / "pipeline.py")


@pytest.mark.parametrize(
    "assignment", ["population", "=x", "population=", "bad_slug=x", "../x=x"]
)
def test_invalid_assignments_fail_before_execution(selection, assignment):
    config, _ = selection
    with pytest.raises(SetupError):
        load_resources(config, (assignment,), None)


def test_dataset_preflight_rejects_unsafe_entries_and_private_aliases(
    selection, tmp_path
):
    config, dataset = selection
    assignment = (f"population={dataset}",)
    with pytest.raises(SetupError, match="Duplicate"):
        load_resources(config, assignment * 2, None)
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(SetupError, match="version subdirectories"):
        load_resources(config, (f"empty={empty}",), None)
    bad = dataset / "v10" / "bad"
    for kind in ("symlink", "fifo", "hardlink"):
        if kind == "symlink":
            bad.symlink_to(dataset / "v2" / "data.csv")
        elif kind == "fifo":
            os.mkfifo(bad)
        else:
            os.link(config.credential_file, bad)
        try:
            with pytest.raises(SetupError, match="symlink|regular|linked"):
                load_resources(config, assignment, None)
        finally:
            bad.unlink()
    (dataset / "v10" / "empty").mkdir()
    assert load_resources(config, assignment, None).datasets
    secret = tmp_path / "secret.env"
    secret.write_text("TEST=CUSTOM\nTEST_VALUE=private\n")
    alias = config.source.path / "alias"
    os.link(secret, alias)
    with pytest.raises(SetupError, match="alias"):
        load_resources(config, (), secret)


def test_connections_literal_grammar_aliases_and_actual_sdk(
    selection, tmp_path, monkeypatch
):
    path = tmp_path / "connections.env"
    service_account = json.dumps(
        {"private_key": "literal\\nkey", "client_email": "test@example.invalid"}
    )
    path.write_text(
        "\n".join(
            [
                "TEST-DHIS2=DHIS2",
                "TEST_DHIS2_URL=https://example.test",
                "TEST_DHIS2_USERNAME=user",
                "TEST_DHIS2_PASSWORD='$(not executed) $HOME `literal`'",
                "DB=POSTGRESQL",
                "DB_HOST=localhost",
                "DB_PORT=5432",
                "DB_USERNAME=user",
                "DB_PASSWORD=secret",
                "DB_DATABASE_NAME=local",
                "STORE=S3",
                "STORE_ACCESS_KEY_ID=id",
                "STORE_SECRET_ACCESS_KEY=key",
                "STORE_BUCKET_NAME=bucket",
                "CLOUD=GCS",
                f"CLOUD_SERVICE_ACCOUNT_KEY='{service_account}'",
                "CLOUD_BUCKET_NAME=bucket",
                "EXTRA=CUSTOM",
                "EXTRA_TOKEN='literal # token'",
                "IASO_TEST=IASO",
                "IASO_TEST_URL=https://example.test",
                "IASO_TEST_USERNAME=user",
                "IASO_TEST_PASSWORD=secret",
            ]
        )
    )
    values = load_connections(path)
    assert values["CLOUD_SERVICE_ACCOUNT_KEY"] == service_account
    assert (
        values["DB_DB_NAME"] == "local" and values["STORE_ACCESS_KEY_SECRET"] == "key"
    )
    monkeypatch.setattr(
        CurrentWorkspace,
        "get_connection_from_env",
        CurrentWorkspace.get_connection_from_env,
    )
    monkeypatch.setenv("EXTRA_AMBIENT", "must not merge")
    local_connections.install(values)
    assert (
        workspace.dhis2_connection("test-dhis2").password
        == "$(not executed) $HOME `literal`"
    )
    assert workspace.postgresql_connection("db").database_name == "local"
    assert workspace.postgresql_connection("db").port == 5432
    assert workspace.s3_connection("store").secret_access_key == "key"
    cloud = workspace.get_connection("cloud")
    assert (
        isinstance(cloud, GCSConnection)
        and cloud.service_account_key == service_account
    )
    assert not hasattr(workspace.get_connection("extra"), "ambient")
    assert workspace.iaso_connection("iaso-test").username == "user"


@pytest.mark.parametrize(
    "text",
    [
        "TEST=DHIS2\nTEST_URL=x",
        "TEST=UNKNOWN",
        "TEST=CUSTOM",
        "HEXA_TOKEN=secret",
        "PYTHONPATH=/evil",
        "LD=CUSTOM\nLD_PRELOAD=evil",
        "HTTP=CUSTOM\nHTTP_PROXY=evil",
        "TEST=CUSTOM\nTEST_VALUE=a\ntest-value=b",
        "TEST=CUSTOM\nTEST_VALUE='unterminated",
        "TEST=DHIS2\nTEST_URL=x\nTEST_USERNAME=u\nTEST_PASSWORD=p\nTEST_UNKNOWN=bad",
        "DB=POSTGRESQL\nDB_HOST=h\nDB_PORT=65536\nDB_USERNAME=u\nDB_PASSWORD=p\nDB_DB_NAME=d",
        "DB=POSTGRESQL\nDB_HOST=h\nDB_PORT=5432\nDB_USERNAME=u\nDB_PASSWORD=p\nDB_DB_NAME=d\nDB_DATABASE_NAME=d",
    ],
)
def test_bad_connections_fail_closed_without_values(tmp_path, text):
    path = tmp_path / "selected.env"
    path.write_text(text)
    with pytest.raises(SetupError) as failure:
        load_connections(path)
    assert "evil" not in str(failure.value) and "unterminated" not in str(failure.value)


@pytest.fixture
def registry(selection):
    config, dataset = selection
    resources = load_resources(config, (f"population={dataset}",), None)
    public = resources.manifest()
    hosted = copy.deepcopy(public)
    hosted[0]["root"] = str(dataset)
    with serving(hosted) as endpoint:
        yield public, endpoint, dataset


def test_sdk_objects_lookup_http_ranges_and_local_writes(registry, monkeypatch):
    public, endpoint, dataset = registry
    calls = []

    def remote(self, identifier, source_workspace_slug=None):
        calls.append((identifier, source_workspace_slug))
        return "remote"

    monkeypatch.setenv("HEXA_WORKSPACE", "test")
    monkeypatch.setattr(CurrentWorkspace, "get_dataset", remote)
    local_datasets.install(public, endpoint)
    local = workspace.get_dataset("population")
    assert isinstance(local, Dataset) and isinstance(local, local_datasets.LocalDataset)
    assert local.source_workspace_slug == "test"
    assert workspace.get_dataset("population", "test").slug == "population"
    assert workspace.get_dataset("population", "other") == "remote"
    assert workspace.get_dataset("unmatched") == "remote"
    assert calls == [("population", "other"), ("unmatched", None)]
    assert (
        Parameter("dataset", type=Dataset, default="population").validate(None).slug
        == "population"
    )
    assert (
        len(
            Parameter("datasets", type=Dataset, multiple=True).validate(
                ["population", "population"]
            )
        )
        == 2
    )
    assert [v.name for v in local.versions] == ["v2", "v10"]
    assert (
        Parameter("defaults", type=Dataset, multiple=True, default=["population"])
        .validate(None)[0]
        .slug
        == "population"
    )
    version = local.latest_version
    assert (
        isinstance(version, DatasetVersion)
        and version.name == "v10"
        and version.created_at == ""
    )
    file = version.get_file("data.csv")
    assert (
        isinstance(file, DatasetFile)
        and file.uri == "data.csv"
        and file.content_type == "text/csv"
    )
    assert file.read() == b"value\n42\n"
    with requests.get(file.download_url, stream=True, timeout=5) as response:
        assert b"".join(response.iter_content(2)) == file.read()
    assert requests.head(file.download_url, timeout=5).headers["Content-Length"] == "9"
    for byte_range, status, content in [
        ("bytes=0-4", 206, b"value"),
        ("bytes=-3", 206, b"42\n"),
        ("bytes=6-", 206, b"42\n"),
        ("bytes=99-", 416, b""),
        ("bytes=0-1,3-4", 416, b""),
    ]:
        response = requests.get(
            file.download_url, headers={"Range": byte_range}, timeout=5
        )
        assert (response.status_code, response.content) == (status, content)
        assert "Content-Range" in response.headers
    with ThreadPoolExecutor(max_workers=12) as pool:
        assert (
            list(
                pool.map(
                    lambda _: requests.get(file.download_url, timeout=10).content,
                    range(24),
                )
            )
            == [b"value\n42\n"] * 24
        )
    new = local.create_version("v11")
    assert new.name == local.latest_version.name == "v11" and list(new.files) == []
    added = new.add_file(b"local bytes", "nested/result.txt")
    assert added.read() == b"local bytes"
    assert (dataset / "v11" / "nested" / "result.txt").read_bytes() == b"local bytes"
    with pytest.raises(FileExistsError):
        new.add_file(b"replacement", "nested/result.txt")
    with pytest.raises(FileExistsError):
        local.create_version("v11")
    assert (
        local.create_version("v3").name == "v3" and local.latest_version.name == "v11"
    )
    old = next(v for v in local.versions if v.name == "v2")
    old.add_file(io.BytesIO(b"old version accepts new files"), "extra.txt")
    assert old.exists("extra.txt") and not old.exists("missing")
    with pytest.raises(FileNotFoundError):
        old.get_file("missing")
    for filename in ("../escape", "/absolute", "a/../escape", ".env"):
        with pytest.raises((ValueError, OSError)):
            new.add_file(b"forbidden", filename)
    new.add_file(b"a", "a/same.txt")
    new.add_file(b"b", "b/same.txt")
    with pytest.raises(ValueError, match="Ambiguous"):
        new.get_file("same.txt")
    assert new.get_file("b/same.txt").read() == b"b"
    assert all(item.id.startswith("local:") for item in [local, version, file])


def test_registry_does_not_serve_unregistered_or_replaced_paths(
    registry, monkeypatch, tmp_path
):
    public, endpoint, dataset = registry
    monkeypatch.setenv("HEXA_WORKSPACE", "test")
    monkeypatch.setattr(CurrentWorkspace, "get_dataset", CurrentWorkspace.get_dataset)
    local_datasets.install(public, endpoint)
    local = workspace.get_dataset("population")
    assert isinstance(local, local_datasets.LocalDataset)
    file = local.latest_version.get_file("other.txt")
    url = file.download_url
    origin = f"http://127.0.0.1:{urlsplit(endpoint).port}"
    for suffix in ("/", "/wrong/catalog/0", "/../etc/passwd", "/%2e%2e/etc/passwd"):
        assert requests.get(origin + suffix, timeout=5).status_code == 404
    assert requests.get(endpoint + "/v10/data.csv", timeout=5).status_code == 404
    target = dataset / "v10" / "nested"
    target.rename(dataset / "v10" / "saved")
    secret = tmp_path / "outside"
    secret.mkdir()
    (secret / "other.txt").write_text("must not serve")
    target.symlink_to(secret, target_is_directory=True)
    assert requests.get(url, timeout=5).status_code == 404
    version = local.latest_version
    with pytest.raises(OSError):
        version.add_file(b"escape", "nested/new.txt")
    assert not (secret / "new.txt").exists()


def test_server_teardown_and_distinct_endpoints(selection):
    config, dataset = selection
    hosted = load_resources(config, (f"population={dataset}",), None).manifest()
    hosted[0]["root"] = str(dataset)
    with serving(hosted) as first, serving(hosted) as second:
        assert first != second
        first_port = urlsplit(first).port
        assert requests.get(first + "/catalog/0", timeout=5).status_code == 200
    with pytest.raises(requests.ConnectionError):
        requests.get(f"http://127.0.0.1:{first_port}/", timeout=1)
    with (
        pytest.raises(ValueError, match="original failure"),
        serving(hosted) as failed_endpoint,
    ):
        raise ValueError("original failure")
    with pytest.raises(requests.ConnectionError):
        requests.get(failed_endpoint + "/catalog/0", timeout=1)
    with pytest.raises(SystemExit) as failure, serving(hosted):
        raise SystemExit(7)
    assert failure.value.code == 7
    with pytest.raises(RuntimeError, match="failed to start"), serving([{}]):
        pytest.fail("Invalid registry cannot start")


def test_connection_redaction_across_chunks_and_json_material():
    key = json.dumps({"private_key": "PRIVATE-LINE-ONE\nPRIVATE-LINE-TWO"})
    redactor = Redactor("token", ("split-secret", key))
    output = io.StringIO()
    redactor.stream(
        io.StringIO(
            "split-"
            + "secret\nPRIVATE-LINE-ONE\n"
            + key
            + "\nhttp://127.0.0.1:12/capability/path\n"
        ),
        output,
    )
    assert (
        "split-secret" not in output.getvalue()
        and "PRIVATE-LINE" not in output.getvalue()
    )
    assert "capability" not in output.getvalue()
    prefix = "S" * 43
    assert prefix not in redactor.text(
        f"ConnectionError: url: /{prefix}/download/id/file.csv"
    )
    assert prefix not in redactor.text(f"ConnectionError: url: /{prefix}")
    assert "PRIVATE-LINE" not in json.dumps(redactor.value({"field": key}))


def test_private_launch_transport_failure_records_and_logs(
    selection, monkeypatch, capsys
):
    config, dataset = selection
    selected = dataset.parent / "connections.env"
    selected.write_text("LOCAL=CUSTOM\nLOCAL_PASSWORD='private-password'\n")
    resources = load_resources(config, (f"population={dataset}",), selected)
    config = replace(config, local_resources=resources)
    commands = []
    payloads = []

    class Input(io.StringIO):
        def close(self):
            payloads.append(json.loads(self.getvalue()))
            super().close()

    class Process:
        def __init__(self, command, **kwargs):
            commands.append(command)
            self.stdin = Input()
            self.stdout = io.StringIO(
                "private-"
                + "password synthetic-token http://127.0.0.1:123/capability\n"
            )

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def wait(self):
            return 1

    monkeypatch.setattr("hexa.runner.subprocess.run", lambda *args, **kwargs: None)
    monkeypatch.setattr("hexa.runner.subprocess.Popen", Process)
    result = run(config, load_credentials(config.credential_file))
    assert result["exit_code"] == 1
    assert (
        payloads[0]["local_resources"]["connections"]["LOCAL_PASSWORD"]
        == "private-password"
    )
    assert payloads[0]["token"] == "synthetic-token"
    assert "private-password" not in json.dumps(
        commands
    ) and "synthetic-token" not in json.dumps(commands)
    assert not any("connections.env" in item for item in commands[0])
    text = (
        capsys.readouterr().err
        + Path(result["record_file"]).read_text()
        + Path(result["log_file"]).read_text()
    )
    assert all(
        secret not in text
        for secret in ("private-password", "synthetic-token", "capability")
    )
    record = json.loads(Path(result["record_file"]).read_text())
    assert record["local_resources"]["connections"] == [
        {"identifier": "LOCAL", "type": "CUSTOM"}
    ]
    assert record["local_resources"]["datasets"][0]["path"] == str(dataset)
    startup = Path(result["record_file"]).parent / "startup"
    assert all(
        "private-password" not in path.read_text() for path in startup.glob("*.py")
    )


def test_spawn_workers_install_private_resolvers(registry, tmp_path):
    public, endpoint, dataset = registry
    worker = tmp_path / "worker_check.py"
    worker.write_text("""from openhexa.sdk import workspace

def check(passed_dataset):
    assert passed_dataset.latest_version.name == "v10"
    assert passed_dataset.latest_version.get_file("data.csv").read() == b"value\\n42\\n"
    dataset = workspace.get_dataset("population")
    assert dataset.latest_version.name == "v10"
    assert dataset.latest_version.get_file("data.csv").read() == b"value\\n42\\n"
    connection = workspace.custom_connection("local")
    assert connection.password == "synthetic-connection-password"
    version = dataset.create_version("v12")
    version.add_file(b"spawned", "result.txt")
    return "checked"
""")
    script = tmp_path / "spawn_check.py"
    script.write_text("""from multiprocess import get_context
from worker_check import check
from openhexa.sdk import workspace
if __name__ == "__main__":
    dataset = workspace.get_dataset("population")
    with get_context("spawn").Pool(1) as pool:
        assert pool.apply(check, (dataset,)) == "checked"
""")
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("HEXA_", "OPENHEXA_"))
    }
    environment.update(
        {
            "PYTHONPATH": os.pathsep.join(
                [str(PACKAGE_DIR / "runtime"), str(tmp_path)]
            ),
            "HEXA_CONNECTED_LOCAL": "1",
            "HEXA_ENVIRONMENT": "LOCAL_PIPELINE",
            "HEXA_SERVER_URL": "https://example.invalid",
            "HEXA_WORKSPACE": "test",
            "HEXA_TOKEN": "synthetic-token",
            "HEXA_LOCAL_DATASETS": json.dumps(
                {"datasets": public, "endpoint": endpoint}
            ),
            "HEXA_LOCAL_CONNECTION_KEYS": json.dumps(["LOCAL", "LOCAL_PASSWORD"]),
            "LOCAL": "CUSTOM",
            "LOCAL_PASSWORD": "synthetic-connection-password",
        }
    )
    result = subprocess.run(
        [sys.executable, str(script)],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert result.stdout == "" and result.stderr == ""
    assert (dataset / "v12" / "result.txt").read_bytes() == b"spawned"


def test_bootstrap_connection_payload_and_no_override_compatibility(tmp_path):
    script = tmp_path / "check.py"
    script.write_text("""import os
from openhexa.sdk import workspace
assert os.environ["HEXA_TOKEN"] == "synthetic-token"
if "LOCAL" in os.environ:
    assert workspace.custom_connection("local").password == "synthetic-password"
""")
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("HEXA_", "OPENHEXA_"))
    }
    environment.update(
        PYTHONPATH=str(PACKAGE_DIR / "runtime"),
        HEXA_CONNECTED_LOCAL="1",
        HEXA_ENVIRONMENT="LOCAL_PIPELINE",
    )
    payload = {"token": "synthetic-token", "parameters": {}}
    for resources in (
        None,
        {
            "datasets": [],
            "connections": {"LOCAL": "CUSTOM", "LOCAL_PASSWORD": "synthetic-password"},
        },
    ):
        current = {**payload, **({"local_resources": resources} if resources else {})}
        subprocess.run(
            [
                sys.executable,
                str(PACKAGE_DIR / "runtime" / "bootstrap.py"),
                str(script),
            ],
            env=environment,
            input=json.dumps(current),
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    bad = {
        **payload,
        "local_resources": {
            "datasets": [],
            "connections": {"HEXA_TOKEN": "must-not-print"},
        },
    }
    with pytest.raises(subprocess.CalledProcessError) as error:
        subprocess.run(
            [
                sys.executable,
                str(PACKAGE_DIR / "runtime" / "bootstrap.py"),
                str(script),
            ],
            env=environment,
            input=json.dumps(bad),
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    assert "must-not-print" not in error.value.stderr


def test_manifest_rejects_runtime_controls_and_traversal(selection):
    config, dataset = selection
    manifest = load_resources(config, (f"population={dataset}",), None).manifest()
    manifest[0]["versions"][0]["files"] = ["../escape"]
    with pytest.raises(ValueError):
        validate_datasets(manifest)
    with pytest.raises(ValueError):
        validate_connections({"HEXA_TOKEN": "injection"})
