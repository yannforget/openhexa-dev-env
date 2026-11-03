"""SDK 2.23.0-compatible datasets backed by an explicitly selected local directory."""

import importlib
import json
import mimetypes
import os
import tempfile
from pathlib import Path, PurePosixPath
from typing import IO, Any, cast
from urllib.parse import quote, urlsplit

import requests
from openhexa.sdk.datasets.dataset import Dataset, DatasetFile, DatasetVersion
from openhexa.sdk.utils import Iterator, Page, read_content
from openhexa.sdk.workspaces.current_workspace import CurrentWorkspace

manifest = importlib.import_module(
    f"{__package__}.resource_manifest" if __package__ else "resource_manifest"
)


class LocalIterator(Iterator):
    """Preserve the SDK's single-use iterator and page accounting."""

    def __init__(self, items: list):
        super().__init__(per_page=50)
        self.items = items

    def _next_page(self):
        start = self.page_number * 50
        if start >= len(self.items):
            return None
        return Page(
            parent=self,
            items=self.items[start : start + 50],
            item_to_value=lambda item: item,
        )


def _response(response):
    if response.status_code == 409:
        raise FileExistsError(
            "Local version or file already exists; overwrites are not supported"
        )
    if response.status_code != 200:
        raise OSError(
            "Local dataset operation failed; check the selected directory and permissions"
        )
    return response.json()


class LocalFile(DatasetFile):
    """A file with a run-local HTTP capability URL, not a remote identity."""

    def __init__(self, version, file: dict):
        self.version = version
        self.id = "local:file:" + file["id"]
        self.uri = file["uri"]
        self.filename = PurePosixPath(self.uri).name
        self.content_type = (
            mimetypes.guess_type(self.uri)[0] or "application/octet-stream"
        )
        self.created_at = ""
        endpoint = urlsplit(version.dataset.endpoint)
        self._download_url = f"{endpoint.scheme}://{endpoint.netloc}{file['route']}"


class LocalVersion(DatasetVersion):
    """Local versions accept new files, but never overwrite existing paths."""

    def __init__(self, dataset: "LocalDataset", version: dict):
        self.dataset = dataset
        self.local_dataset = dataset
        self.local_id = version["id"]
        self.id = "local:version:" + self.local_id
        self.name = version["name"]
        self.created_at = ""

    @property
    def files(self) -> Any:
        """Iterate the version's registered files, including completed local writes."""
        version = next(
            v for v in self.local_dataset.catalog() if v["id"] == self.local_id
        )
        return LocalIterator([LocalFile(self, file) for file in version["files"]])

    def get_file(self, filename: str) -> DatasetFile:
        """Prefer exact relative paths; reject ambiguous basename-only lookup."""
        files = list(self.files)
        exact = [file for file in files if file.uri == filename]
        matches = exact or [file for file in files if file.filename == filename]
        if not matches:
            raise FileNotFoundError("File not found in local dataset version")
        if len(matches) != 1:
            raise ValueError("Ambiguous local filename; use the relative uri")
        return matches[0]

    def exists(self, objectKey: str):
        """Check local membership with the same ambiguity rules as get_file."""
        try:
            self.get_file(objectKey)
        except FileNotFoundError:
            return False
        return True

    def add_file(self, source, filename: str | None = None) -> DatasetFile:
        """Copy into the selected dataset directory, preserving relative filenames."""
        if filename is None and isinstance(source, (str, os.PathLike)):
            filename = Path(source).name
        if not manifest.valid_relative(filename):
            raise ValueError(
                "A safe relative filename is required for local dataset files"
            )
        # Spooling bounds memory and provides an exact HTTP content length for all SDK input forms.
        with tempfile.TemporaryFile() as staged, read_content(source) as content:
            if isinstance(content, bytes):
                staged.write(content)
            else:
                reader = cast(IO, content)
                while chunk := reader.read(65536):
                    staged.write(chunk.encode() if isinstance(chunk, str) else chunk)
            length = staged.tell()
            staged.seek(0)
            response = requests.put(
                f"{self.local_dataset.endpoint}/files/{self.local_dataset.index}/{self.local_id}",
                data=staged if length else b"",
                headers={
                    "X-Hexa-URI": quote(str(filename), safe=""),
                    "Content-Length": str(length),
                },
                timeout=60,
            )
        return LocalFile(self, _response(response))

    def _generate_upload_url(self, filename: str, content_type: str):
        """Local files have no remote upload operation."""
        raise NotImplementedError(
            "Remote upload URLs are unavailable for local dataset versions"
        )

    def _azure_put(self, *args, **kwargs):
        """Prevent inherited remote-only upload paths from using local IDs."""
        raise NotImplementedError(
            "Remote uploads are unavailable for local dataset versions"
        )

    _azure_upload_by_blocks = _azure_put


class LocalDataset(Dataset):
    """Synthetic metadata is labelled local and carries no remote timestamps."""

    def __init__(self, item: dict, workspace: str, endpoint: str, index: int):
        super().__init__(
            id=f"local:dataset:{item['slug']}",
            slug=item["slug"],
            name=item["slug"],
            description="Local writable dataset override (live mount)",
            source_workspace_slug=workspace,
        )
        self.endpoint = endpoint
        self.index = index

    def catalog(self) -> list[dict]:
        """Read the run-local registry, shared by all SDK task processes."""
        return _response(
            requests.get(f"{self.endpoint}/catalog/{self.index}", timeout=60)
        )

    @property
    def latest_version(self) -> DatasetVersion:
        """Select the naturally greatest directory name, not the last write time."""
        return LocalVersion(self, self.catalog()[-1])

    @property
    def versions(self) -> Any:
        """Iterate local versions in ascending natural name order."""
        return LocalIterator(
            [LocalVersion(self, version) for version in self.catalog()]
        )

    def create_version(self, name) -> DatasetVersion:
        """Create an empty local version; natural ordering still determines latest."""
        name = str(name)
        if not manifest.valid_version(name):
            raise ValueError(
                "A local version name must be one safe directory component"
            )
        version = _response(
            requests.post(
                f"{self.endpoint}/versions/{self.index}",
                json={"name": name},
                timeout=60,
            )
        )
        return LocalVersion(self, version)


def install(datasets: list[dict], endpoint: str) -> None:
    """Patch only workspace dataset lookup, before conversion and in spawn workers."""
    datasets = manifest.validate_datasets(datasets)
    original = CurrentWorkspace.get_dataset
    registry = {item["slug"]: (index, item) for index, item in enumerate(datasets)}

    def get_dataset(self, identifier: str, source_workspace_slug: str | None = None):
        if identifier in registry and source_workspace_slug in (None, self.slug):
            index, item = registry[identifier]
            return LocalDataset(item, self.slug, endpoint, index)
        if source_workspace_slug is None:
            return original(self, identifier)
        return original(self, identifier, source_workspace_slug)

    CurrentWorkspace.get_dataset = get_dataset


def restore() -> None:
    """Install the parent's private process-environment registry in fresh SDK workers."""
    payload = os.environ.get("HEXA_LOCAL_DATASETS")
    if payload is not None:
        data = json.loads(payload)
        install(data["datasets"], data["endpoint"])
