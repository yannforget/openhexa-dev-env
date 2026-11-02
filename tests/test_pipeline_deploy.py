"""Pipeline deployment against a fake server: manifest, dry run, one dispatch."""

import base64
import io
import json
import zipfile

import pytest

from hexa.api import API, APIError
from hexa.cli import main
from hexa.config import Credentials, SetupError

CREDENTIALS = Credentials("https://example.invalid", "selected", "synthetic-token")
PIPELINE = {
    "id": "pipeline-id",
    "code": "existing",
    "name": "Existing",
    "currentVersion": {"id": "v1-id", "name": None, "versionName": "v1"},
    "permissions": {"update": True},
}
SOURCE = (
    "from openhexa.sdk import parameter, pipeline\n"
    '@pipeline(name="Example", timeout=600)\n'
    '@parameter("year", type=int, default=2025)\n'
    "def example(year): pass\n"
)


def _field(name):
    return {
        "name": name,
        "description": None,
        "defaultValue": None,
        "type": {"kind": "SCALAR", "name": "String", "ofType": None},
    }


class Server:
    """Answers the reads deploy makes and records every mutation."""

    def __init__(self, version_fields=("zipfile", "files")):
        self.version_fields = version_fields
        self.mutations: list[tuple[str, dict]] = []
        self.result: dict | None = None
        self.failure: APIError | None = None

    def graphql(self, query, variables):
        if query.startswith("mutation"):
            self.mutations.append((query, variables["input"]))
            if self.failure is not None:
                raise self.failure
            return {query.split("{", 1)[1].split("(", 1)[0]: self.result}
        if "__type" in query:
            fields = {"CreatePipelineInput": ("name", "version")}.get(
                variables["name"], ("name", *self.version_fields)
            )
            return {
                "__type": {
                    "kind": "INPUT_OBJECT",
                    "name": variables["name"],
                    "description": None,
                    "fields": None,
                    "inputFields": [_field(name) for name in fields],
                    "enumValues": None,
                    "possibleTypes": None,
                }
            }
        if "query Pipelines" in query:
            items = [PIPELINE]
        elif "versions(" in query:
            items = [{"id": "v1-id", "name": "v1", "versionName": "v1 [v1]"}]
        elif "query Workspace" in query:
            return {
                "workspace": {
                    "slug": "selected",
                    "permissions": {"createPipeline": True},
                }
            }
        else:
            raise AssertionError(query)
        page = {"pageNumber": 1, "totalPages": 1, "totalItems": len(items)}
        if "versions(" in query:
            return {"pipeline": {"versions": {**page, "items": items}}}
        return {"pipelines": {**page, "items": items}}


@pytest.fixture
def server(monkeypatch):
    fake = Server()
    monkeypatch.setattr(API, "graphql", lambda self, q, v: fake.graphql(q, v))
    return fake


@pytest.fixture
def source(tmp_path):
    directory = tmp_path / "pipeline"
    (directory / "assets").mkdir(parents=True)
    (directory / "__pycache__").mkdir()
    (directory / "pipeline.py").write_text(SOURCE)
    (directory / "assets" / "mapping.json").write_text('{"a": 1}')
    (directory / "assets" / "logo.png").write_bytes(b"\x89PNG\xff\x00")
    (directory / "data").mkdir()
    (directory / "data" / "big.csv").write_text("x\n")
    (directory / "scratch.ipynb").write_text("{}")
    (directory / "__pycache__" / "pipeline.pyc").write_bytes(b"\x00")
    (directory / ".hexaignore").write_text("# local only\ndata/\n*.ipynb\n")
    return directory


def test_dry_run_lists_the_manifest_and_writes_nothing(server, source):
    result = API(CREDENTIALS).deploy_pipeline(
        source, "v2", pipeline="existing", dry_run=True
    )
    assert [f["path"] for f in result["files"]] == [
        "assets/logo.png",
        "assets/mapping.json",
        "pipeline.py",
    ]
    assert result["transport"] == "files" and result["target"]["versions"] == ["v1"]
    assert result["pipeline"]["timeout"] == 600
    assert result["pipeline"]["parameters"][0]["code"] == "year"
    assert len(result["sha256"]) == 64
    assert server.mutations == []
    with pytest.raises(APIError) as duplicate:
        API(CREDENTIALS).deploy_pipeline(
            source, "v1", pipeline="existing", dry_run=True
        )
    assert duplicate.value.code == "DUPLICATE_VERSION"
    named = API(CREDENTIALS).deploy_pipeline(
        source, "v1", new_name="Existing", dry_run=True
    )
    assert "already exists" in named["warnings"][0]


