"""Integration fixture: read one dataset file and resolve one connection, locally.

Run by `tests/test_container.py` with resources named in the integration config.
It never publishes output and never calls the connection's external service.
"""

import hashlib
import json
from pathlib import Path

from openhexa.sdk import Dataset, current_run, parameter, pipeline, workspace

assert current_run is not None

PROCESSING_VALUE = 42


@pipeline(name="Connected-local dataset summary")
@parameter("dataset", type=Dataset)
@parameter("version", type=str)
@parameter("filename", type=str)
@parameter("connection", type=str)
@parameter("fail_processing", type=bool, default=False)
def summarize(
    dataset: Dataset,
    version: str,
    filename: str,
    connection: str,
    fail_processing: bool,
) -> None:
    checked = check_connection(connection)
    summary = process_input(checked, dataset, version, filename, fail_processing)
    verify_output(summary)


@summarize.task
def check_connection(slug: str) -> str:
    resolved = workspace.get_connection(slug)
    current_run.log_info("Connection resolved; values withheld; no external call")
    return type(resolved).__name__


@summarize.task
def process_input(
    connection_type: str,
    dataset: Dataset,
    version: str,
    filename: str,
    fail_processing: bool,
) -> str:
    if fail_processing:
        raise ValueError("Deliberate processing failure requested")
    selected = next(
        (item for item in dataset.versions if version in (item.id, item.name)), None
    )
    if selected is None:
        raise ValueError("Configured version is missing or inaccessible")
    file = selected.get_file(filename)
    content = file.read()
    report = {
        "dataset_id": dataset.id,
        "version_id": selected.id,
        "file_id": file.id,
        "bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "connection_type": connection_type,
        "processing_value": PROCESSING_VALUE,
    }
    path = Path(workspace.files_path) / "summary.json"
    path.write_text(json.dumps(report, indent=2) + "\n")
    current_run.log_info("Dataset bytes summarized in local output")
    current_run.add_file_output(str(path))
    return str(path)


@summarize.task
def verify_output(path: str) -> None:
    report = json.loads(Path(path).read_text())
    if report["processing_value"] != PROCESSING_VALUE:
        raise ValueError("Dependent task found an invalid local summary")
    current_run.log_info("Dependent task verified local summary")


if __name__ == "__main__":
    summarize()
