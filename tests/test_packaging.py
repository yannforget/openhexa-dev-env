"""Distribution contents and isolated installation, without API or container access."""

import json
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest


def test_installed_wheel_is_self_contained_and_distributions_exclude_secrets(
    installed_hexa: Path, tmp_path: Path
):
    installation = installed_hexa.parent.parent
    wheel = next((installation / "dist").glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        members = archive.namelist()
        assert "hexa/runtime/bootstrap.py" in members
        assert "hexa/runtime/sitecustomize.py" in members
        assert {
            "hexa/resources.py",
            "hexa/runtime/resource_manifest.py",
            "hexa/runtime/local_datasets.py",
            "hexa/runtime/local_connections.py",
            "hexa/runtime/file_server.py",
        } <= set(members)
        assert "hexa/cli.py" in members
        assert "hexa/py.typed" in members
        metadata_path = next(
            name for name in members if name.endswith(".dist-info/METADATA")
        )
        metadata = archive.read(metadata_path).decode()
        assert "Name: hexa\n" in metadata
        assert "Requires-Dist: openhexa-sdk" not in metadata
        assert not any(Path(name).name.startswith(".env") for name in members)
        assert not any("__pycache__" in name or ".hexa/" in name for name in members)
    source = next((installation / "dist").glob("*.tar.gz"))
    with tarfile.open(source) as archive:
        members = archive.getnames()
        assert any(name.endswith("tests/test_packaging.py") for name in members)
        assert any(name.endswith("examples/webapp/index.html") for name in members)
        assert any(
            name.endswith("skills/openhexa/references/webapps.md") for name in members
        )
        assert any(
            name.endswith("tests/fixtures/connected_pipeline.py") for name in members
        )
        assert any(name.endswith("tests/integration.example.json") for name in members)
        template = next(name for name in members if Path(name).name == ".env.example")
        example = archive.extractfile(template)
        assert example is not None
        assert (
            b"OPENHEXA_TOKEN=replace-with-designated-workspace-token" in example.read()
        )
        assert not any(
            Path(name).name.startswith(".env") and Path(name).name != ".env.example"
            for name in members
        )
        assert not any("__pycache__" in name or ".hexa/" in name for name in members)
    result = subprocess.run(
        [str(installed_hexa), "--help"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.stdout.startswith("Usage: hexa ") and "datasets" in result.stdout
    result = subprocess.run(
        [str(installed_hexa), "run", "--help"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert "--local-dataset" in result.stdout and "--connections-file" in result.stdout
    result = subprocess.run(
        [str(installed_hexa), "webapps", "create", "--help"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert "--directory" in result.stdout and "--dry-run" in result.stdout
    assert "--scope" in result.stdout and "--yes" in result.stdout
    result = subprocess.run(
        [str(installed_hexa), "pipelines", "deploy", "--help"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert "--version-name" in result.stdout and "--dry-run" in result.stdout
    result = subprocess.run(
        [str(installed_hexa), "runs", "start", "--help"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert "--version" in result.stdout and "--dry-run" in result.stdout
    python = installation / "tools" / "hexa" / "bin" / "python"
    subprocess.run(
        [
            str(python),
            "-c",
            "import importlib.util; from hexa.api import API; assert importlib.util.find_spec('openhexa') is None",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        timeout=10,
    )
    pipeline = tmp_path / "pipelines" / "example"
    pipeline.mkdir(parents=True)
    (pipeline / "pipeline.py").write_text(
        "raise RuntimeError('must not execute source')\n"
        "from openhexa.sdk import pipeline, parameter\n"
        '@pipeline(name="Wheel example")\n'
        '@parameter("year", type=int, default=2025)\n'
        "def example(year): pass\n"
    )
    (tmp_path / "inputs.json").write_text('{"year": 2024}')
    for command in (
        ["inspect", "pipelines/example"],
        [
            "validate",
            "pipelines/example",
            "--parameters",
            "inputs.json",
            "--set",
            "year=2025",
        ],
    ):
        result = subprocess.run(
            [str(installed_hexa), *command, "--json"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        detail = json.loads(result.stdout)
        assert detail["exit_code"] == 0
        assert detail["pipeline"]["name"] == "Wheel example"
        assert result.stderr == ""
    (tmp_path / "hexa.json").write_text(
        json.dumps({"podman": "direct", "source": {"path": "pipelines/example"}})
    )
    doctor = subprocess.run(
        [str(installed_hexa), "doctor", "--json"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    report = json.loads(doctor.stdout)
    assert doctor.returncode in (0, 2) and report["exit_code"] == doctor.returncode
    assert [c["name"] for c in report["checks"]][:4] == [
        "config",
        "credential_file",
        "podman",
        "source",
    ]
    assert report["container"]["mounts"][-1]["target"] == "/source"
    (tmp_path / "hexa.json").write_text(json.dumps({"credential_file": "missing.env"}))
    with pytest.raises(subprocess.CalledProcessError) as failed:
        subprocess.run(
            [str(installed_hexa), "workspace", "show", "--json"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    detail = json.loads(failed.value.stdout)
    assert failed.value.returncode == 2 and detail["code"] == "INVALID_INPUT"
    assert detail["message"].startswith(
        f"Credential file not found: {tmp_path / 'missing.env'}\n"
    )
    assert "OPENHEXA_TOKEN=your-workspace-token" in detail["message"]
    assert failed.value.stderr == ""
