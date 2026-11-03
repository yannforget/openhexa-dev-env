"""Opt-in container tests: offline runner checks, plus one read-only connected run.

Requires the pinned runtime image already in the local Podman cache; nothing is pulled.
"""

import json
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
import requests

from hexa.config import load_credentials

ROOT = Path(__file__).resolve().parents[1]
pytestmark = [pytest.mark.integration, pytest.mark.container]
FIXTURES = Path(__file__).parent / "fixtures"


def prepare(
    engine: dict[str, str], integration: dict[str, Any] | None = None
) -> tuple[Path, Path, dict]:
    """Keep every relabelled fixture in an isolated tool-owned directory."""
    directory = ROOT / ".hexa" / "tests" / "containers" / uuid.uuid4().hex
    source = directory / "source"
    source.mkdir(parents=True)
    data: dict[str, Any] = {"source": {"path": "source"}, **engine}
    if integration is not None:
        data["credential_file"] = str(integration["credential_file"])
        data["parameters"] = {
            "dataset": integration["dataset"],
            "version": integration["version"],
            "filename": integration["file"],
            "connection": integration["connection"],
            "fail_processing": False,
        }
        source.joinpath("pipeline.py").write_text(
            '"""Instrumented copy of the connected fixture."""\nimport guard\n'
            + (FIXTURES / "connected_pipeline.py").read_text()
        )
        shutil.copy(FIXTURES / "guard.py", source)
    else:
        (directory / ".env").write_text(
            "OPENHEXA_URL=https://example.invalid\nOPENHEXA_WORKSPACE=test\nOPENHEXA_TOKEN=synthetic-token\n"
        )
        data["credential_file"] = ".env"
        data["parameters"] = {"fail": False, "pause": 5}
        local_input = directory / "input"
        local_input.mkdir()
        (local_input / "input.txt").write_text("local input\n")
        data["inputs"] = [{"path": "input", "name": "data"}]
        shutil.copy(FIXTURES / "offline_pipeline.py", source / "pipeline.py")
    config = directory / "config.json"
    config.write_text(json.dumps(data))
    return config, source, data


