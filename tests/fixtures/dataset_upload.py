"""Small offline GraphQL/storage double, also usable from an isolated wheel install."""

import copy
import io
import json
from typing import Any
from urllib.error import URLError

from hexa.api import API

DATASET_ID = "11111111-1111-4111-8111-111111111111"
VERSION_ID = "22222222-2222-4222-8222-222222222222"
SIGNED_URL = "https://storage.invalid/upload?signed=private"
READ_URL = "https://storage.invalid/read/"
TOKEN = "synthetic-token"


class DatasetRemote:
    """Serve just the dataset contracts exercised by creation and upload."""

    def __init__(self):
        self.dataset = {
            "id": DATASET_ID,
            "slug": "sales",
            "name": "Sales [bold] " + TOKEN,
            "description": "Sales",
            "workspace": {"slug": "source"},
            "permissions": {"createVersion": True},
            "latestVersion": {"id": "old-version", "name": "old"},
        }
        self.initial_version = None
        self.registered = []
        self.uploads = []
        self.calls = []
        self.failure = None
        self.fail_at_file = 0
        self.rejection = False
        self.incomplete = False
        self.extra_dataset = None
        self.missing_version = False
        self.create_allowed = True
        self.storage_headers: dict[str, str] | None = {
            "Content-Type": "application/custom",
            "x-provider": "yes",
        }
        self.content_type = ""
        self.uris: list[str] = []

    def graphql(self, query, variables):
        self.calls.append((query, copy.deepcopy(variables)))
        if query.startswith("query WorkspaceDatasets"):
            assert variables["ws"] == "selected"
            datasets = [self.dataset]
            if self.extra_dataset is not None:
                datasets.append(self.extra_dataset)
            return {
                "workspace": {
                    "datasets": self.page(
                        [
                            {"id": "link-" + item["id"], "dataset": copy.deepcopy(item)}
                            for item in datasets
                        ]
                    )
                }
            }
        if query.startswith("query Workspace"):
            return {
                "workspace": {
                    "slug": "selected",
                    "permissions": {"createDataset": self.create_allowed},
                }
            }
        if query.startswith("query Dataset"):
            assert variables["id"] == DATASET_ID
            return {"dataset": copy.deepcopy(self.dataset)}
        if query.startswith("query Version"):
            assert variables["id"] == VERSION_ID
            if self.failure == "verify":
                raise URLError("private verification diagnostic " + SIGNED_URL)
            return {
                "datasetVersion": {"files": self.page(copy.deepcopy(self.registered))}
            }
        field = query.split("{", 1)[1].split("(", 1)[0]
        values = variables["input"]
        if field == "prepareVersionFileDownload":
            return {
                field: {
                    "success": True,
                    "errors": [],
                    "downloadUrl": READ_URL + values["fileId"],
                }
            }
        if self.failure == field and len(self.registered) == self.fail_at_file:
            if self.rejection:
                return {field: {"success": False, "errors": ["PERMISSION_DENIED"]}}
            raise URLError("private mutation diagnostic " + SIGNED_URL)
        result: dict[str, Any] = {"success": True, "errors": []}
        if field == "createDataset":
            assert values["workspaceSlug"] == "selected"
            result["dataset"] = {
                **self.dataset,
                "workspace": {"slug": "selected"},
                "latestVersion": self.initial_version,
            }
        elif field == "createDatasetVersion":
            assert values["datasetId"] == DATASET_ID
            result["version"] = (
                None
                if self.missing_version
                else {
                    "id": VERSION_ID,
                    "name": values["name"],
                    "dataset": {"id": DATASET_ID},
                }
            )
        else:
            assert values["versionId"] == VERSION_ID
            if field == "generateDatasetUploadUrl":
                self.uris.append(values["uri"])
                self.content_type = values["contentType"]
                result.update(uploadUrl=SIGNED_URL, headers=self.storage_headers)
            else:
                assert field == "createDatasetVersionFile"
                item = {
                    "id": "file-" + values["uri"],
                    "filename": values["uri"],
                    "uri": f"{DATASET_ID}/{VERSION_ID}/{values['uri']}",
                    "contentType": values["contentType"],
                    "size": len(self.uploads[-1]),
                }
                self.registered.append(item)
                result["file"] = copy.deepcopy(item)
        return {field: result}

    def page(self, items):
        return {
            "items": items,
            "pageNumber": 1,
            "totalPages": 2 if self.incomplete else 1,
            "totalItems": len(items),
        }

    def open(self, request, timeout):
        assert timeout == 60
        if request.full_url.endswith("/graphql/"):
            assert request.get_header("Authorization") == "Bearer " + TOKEN
            values = json.loads(request.data)
            return io.BytesIO(
                json.dumps(
                    {"data": self.graphql(values["query"], values["variables"])}
                ).encode()
            )
        if request.full_url.startswith(READ_URL):
            assert request.get_method() == "GET"
            file_id = request.full_url.removeprefix(READ_URL)
            index = [item["id"] for item in self.registered].index(file_id)
            return io.BytesIO(self.uploads[index])
        assert request.full_url == SIGNED_URL
        assert request.get_method() == "PUT"
        assert request.get_header("Authorization") is None
        assert request.get_header("Cookie") is None
        headers = (
            {"Content-Type": self.content_type}
            if self.storage_headers is None
            else self.storage_headers
        )
        for key, value in headers.items():
            assert request.get_header(key.capitalize()) == value
        assert not isinstance(request.data, bytes)
        raw = bytearray()
        while chunk := request.data.read(65536):
            raw.extend(chunk)
        assert request.get_header("Content-length") == str(len(raw))
        self.uploads.append(bytes(raw))
        if self.failure == "storage_put":
            raise URLError(SIGNED_URL)
        return io.BytesIO(b"")

    def install(self):
        """Inject the offline opener without changing production request handling."""
        original = API.__init__
        remote = self

        def initialize(client, *args, **kwargs):
            original(client, *args, **kwargs)
            client.opener.open = remote.open

        API.__init__ = initialize
