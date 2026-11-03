"""Inject the private stdin token before executing ordinary mounted source."""

import importlib
import json
import os
import runpy
import sys
from contextlib import nullcontext
from pathlib import Path

sitecustomize = importlib.import_module("sitecustomize")
file_server = importlib.import_module("file_server")
resource_manifest = importlib.import_module("resource_manifest")

# Exit status reported to the host when a mount cannot be read.
UNREADABLE_MOUNT = 77


def _unreadable(entrypoint: Path) -> tuple[Path, str] | None:
    """Return the first unreadable mount path and the reason, if any."""
    inputs = Path("/inputs")
    directories = [entrypoint.parent]
    if inputs.is_dir():
        directories.extend(sorted(inputs.iterdir()))
    for path in (*directories, entrypoint):
        try:
            if path.is_dir():
                os.listdir(path)
            else:
                path.open("rb").close()
        except PermissionError:
            return path, "permission denied"
        except OSError as error:
            return path, (error.strerror or type(error).__name__).lower()
    return None


def _check_datasets(datasets: list[dict]) -> None:
    for item in datasets:
        root = Path(item["root"])
        paths = [root, *(root / version["name"] for version in item["versions"])]
        for path in paths:
            try:
                os.listdir(path)
                if not os.access(path, os.W_OK | os.X_OK):
                    raise PermissionError("Dataset directory is not writable")
            except OSError:
                print(
                    f"hexa: cannot read {path} (dataset mount must be readable and writable)",
                    file=sys.stderr,
                )
                sys.exit(UNREADABLE_MOUNT)
        for version in item["versions"]:
            for relative in version["files"]:
                try:
                    file_server.open_registered(
                        str(root), version["name"] + "/" + relative
                    ).close()
                except OSError:
                    print(
                        f"hexa: cannot read {root / version['name'] / relative} (local dataset file unavailable)",
                        file=sys.stderr,
                    )
                    sys.exit(UNREADABLE_MOUNT)


def main() -> None:
    """Validate startup and hand control to the pipeline's normal main guard."""
    if not sitecustomize.adapter_ready:
        raise RuntimeError("Connected-local startup adapter did not initialize")
    entrypoint = sys.argv[1]
    unreadable = _unreadable(Path(entrypoint))
    if unreadable is not None:
        print(f"hexa: cannot read {unreadable[0]} ({unreadable[1]})", file=sys.stderr)
        sys.exit(UNREADABLE_MOUNT)
    payload = json.loads(sys.stdin.readline())
    if (
        not isinstance(payload, dict)
        or set(payload)
        not in ({"token", "parameters"}, {"token", "parameters", "local_resources"})
        or not isinstance(payload["token"], str)
        or not payload["token"]
        or not isinstance(payload["parameters"], dict)
    ):
        raise RuntimeError("Invalid private bootstrap payload")
    resources = payload.get("local_resources", {"datasets": [], "connections": {}})
    if not isinstance(resources, dict) or set(resources) != {"datasets", "connections"}:
        raise RuntimeError("Invalid local resource payload")
    datasets = resource_manifest.validate_datasets(resources["datasets"])
    connections = resource_manifest.validate_connections(resources["connections"])
    if datasets or connections:
        from importlib.metadata import version

        if version("openhexa.sdk") != sitecustomize.WRITTEN_FOR_SDK:
            raise RuntimeError("Local resources require the pinned SDK version")
    _check_datasets(datasets)
    os.environ["HEXA_TOKEN"] = payload.pop("token")
    os.environ.update(connections)
    os.environ["HEXA_LOCAL_CONNECTION_KEYS"] = json.dumps(list(connections))
    if connections:
        importlib.import_module("local_connections").install(connections)
    sys.argv = [entrypoint, "-c", json.dumps(payload["parameters"])]
    del payload, resources, connections
    with file_server.serving(datasets) if datasets else nullcontext(None) as endpoint:
        if endpoint is not None:
            adapter = importlib.import_module("local_datasets")
            os.environ["HEXA_LOCAL_DATASETS"] = json.dumps(
                {"datasets": datasets, "endpoint": endpoint}
            )
            adapter.install(datasets, endpoint)
        try:
            runpy.run_path(entrypoint, run_name="__main__")
        finally:
            os.environ.pop("HEXA_LOCAL_DATASETS", None)


if __name__ == "__main__":
    main()
