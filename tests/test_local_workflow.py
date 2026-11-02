"""Multi-pipeline selection and bounded offline checks; never execute source."""

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hexa.cli import main
from hexa.commands.common import Invocation
from hexa.config import (
    PACKAGE_DIR,
    PINNED_IMAGE,
    Config,
    Project,
    SetupError,
    load_credentials,
    load_parameters,
    podman_command,
)
from hexa.parameters import inspect_pipeline, validate_parameters
from hexa.runner import run, startup_hint


def pipeline_at(directory: Path, declarations: str = "") -> Path:
    directory.mkdir(parents=True)
    path = directory / "pipeline.py"
    path.write_text(
        "raise RuntimeError('source must never execute')\n"
        "from openhexa.sdk import pipeline, parameter\n"
        '@pipeline(name="Local example")\n'
        + declarations
        + "def example(**kwargs): pass\n"
    )
    return path


def test_directory_selection_keeps_project_scope_and_isolates_inputs(
    tmp_path, monkeypatch, capsys
):
    project = tmp_path / "project"
    first = project / "pipelines" / "first"
    second = project / "pipelines" / "second"
    pipeline_at(first, '@parameter("year", type=int, default=2024)\n')
    pipeline_at(second, '@parameter("dry_run", type=bool)\n')
    (project / ".env").write_text(
        "OPENHEXA_URL=https://example.invalid\nOPENHEXA_WORKSPACE=test\nOPENHEXA_TOKEN=synthetic-token\n"
    )
    config = project / "hexa.json"
    config.write_text(
        json.dumps(
            {
                "source": {"path": "pipelines/first", "relabel": True},
                "parameters": {"year": 2025},
            }
        )
    )
    caller = tmp_path / "caller"
    caller.mkdir()
    monkeypatch.chdir(caller)
    inputs = caller / "inputs.json"
    inputs.write_text('{"years": [], "optional": null}')
    observed = []

    def execute(settings, credentials):
        observed.append(settings)
        assert settings.project_dir == project
        assert credentials.workspace == "test"
        assert settings.credential_file == project / ".env"
        assert settings.image == PINNED_IMAGE
        return {"exit_code": 7}

    monkeypatch.setattr("hexa.cli.run", execute)
    common = ["--config", str(config), "--json"]
    for args in (
        [],
        ["../project/pipelines/first"],
        ["../project/pipelines/second"],
        [
            "../project/pipelines/second",
            "--parameters",
            "inputs.json",
            "--set",
            "dry_run=false",
        ],
        [
            "../project/pipelines/first",
            "--parameters",
            "inputs.json",
            "--set",
            'connection="demo"',
        ],
    ):
        assert main(["run", *args, *common]) == 7
        assert json.loads(capsys.readouterr().out)["exit_code"] == 7
    assert [item.source.path for item in observed] == [
        first,
        first,
        second,
        second,
        first,
    ]
    assert all(item.source.relabel for item in observed)
    assert [item.parameters for item in observed] == [
        {"year": 2025},
        {"year": 2025},
        {},
        {"years": [], "optional": None, "dry_run": False},
        {"years": [], "optional": None, "connection": "demo"},
    ]
    (second / ".env").write_text("PRIVATE=value")
    with pytest.raises(SetupError, match="dotenv"):
        Config.from_project(Project.load(config), second)
    with pytest.raises(SetupError, match="pipeline subdirectory"):
        Config.from_project(Project.load(config), project)


@pytest.mark.parametrize(
    ("text", "overrides"),
    [
        ('{"year": 1, "year": 2}', ()),
        ('{"nested": {"x": 1, "x": 2}}', ()),
        ("[]", ()),
        ("{}", ("year=1", "year=2")),
        ("{}", ("year=not-json",)),
        ("{}", ("year",)),
        ('{"year": NaN}', ()),
        ("{}", ('x={"a": 1, "a": 2}',)),
    ],
)
def test_input_loader_rejects_ambiguous_or_non_json_inputs(tmp_path, text, overrides):
    inputs = tmp_path / "inputs.json"
    inputs.write_text(text)
    with pytest.raises(ValueError):
        load_parameters(Project.load(), tmp_path, inputs, overrides)


