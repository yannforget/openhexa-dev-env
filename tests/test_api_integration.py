"""Opt-in read-only API checks against resources named in the integration config."""

from pathlib import Path
from typing import Any

import pytest

from hexa.api import API
from hexa.config import load_credentials

pytestmark = [pytest.mark.integration, pytest.mark.api]


@pytest.fixture(scope="module")
def api(integration: dict[str, Any]) -> API:
    return API(load_credentials(integration["credential_file"]))


def test_connection_metadata_withholds_values(api: API, integration: dict[str, Any]):
    assert api.connections()["complete"]
    connection = api.connection(integration["connection"])
    assert all("value" not in field for field in connection["fields"])


def test_dataset_version_file_and_metadata(api: API, integration: dict[str, Any]):
    dataset = api.dataset(integration["dataset"])
    assert api.dataset(dataset["id"])["id"] == dataset["id"]
    files = api.files(dataset["id"], integration["version"])
    assert integration["version"] in (files["version"]["id"], files["version"]["name"])
    file = next(item for item in files["items"] if item["path"] == integration["file"])
    assert api.metadata(dataset["id"])["targetId"]
    assert api.metadata(dataset["id"], files["version"]["id"])["targetId"]
    assert api.metadata(dataset["id"], files["version"]["id"], file["id"])["targetId"]


def test_pipeline_versions_runs_and_logs(api: API, integration: dict[str, Any]):
    pipeline = api.pipeline(integration["pipeline"])
    assert api.pipeline_show(pipeline["id"])["currentVersion"]
    versions = api.pipeline_versions(pipeline["id"])
    assert versions["complete"] and versions["items"]
    runs = api.runs(pipeline["code"])
    assert runs["items"], "The configured pipeline needs at least one run"
    run = api.run_show(runs["items"][0]["id"])
    assert run["pipeline"]["id"] == pipeline["id"]
    assert run["pipeline"]["workspace"]["slug"] == api.credentials.workspace
    assert "config" not in run and "logs" not in run and "messages" not in run
    logs = api.run_logs(run["id"], limit=3)
    assert len(logs["items"]) <= 3 and logs["server_side_bounds"] is False
    assert len(api.run_logs(run["id"], "runtime", 3)["items"]) <= 3


def test_pipeline_source_is_extracted_not_run(
    api: API, integration: dict[str, Any], tmp_path: Path
):
    pipeline = api.pipeline(integration["pipeline"])
    version = pipeline["currentVersion"]["id"]
    result = api.source(pipeline["id"], version, tmp_path / "source")
    assert result["executed"] is False and result["version"]["id"] == version
    assert (Path(result["destination"]) / "pipeline.py").is_file()


def test_workspace_context_and_tags(api: API, integration: dict[str, Any]):
    context = api.context()
    assert context["workspace"] == api.credentials.workspace
    assert [d["source"] for d in context["documents"]] == [
        "workspace.description",
        "bucket:AGENTS.md",
    ]
    assert api.tags("workspace")["workspace"] == api.credentials.workspace
    pipeline = api.pipeline(integration["pipeline"])
    assert api.tags("pipelines", pipeline["id"])["id"] == pipeline["id"]
