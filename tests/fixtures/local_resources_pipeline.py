"""Synthetic local-resource compatibility check; rejects non-loopback HTTP calls."""

import importlib
import json
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import requests
from openhexa.sdk import parameter, pipeline, workspace
from openhexa.sdk.datasets import Dataset
from openhexa.sdk.workspaces.connection import DHIS2Connection

original_request = requests.Session.request
original_httpx_request = httpx.Client.request


def request(self, method, url, *args, **kwargs):
    """Prevent the fixture from making any external HTTP request."""
    assert urlsplit(str(url)).hostname == "127.0.0.1", (
        "External request refused by fixture"
    )
    return original_request(self, method, url, *args, **kwargs)


def httpx_request(self, method, url, *args, **kwargs):
    """Prevent generated SDK clients from contacting external services."""
    assert urlsplit(str(url)).hostname == "127.0.0.1", (
        "External request refused by fixture"
    )
    return original_httpx_request(self, method, url, *args, **kwargs)


requests.Session.request = request
httpx.Client.request = httpx_request


@pipeline("Local resource compatibility")
@parameter("dataset", type=Dataset, default="population")
@parameter("datasets", type=Dataset, multiple=True, default=["population"])
@parameter("connection", type=DHIS2Connection, default="local-dhis")
@parameter("fail", type=bool, default=False)
def local_resources(dataset, datasets, connection, fail):
    """Exercise typed defaults before handing objects to spawn workers."""
    assert dataset.latest_version.name == "v10"
    assert (
        datasets[0].slug == "population" and connection.password == "fixture-password"
    )
    check(dataset, fail)


@local_resources.task
def check(dataset, fail):
    """Check reads, HTTP consumers and persistent local writes in a spawned task."""
    direct = workspace.get_dataset("population")
    assert [version.name for version in direct.versions] == ["v2", "v10"]
    assert workspace.dhis2_connection("local-dhis").password == "fixture-password"
    version = dataset.latest_version
    file = version.get_file("data.csv")
    assert file.read() == b"value\n42\n"
    assert requests.get(file.download_url, timeout=5).content == file.read()
    with requests.get(file.download_url, stream=True, timeout=5) as response:
        assert b"".join(response.iter_content(2)) == file.read()
    ranged = requests.get(file.download_url, headers={"Range": "bytes=6-"}, timeout=5)
    assert ranged.status_code == 206 and ranged.content == b"42\n"
    pandas = importlib.import_module("pandas")
    assert pandas.read_csv(file.download_url)["value"].tolist() == [42]
    created = direct.create_version("v11")
    assert (
        created.add_file(b"local output", "nested/result.txt").read() == b"local output"
    )
    try:
        created.add_file(b"overwrite", "nested/result.txt")
    except FileExistsError:
        overwrite_refused = True
    else:
        overwrite_refused = False
    assert overwrite_refused
    try:
        Path(__file__).write_text("source must stay read-only")
    except PermissionError:
        source_readonly = True
    else:
        source_readonly = False
    assert source_readonly
    print("fixture-password")
    if fail:
        raise RuntimeError("Synthetic local resource failure")
    latest = direct.latest_version
    assert latest is not None
    Path(workspace.files_path, "resources.json").write_text(
        json.dumps(
            {
                "source_readonly": source_readonly,
                "latest": latest.name,
                "download_url": file.download_url,
            }
        )
    )


if __name__ == "__main__":
    local_resources()