def test_offline_commands_never_load_credentials_import_source_or_invoke_services(
    tmp_path, monkeypatch, capsys
):
    pipeline_at(
        tmp_path / "source",
        (
            '@parameter("dataset", type=Dataset, help="Workspace slug")\n'
            '@parameter("connection", type=DHIS2Connection)\n'
            '@parameter("years", type=int, multiple=True, default=[2024], choices=[2024, 2025])\n'
            '@parameter("dry_run", type=bool)\n'
        ),
    )
    monkeypatch.chdir(tmp_path)
    (tmp_path / "hexa.json").write_text(
        json.dumps(
            {
                "source": {"path": "source", "relabel": True},
                "credential_file": "missing.env",
                "podman": "not-an-engine",
                "image": "not-an-image",
                "parameters": {"dataset": "population", "connection": "demo"},
            }
        )
    )

    def forbidden(*args, **kwargs):
        pytest.fail(
            "Offline commands must not load credentials, access APIs or invoke processes"
        )

    monkeypatch.setattr(Invocation, "load", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr("hexa.api.API._graphql", forbidden)
    assert main(["inspect", "source", "--json"]) == 0
    schema = json.loads(capsys.readouterr().out)
    assert schema["pipeline"]["name"] == "Local example"
    assert schema["supported"] is True
    assert [p["code"] for p in schema["parameters"]] == [
        "dataset",
        "connection",
        "years",
        "dry_run",
    ]
    assert schema["parameters"][2]["default"] == [2024]
    assert main(["validate", "source", "--set", "years=[]", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["valid"] is True
    assert (
        "workspace existence, visibility and connection type were not checked"
        in result["limitations"]
    )
    assert "population" not in json.dumps(result) and "parameters" not in result
    assert (
        main(
            [
                "validate",
                "source",
                "--set",
                "dataset=123",
                "--set",
                "years=[2026]",
                "--json",
            ]
        )
        == 2
    )
    result = json.loads(capsys.readouterr().out)
    assert result["code"] == "INVALID_INPUT" and result["valid"] is False
    assert len(result["diagnostics"]) == 2
    assert "123" not in json.dumps(result) and "2026" not in json.dumps(result)
    assert main(["inspect", "source"]) == 0
    assert "Local example" in capsys.readouterr().out
    assert main(["validate", "source", "--set", 'unknown="never-print-this"']) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.startswith("Error: ")
    assert "Unknown parameter code(s)" in captured.err
    assert "connection type were not checked" in captured.err
    assert "never-print-this" not in captured.err


@pytest.mark.parametrize(
    ("kind", "metadata", "value", "valid"),
    [
        ("str", "", None, False),
        ("str", ", required=False", "  ", True),
        ("str", ', default="a", choices=["a"]', None, True),
        ("str", ', default="a", choices=["a"]', "  ", True),
        ("str", ', choices=["a"]', " a ", True),
        ("str", ', choices=["a"]', "b", False),
        ("int", "", True, True),
        ("int", "", 1.0, False),
        ("float", "", 1, True),
        ("float", "", True, True),
        ("bool", "", None, True),
        ("bool", "", "false", False),
        ("str", ", multiple=True, required=False", [None, " "], True),
        ("int", ", multiple=True", [], False),
        ("int", ", multiple=True, default=[1]", [], True),
        ("int", ", multiple=True, default=[1]", None, True),
        ("int", ", multiple=True", [None, 1], True),
        ("int", ", multiple=True", 1, False),
    ],
)
def test_supported_normalization_matches_pinned_sdk_without_changing_explicit_inputs(
    tmp_path, kind, metadata, value, valid
):
    from openhexa.sdk.pipelines.exceptions import (
        InvalidParameterError,
        ParameterValueError,
    )
    from openhexa.sdk.pipelines.parameter import Parameter

    path = pipeline_at(
        tmp_path / "source", f'@parameter("value", type={kind}{metadata})\n'
    )
    schema = inspect_pipeline(path)
    assert schema["supported"] is True
    parameter = schema["parameters"][0]
    types = {"str": str, "int": int, "float": float, "bool": bool}
    sdk_parameter = Parameter(
        "value",
        type=types[kind],
        **{
            key: parameter[key]
            for key in ("required", "multiple", "default", "choices")
        },
    )
    if valid:
        sdk_parameter.validate(value)
    else:
        with pytest.raises((InvalidParameterError, ParameterValueError)):
            sdk_parameter.validate(value)
    values = {"value": value}
    before = json.dumps(values)
    assert validate_parameters(schema, values)["valid"] is valid
    assert json.dumps(values) == before
    if value is None:
        assert validate_parameters(schema, {})["valid"] is valid


@pytest.mark.parametrize(
    "declarations",
    [
        '@parameter("value", type=int, default=compute())\n',
        '@parameter("value", type=str, choices="choices.json")\n',
        '@parameter("value", type=str, widget=widget)\n',
        '@parameter("value", type=Alias)\n',
        '@parameter("value", type=bool, disables=["other"])\n',
        '@parameter("value", type=int, multiple=True, default=(1, 2))\n',
    ],
)
def test_unsupported_static_declarations_do_not_gate_existing_execution(
    tmp_path, monkeypatch, capsys, declarations
):
    pipeline_at(tmp_path / "source", declarations)
    (tmp_path / ".env").write_text(
        "OPENHEXA_URL=https://example.invalid\nOPENHEXA_WORKSPACE=test\nOPENHEXA_TOKEN=synthetic-token\n"
    )
    monkeypatch.chdir(tmp_path)
    assert main(["inspect", "source", "--json"]) == 2
    schema = json.loads(capsys.readouterr().out)
    assert schema["supported"] is False and ":4:" in schema["diagnostics"][0]
    assert main(["validate", "source", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["valid"] is False
    monkeypatch.setattr("hexa.cli.run", lambda settings, credentials: {"exit_code": 0})
    assert main(["run", "source", "--set", "value=null", "--json"]) == 0
    capsys.readouterr()


@pytest.mark.parametrize(
    "source",
    [
        "from openhexa.sdk import pipeline as flow\n@flow(name='Alias')\ndef example(): pass\n",
        "@pipeline(name='First')\ndef first(): pass\n@pipeline(name='Second')\ndef second(): pass\n",
        "@pipeline(name=compute())\ndef example(): pass\n",
        "def invalid(:\n",
    ],
)
def test_parser_limits_are_actionable_without_evaluating_source(tmp_path, source):
    path = tmp_path / "pipeline.py"
    path.write_text(source)
    result = inspect_pipeline(path)
    assert result["exit_code"] == 2 and result["supported"] is False
    assert result["diagnostics"] and all(
        str(path) in item for item in result["diagnostics"]
    )
    assert "compute()" not in json.dumps(result)


@pytest.mark.parametrize("secret_type", ["Secret", "Password"])
def test_secret_defaults_are_withheld_and_new_secret_inputs_rejected(
    tmp_path, monkeypatch, capsys, secret_type
):
    path = pipeline_at(
        tmp_path / "source",
        (
            f'@parameter("password", type={secret_type}, default="never-echo-this")\n'
            '@parameter("other", type=int, default=compute())\n'
        ),
    )
    if secret_type == "Password":
        path.write_text(
            "from openhexa.sdk import Secret as Password\n" + path.read_text()
        )
    monkeypatch.chdir(tmp_path)
    assert main(["inspect", "source", "--json"]) == 2
    result = capsys.readouterr()
    assert "never-echo-this" not in result.out + result.err
    assert json.loads(result.out)["parameters"][0]["default"] == "[withheld]"
    assert (
        main(["validate", "source", "--set", 'password="private-value"', "--json"]) == 2
    )
    assert "Secret parameter overrides" in capsys.readouterr().out
    (tmp_path / ".env").write_text(
        "OPENHEXA_URL=https://example.invalid\nOPENHEXA_WORKSPACE=test\nOPENHEXA_TOKEN=synthetic-token\n"
    )
    (tmp_path / "inputs.json").write_text('{"password": "private-value"}')
    for options in (
        ["--set", 'password="private-value"'],
        ["--parameters", "inputs.json"],
    ):
        assert main(["run", "source", *options, "--json"]) == 2
        result = capsys.readouterr()
        assert "Secret parameter overrides" in result.out
        assert "private-value" not in result.out + result.err


def test_custom_entrypoint_and_parameter_isolation_apply_to_validation(
    tmp_path, monkeypatch, capsys
):
    path = pipeline_at(tmp_path / "first", '@parameter("year", type=int)\n')
    path.rename(path.with_name("custom.py"))
    path = pipeline_at(tmp_path / "second", '@parameter("year", type=int)\n')
    path.rename(path.with_name("custom.py"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "hexa.json").write_text(
        json.dumps(
            {
                "source": {"path": "first", "relabel": False},
                "entrypoint": "custom.py",
                "parameters": {"year": 2025},
            }
        )
    )
    assert main(["validate", "first", "--json"]) == 0
    capsys.readouterr()
    assert main(["validate", "second", "--json"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["diagnostics"] == [
        "Parameter year: a value or nonempty default is required."
    ]
    (tmp_path / "empty.json").write_text("{}")
    assert main(["validate", "first", "--parameters", "empty.json", "--json"]) == 2
    capsys.readouterr()


def test_selected_mounts_get_a_shared_label_unless_opted_out(
    tmp_path, monkeypatch, capsys, json_shape
):
    project = tmp_path / "project"
    pipeline_at(project / "pipelines" / "first")
    pipeline_at(project / "pipelines" / "other")
    (project / "data").mkdir()
    (project / ".env").write_text(
        "OPENHEXA_URL=https://example.invalid\nOPENHEXA_WORKSPACE=test\nOPENHEXA_TOKEN=synthetic-token\n"
    )
    config = project / "hexa.json"
    commands = []
    results = []

    class Process:
        def __init__(self, command, **kwargs):
            commands.append(command)
            self.stdin = io.StringIO()
            self.stdout = io.StringIO("")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def wait(self):
            return 0

    monkeypatch.setattr("hexa.runner.subprocess.run", lambda *a, **k: None)
    monkeypatch.setattr("hexa.runner.subprocess.Popen", Process)

    def volumes(data: dict, directory: str | None = None) -> list[str]:
        config.write_text(json.dumps(data))
        settings = Config.from_project(
            Project.load(config), None if directory is None else Path(directory)
        )
        results.append(run(settings, load_credentials(project / ".env")))
        command = commands.pop()
        return [command[i + 1] for i, a in enumerate(command) if a == "--volume"]

    monkeypatch.chdir(project)
    labelled = volumes(
        {
            "source": {"path": "pipelines/first"},
            "inputs": [{"path": "data", "name": "data"}],
        },
        "pipelines/other",
    )
    assert labelled[4:] == [
        f"{project / 'pipelines' / 'other'}:/source:ro,z",
        f"{project / 'data'}:/inputs/data:ro,z",
    ]
    assert all(item.endswith(",Z") for item in labelled[:4])
    assert "SELinux label :z (shared)" in capsys.readouterr().err
    unlabelled = volumes(
        {
            "source": {"path": "pipelines/first", "relabel": False},
            "inputs": [{"path": "data", "name": "data", "relabel": False}],
        },
        "pipelines/other",
    )
    assert [item.rsplit(":", 1)[1] for item in unlabelled[4:]] == ["ro", "ro"]
    capsys.readouterr()
    assert main(["run", "--help"]) == 0
    json_shape(
        capsys.readouterr().out,
        {
            **results[-1],
            "hint": None,
            "remote_writes": [{"operation": "x", "success": True}],
        },
    )


def test_home_and_directories_containing_home_are_refused(tmp_path, monkeypatch):
    home = tmp_path / "home"
    pipeline_at(home)
    (tmp_path / ".env").write_text(
        "OPENHEXA_URL=https://example.invalid\nOPENHEXA_WORKSPACE=test\nOPENHEXA_TOKEN=synthetic-token\n"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    for directory in (home, tmp_path):
        with pytest.raises(SetupError, match=f"Refusing to mount {directory}:"):
            Config.from_project(Project.load(), directory)


def test_unreadable_entrypoint_exits_77_with_one_line(tmp_path):
    entrypoint = pipeline_at(tmp_path / "source")
    entrypoint.chmod(0)
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("HEXA_", "OPENHEXA_"))
    }
    env.update(
        PYTHONPATH=str(PACKAGE_DIR / "runtime"),
        HEXA_CONNECTED_LOCAL="1",
        HEXA_ENVIRONMENT="LOCAL_PIPELINE",
    )
    with pytest.raises(subprocess.CalledProcessError) as failure:
        subprocess.run(
            [
                sys.executable,
                str(PACKAGE_DIR / "runtime" / "bootstrap.py"),
                str(entrypoint),
            ],
            env=env,
            input="",
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    result = failure.value
    assert result.returncode == 77
    assert result.stderr == f"hexa: cannot read {entrypoint} (permission denied)\n"


def test_startup_failures_map_to_a_single_hint(tmp_path, monkeypatch):
    pipeline_at(tmp_path / "source")
    (tmp_path / "data").mkdir()
    (tmp_path / ".env").write_text(
        "OPENHEXA_URL=https://example.invalid\nOPENHEXA_WORKSPACE=test\nOPENHEXA_TOKEN=synthetic-token\n"
    )
    (tmp_path / "hexa.json").write_text(
        json.dumps(
            {
                "source": {"path": "source"},
                "inputs": [{"path": "data", "name": "data", "relabel": False}],
            }
        )
    )
    monkeypatch.chdir(tmp_path)
    config = Config.from_project(Project.load())
    probes = {
        "getenforce": "Enforcing",
        "ls": "unconfined_u:object_r:user_home_t:s0 /path",
    }
    monkeypatch.setattr(
        "hexa.runner.subprocess.run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, probes[command[1]] + "\n"
        ),
    )
    hint = startup_hint(
        config, 77, ["hexa: cannot read /inputs/data/x.csv (permission denied)"]
    )
    assert hint is not None
    assert "SELinux: Enforcing" in hint and "label: unconfined_u:" in hint
    assert f"host path: {tmp_path / 'data'}" in hint and "relabel: false" in hint
    hint = startup_hint(
        config,
        78,
        [
            (
                "hexa: startup adapter refused: "
                "openhexa.sdk 3.0.0: CurrentRun._connected is not a property"
            )
        ],
    )
    assert hint is not None and "_connected is not a property" in hint
    assert startup_hint(config, 1, []) is None


def test_run_record_log_and_write_summary(tmp_path, monkeypatch, capsys):
    pipeline_at(
        tmp_path / "source",
        '@parameter("year", type=int)\n@parameter("key", type=Secret)\n',
    )
    (tmp_path / ".env").write_text(
        "OPENHEXA_URL=https://example.invalid\nOPENHEXA_WORKSPACE=test\nOPENHEXA_TOKEN=synthetic-token\n"
    )
    (tmp_path / "hexa.json").write_text(
        json.dumps(
            {"source": {"path": "source"}, "parameters": {"year": 2025, "key": "k"}}
        )
    )
    monkeypatch.chdir(tmp_path)
    writes: list[dict] = []

    class Process:
        def __init__(self, command, **kwargs):
            volumes = [command[i + 1] for i, a in enumerate(command) if a == "--volume"]
            record = next(v.split(":")[0] for v in volumes if ":/record:" in v)
            with open(Path(record) / "writes.jsonl", "w") as ledger:
                ledger.writelines(json.dumps(entry) + "\n" for entry in writes)
            self.stdin = io.StringIO()
            self.stdout = io.StringIO("working with synthetic-token\n")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def wait(self):
            return 0

    monkeypatch.setattr("hexa.runner.subprocess.run", lambda *a, **k: None)
    monkeypatch.setattr("hexa.runner.subprocess.Popen", Process)
    config = Config.from_project(Project.load())
    credentials = load_credentials(tmp_path / ".env")
    result = run(config, credentials)
    stderr = capsys.readouterr().err
    assert "Remote writes (via SDK; other HTTP clients are not tracked): none" in stderr
    log = Path(result["log_file"]).read_text()
    assert "synthetic-token" not in log and "working with [TOKEN withheld]" in log
    record = json.loads(Path(result["record_file"]).read_text())
    assert record["parameters"] == {"year": 2025, "key": "[withheld]"}
    assert record["exit_code"] == 0 and record["remote_writes"] == []
    assert len(record["source"]["sha256"]) == 64
    writes.extend(
        [
            {
                "operation": "createDatasetVersion",
                "success": True,
                "version": {"id": "5b53aaaa", "name": "v2"},
            },
            *[{"operation": "createDatasetVersionFile", "success": True}] * 6,
        ]
    )
    result = run(config, credentials)
    assert result["remote_writes"] == writes
    assert (
        "Remote writes (via SDK; other HTTP clients are not tracked): "
        "createDatasetVersion → v2 (5b53…); createDatasetVersionFile ×6"
    ) in capsys.readouterr().err


def test_doctor_reports_each_status_without_reading_credentials(
    tmp_path, monkeypatch, capsys, json_shape
):
    pipeline_at(tmp_path / "source")
    (tmp_path / "hexa.json").write_text(json.dumps({"source": {"path": "source"}}))
    monkeypatch.chdir(tmp_path)
    host = {"getenforce": "Enforcing", "ls": "system_u:object_r:user_home_t:s0 /x"}
    cached = {"value": False}

    def fake_run(command, **kwargs):
        name = command[1]
        if name == "podman":
            if not cached["value"]:
                raise subprocess.CalledProcessError(125, command)
            return subprocess.CompletedProcess(command, 0, "id\n")
        if name == "test":
            return subprocess.CompletedProcess(command, 0, "")
        if name not in host:
            raise FileNotFoundError(name)
        return subprocess.CompletedProcess(command, 0, host[name] + "\n")

    monkeypatch.setattr("hexa.doctor.subprocess.run", fake_run)
    monkeypatch.setattr(
        "hexa.config.load_credentials",
        lambda path: pytest.fail("doctor must not read credentials"),
    )
    assert main(["doctor", "--json"]) == 2
    report = json.loads(capsys.readouterr().out)
    statuses = {check["name"]: check["status"] for check in report["checks"]}
    assert statuses == {
        "config": "ok",
        "credential_file": "missing",
        "podman": "ok",
        "source": "ok",
        "mount /source": "ok",
        "selinux": "ok",
        "image": "missing",
    }
    assert "does not pull images" in report["checks"][-1]["hint"]
    mounts = {m["target"]: m for m in report["container"]["mounts"]}
    assert mounts["/source"]["mode"] == "ro,z"
    assert mounts["/source"]["selinux_label"] == "system_u:object_r:user_home_t:s0"
    assert mounts["/scratch"]["host_path"].endswith("/tmp")
    assert "HEXA_TOKEN" not in report["container"]["environment"]
    assert main(["doctor", "--help"]) == 0
    json_shape(capsys.readouterr().out, report)
    (tmp_path / ".env").write_text("")
    cached["value"] = True
    del host["getenforce"]
    assert main(["doctor"]) == 0
    text = capsys.readouterr().out
    assert "unchecked selinux" in text and "Container: " in text
    (tmp_path / "source" / ".env").write_text("X=1")
    assert main(["doctor", "--json"]) == 2
    report = json.loads(capsys.readouterr().out)
    check = next(c for c in report["checks"] if c["name"] == "mount /source")
    assert check["status"] == "missing" and "dotenv" in check["detail"]


@pytest.mark.parametrize("configured", [None, "blsq/openhexa-blsq-environment:1.17.11"])
def test_doctor_workspace_describes_the_hosted_image(
    tmp_path, monkeypatch, capsys, configured
):
    pipeline_at(tmp_path / "source")
    (tmp_path / "hexa.json").write_text(json.dumps({"source": {"path": "source"}}))
    (tmp_path / ".env").write_text(
        "OPENHEXA_URL=https://example.invalid\nOPENHEXA_WORKSPACE=test\nOPENHEXA_TOKEN=synthetic-token\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "hexa.doctor.subprocess.run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "x\n"),
    )
    monkeypatch.setattr(
        "hexa.api.API.workspace",
        lambda self: {"slug": "test", "dockerImage": configured},
    )
    assert main(["doctor", "--workspace", "--json"]) == 0
    captured = capsys.readouterr()
    hosted = json.loads(captured.out)["hosted_image"]
    assert "synthetic-token" not in captured.out + captured.err
    if configured:
        assert hosted["source"] == "configured on workspace"
        assert hosted["image"] == configured
    else:
        assert hosted["source"] == "platform default"
        assert hosted["tags_url"].startswith("https://hub.docker.com/")
        assert "not https://example.invalid" in hosted["server_url"]


@pytest.mark.parametrize(
    ("mode", "distrobox", "expected"),
    [
        ("auto", True, ("distrobox-host-exec", "podman")),
        ("auto", False, ("podman",)),
        ("host", False, ("distrobox-host-exec", "podman")),
        ("direct", True, ("podman",)),
    ],
)
def test_podman_command_detects_distrobox_only_in_auto_mode(
    monkeypatch, mode, distrobox, expected
):
    monkeypatch.setattr("hexa.config.in_distrobox", lambda: distrobox)
    assert podman_command(mode) == expected


def test_unknown_podman_mode_is_rejected():
    with pytest.raises(SetupError, match="'auto', 'host'"):
        podman_command("docker")