@pytest.mark.parametrize("version_fields", [("zipfile", "files"), ("zipfile",)])
def test_deploy_dispatches_once_with_files_or_zipfile(server, source, version_fields):
    server.version_fields = version_fields
    server.result = {
        "success": True,
        "errors": [],
        "pipelineVersion": {
            "id": "v2-id",
            "name": "v2",
            "versionNumber": 2,
            "pipeline": {"id": "pipeline-id", "code": "existing", "name": "Existing"},
        },
    }
    result = API(CREDENTIALS).deploy_pipeline(source, "v2", pipeline="existing")
    assert result["version"] == {"id": "v2-id", "name": "v2", "versionNumber": 2}
    assert result["pipeline"]["id"] == "pipeline-id"
    [(query, values)] = server.mutations
    assert "uploadPipeline(" in query and "parameters" not in values
    assert values["pipelineCode"] == "existing" and values["timeout"] == 600
    if "files" in version_fields:
        sent = {f["path"]: f for f in values["files"]}
        assert sent["pipeline.py"] == {
            "path": "pipeline.py",
            "content": SOURCE,
            "encoding": "TEXT",
        }
        assert (
            base64.b64decode(sent["assets/logo.png"]["content"]) == b"\x89PNG\xff\x00"
        )
    else:
        archive = zipfile.ZipFile(io.BytesIO(base64.b64decode(values["zipfile"])))
        assert archive.read("pipeline.py").decode() == SOURCE
        assert sorted(archive.namelist()) == [
            "assets/logo.png",
            "assets/mapping.json",
            "pipeline.py",
        ]


def test_new_pipeline_is_created_with_its_first_version_in_one_request(server, source):
    server.result = {
        "success": True,
        "errors": [],
        "pipeline": {"id": "new-id", "code": "new", "name": "New"},
        "pipelineVersion": {"id": "v1-id", "name": "v1", "versionNumber": 1},
    }
    result = API(CREDENTIALS).deploy_pipeline(
        source, "v1", new_name="New", description="About"
    )
    assert result["pipeline"]["code"] == "new" and result["version"]["id"] == "v1-id"
    [(query, values)] = server.mutations
    assert "createPipeline(" in query
    assert values["description"] == "About" and values["version"]["name"] == "v1"
    assert values["version"]["timeout"] == 600 and "files" in values["version"]


@pytest.mark.parametrize("outcome", ["uncertain", "rejected"])
def test_failed_deploy_is_not_retried_and_names_the_inspection(server, source, outcome):
    if outcome == "uncertain":
        server.failure = APIError("network", "API_FAILURE")
    else:
        server.result = {
            "success": False,
            "errors": ["PIPELINE_CODE_PARSING_ERROR"],
            "details": "bad decorator",
        }
    with pytest.raises(APIError) as caught:
        API(CREDENTIALS).deploy_pipeline(source, "v2", pipeline="existing")
    error = caught.value
    assert len(server.mutations) == 1
    assert "hexa pipelines versions existing" in str(error)
    if outcome == "uncertain":
        assert error.code == "UNCERTAIN_WRITE"
    else:
        assert error.code == "WRITE_REJECTED"
        assert "PIPELINE_CODE_PARSING_ERROR" in str(error)
        assert error.details is not None and error.details["details"] == "bad decorator"


def test_oversized_or_secret_bearing_packages_are_refused_before_writing(
    server, source
):
    (source / "assets" / "large.bin").write_bytes(b"\xff" * 5 * 1024 * 1024)
    with pytest.raises(SetupError, match="5 MiB"):
        API(CREDENTIALS).deploy_pipeline(source, "v2", pipeline="existing")
    (source / "assets" / "large.bin").unlink()
    (source / "assets" / "settings.json").write_text('{"token": "synthetic-token"}')
    with pytest.raises(SetupError, match="token"):
        API(CREDENTIALS).deploy_pipeline(source, "v2", pipeline="existing")
    assert server.mutations == []


def test_cli_requires_an_explicit_mode_and_renders_the_dry_run(
    server, source, tmp_path, capsys
):
    (tmp_path / ".env").write_text(
        "OPENHEXA_URL=https://example.invalid\nOPENHEXA_WORKSPACE=selected\n"
        "OPENHEXA_TOKEN=synthetic-token\n"
    )
    (tmp_path / "hexa.json").write_text(json.dumps({"credential_file": ".env"}))
    base = ["pipelines", "deploy", str(source), "--config", str(tmp_path / "hexa.json")]
    assert main([*base, "--pipeline", "existing", "--version-name", "v2"]) == 2
    assert "Choose --dry-run or --yes" in capsys.readouterr().err
    assert main([*base, "--version-name", "v2", "--dry-run"]) == 2
    capsys.readouterr()
    assert (
        main([*base, "--pipeline", "existing", "--version-name", "v2", "--dry-run"])
        == 0
    )
    output = capsys.readouterr().out
    assert "Target: pipeline existing (pipeline-id), current version v1" in output
    assert "  assets/mapping.json  (8 B)" in output
    assert "Parameters (parsed by the server on upload): year: int" in output
    assert output.rstrip().endswith("Rerun with --yes to deploy.")
    assert server.mutations == []