def invoke(config: Path, executable: Path, expected: int = 0) -> tuple[dict, str]:
    """Retain only already-filtered CLI output in test memory."""
    try:
        result = subprocess.run(
            [
                str(executable),
                "run",
                "--config",
                str(config),
                "--json",
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.CalledProcessError as error:
        assert error.returncode == expected, error.stderr
        return json.loads(error.stdout), error.stderr
    assert expected == 0
    return json.loads(result.stdout), result.stderr


def test_offline_spawn_reporting_mounts_metadata_and_failure(
    installed_hexa: Path, podman_mode: dict[str, str], podman_command: tuple[str, ...]
):
    config, _, data = prepare(podman_mode)
    project = dict(data)
    (config.parent / "hexa.json").write_text(json.dumps(project))
    launcher = config.parent / "hexa"
    launcher.symlink_to(installed_hexa)
    command = [str(launcher), "run", "--json"]
    with subprocess.Popen(
        command,
        cwd=config.parent,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        assert process.stdout is not None and process.stderr is not None
        for _ in range(2):
            first = process.stderr.readline()
            assert first.startswith("SELinux label :z (shared) on "), first
        first = process.stderr.readline()
        assert first.startswith("Local run "), first
        run_id = first.split()[2].rstrip(";")
        inspection = None
        for _ in range(50):
            try:
                metadata = subprocess.run(
                    [*podman_command, "inspect", "hexa-" + run_id],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            except subprocess.CalledProcessError:
                time.sleep(0.1)
            else:
                inspection = json.loads(metadata.stdout)[0]
                break
        assert inspection is not None
        if "synthetic-token" in json.dumps(inspection):
            pytest.fail("Token leaked into Podman metadata")
        assert not any(
            value.startswith("HEXA_TOKEN=") for value in inspection["Config"]["Env"]
        )
        assert all(Path(item["Source"]).name != ".env" for item in inspection["Mounts"])
        stdout, stderr = process.communicate(timeout=120)
        assert process.returncode == 0, stderr
    result = json.loads(stdout)
    assert "Remote writes (via SDK; other HTTP clients are not tracked): none" in stderr
    assert "Sending output" in Path(result["log_file"]).read_text()
    record = json.loads(Path(result["record_file"]).read_text())
    assert record["exit_code"] == 0 and record["remote_writes"] == []
    report = json.loads((Path(result["output_directory"]) / "offline.json").read_text())
    assert report["source_readonly"] and report["cpu_count"] == 2
    assert report["consumer_pid"] != report["producer_pid"]
    assert "Progress update: 100%" in stderr and "Sending output" in stderr
    data["parameters"] = {"fail": True, "pause": 0}
    config.write_text(json.dumps(data))
    result, stderr = invoke(config, installed_hexa, expected=1)
    assert result["exit_code"] == 1
    assert "synthetic-token" not in stderr and "signature=" not in stderr
    assert (
        "Synthetic failure" in stderr
        and "produce" in stderr
        and "pipeline.py" in stderr
    )
    assert not (Path(result["output_directory"]) / "offline.json").exists()


def test_concurrent_runs_share_the_same_labelled_source(
    installed_hexa: Path, podman_mode: dict[str, str]
):
    config, _, _ = prepare(podman_mode)
    command = [str(installed_hexa), "run", "--config", str(config), "--json"]
    processes = [
        subprocess.Popen(
            command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        for _ in range(2)
    ]
    for process in processes:
        _, stderr = process.communicate(timeout=120)
        assert process.returncode == 0, stderr


def test_unreadable_input_prints_a_hint_not_a_traceback(
    installed_hexa: Path, podman_mode: dict[str, str]
):
    config, _, data = prepare(podman_mode)
    unreadable = config.parent / "input"
    data["inputs"] = [{"path": "input", "name": "data", "relabel": False}]
    config.write_text(json.dumps(data))
    unreadable.chmod(0)
    try:
        result, stderr = invoke(config, installed_hexa, expected=77)
    finally:
        unreadable.chmod(0o700)
    assert result["exit_code"] == 77
    assert "hexa: cannot read /inputs/data (permission denied)" in stderr
    assert "Hint: A mount is unreadable" in stderr and "Traceback" not in stderr


@pytest.mark.parametrize("fail", [False, True])
def test_installed_wheel_local_resources_without_remote_access(
    installed_hexa: Path, podman_mode: dict[str, str], fail: bool
):
    config, source, data = prepare(podman_mode)
    shutil.copy(FIXTURES / "local_resources_pipeline.py", source / "pipeline.py")
    data["parameters"] = {"fail": fail}
    config.write_text(json.dumps(data))
    dataset = config.parent / "population"
    for version in ("v2", "v10"):
        (dataset / version).mkdir(parents=True)
        (dataset / version / "data.csv").write_bytes(b"value\n42\n")
    connections = config.parent / "connections.env"
    connections.write_text(
        "LOCAL_DHIS=DHIS2\nLOCAL_DHIS_URL=https://example.invalid\nLOCAL_DHIS_USERNAME=fixture-user\nLOCAL_DHIS_PASSWORD=fixture-password\n"
    )
    process = subprocess.run(
        [
            str(installed_hexa),
            "run",
            "--config",
            str(config),
            "--local-dataset",
            f"population={dataset}",
            "--connections-file",
            str(connections),
            "--json",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert process.returncode == (1 if fail else 0), process.stderr
    result = json.loads(process.stdout)
    assert result["exit_code"] == process.returncode and result["remote_writes"] == []
    assert (dataset / "v11" / "nested" / "result.txt").read_bytes() == b"local output"
    retained = (
        Path(result["record_file"]).read_text() + Path(result["log_file"]).read_text()
    )
    assert "fixture-password" not in process.stdout + process.stderr + retained
    assert "synthetic-token" not in process.stdout + process.stderr + retained
    if fail:
        assert "Synthetic local resource failure" in process.stderr
    else:
        report = json.loads(
            (Path(result["output_directory"]) / "resources.json").read_text()
        )
        assert report["source_readonly"] and report["latest"] == "v11"
        with pytest.raises(requests.ConnectionError):
            requests.get(report["download_url"], timeout=1)


@pytest.mark.api
def test_connected_guarded_edit_rerun_and_download(
    installed_hexa: Path, podman_mode: dict[str, str], integration: dict[str, Any]
):
    config, source, data = prepare(podman_mode, integration)
    first, logs = invoke(config, installed_hexa)
    output = Path(first["output_directory"])
    summary = json.loads((output / "summary.json").read_text())
    assert summary["bytes"] > 0 and summary["processing_value"] == 42
    operations = [
        json.loads(line)
        for line in (output / "operations.jsonl").read_text().splitlines()
    ]
    assert all(item["allowed"] for item in operations)
    assert {"getDataset", "getConnection", "STORAGE_GET"} <= {
        item["operation"] for item in operations
    }
    assert "Successfully completed" in logs
    pipeline = source / "pipeline.py"
    pipeline.write_text(
        pipeline.read_text().replace("PROCESSING_VALUE = 42", "PROCESSING_VALUE = 43")
    )
    second, _ = invoke(config, installed_hexa)
    changed = json.loads(
        (Path(second["output_directory"]) / "summary.json").read_text()
    )
    assert changed["processing_value"] == 43 and changed["sha256"] == summary["sha256"]
    assert first["image"] == second["image"]
    data["parameters"]["fail_processing"] = True
    config.write_text(json.dumps(data))
    failed, logs = invoke(config, installed_hexa, expected=1)
    assert "Deliberate processing failure" in logs
    assert not (Path(failed["output_directory"]) / "summary.json").exists()
    destination = config.parent / "downloaded"
    result = subprocess.run(
        [
            str(installed_hexa),
            "datasets",
            "download",
            integration["dataset"],
            summary["file_id"],
            "--version",
            summary["version_id"],
            "--output",
            str(destination),
            "--config",
            str(config),
            "--json",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    downloaded = json.loads(result.stdout)
    assert downloaded["bytes"] == summary["bytes"]
    assert downloaded["sha256"] == summary["sha256"]
    token = load_credentials(integration["credential_file"]).token
    if token in logs + result.stdout + result.stderr:
        pytest.fail("API token leaked in CLI output")
