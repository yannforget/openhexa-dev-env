"""Remote run launch and waiting against a fake server: one dispatch, state machine."""

import json

import pytest

from hexa.api import API, APIError
from hexa.cli import main
from hexa.config import Credentials, SetupError

CREDENTIALS = Credentials("https://example.invalid", "selected", "synthetic-token")
PIPELINE = {
    "id": "pipeline-id",
    "code": "quality",
    "name": "Quality",
    "currentVersion": {"id": "v2-id"},
    "permissions": {"run": True},
}
PARAMETERS = [
    {
        "code": "mode",
        "type": "str",
        "required": True,
        "multiple": False,
        "default": None,
        "choices": ["bad", "corrected"],
    },
    {
        "code": "seed",
        "type": "int",
        "required": True,
        "multiple": False,
        "default": 42,
        "choices": None,
    },
    {
        "code": "source",
        "type": "dataset",
        "required": False,
        "multiple": False,
        "default": None,
        "choices": None,
    },
    {
        "code": "api_key",
        "type": "secret",
        "required": False,
        "multiple": False,
        "default": None,
        "choices": None,
    },
]


class Server:
    """Answers start/wait reads; statuses are served in order, the last one repeats."""

    def __init__(self):
        self.mutations: list[dict] = []
        self.statuses = ["queued", "running", "running", "success"]
        self.polls = 0
        self.failure: APIError | None = None

    def graphql(self, query, variables):
        if query.startswith("mutation"):
            self.mutations.append(variables["input"])
            if self.failure is not None:
                raise self.failure
            return {
                "runPipeline": {
                    "success": True,
                    "errors": [],
                    "run": {"id": "run-id", "status": "queued"},
                }
            }
        if "query Pipelines" in query:
            return {
                "pipelines": {
                    "pageNumber": 1,
                    "totalPages": 1,
                    "totalItems": 1,
                    "items": [PIPELINE],
                }
            }
        if "versions(" in query:
            items = [
                {
                    "id": "v2-id",
                    "name": "v2",
                    "versionName": "v2 [v2]",
                    "versionNumber": 2,
                }
            ]
            return {
                "pipeline": {
                    "versions": {
                        "pageNumber": 1,
                        "totalPages": 1,
                        "totalItems": 1,
                        "items": items,
                    }
                }
            }
        if "query Version" in query:
            return {
                "pipelineVersion": {
                    "id": "v2-id",
                    "pipeline": {"id": "pipeline-id"},
                    "config": {"mode": "corrected"},
                    "parameters": PARAMETERS,
                }
            }
        if "query Status" in query:
            status = self.statuses[min(self.polls, len(self.statuses) - 1)]
            self.polls += 1
            return {
                "pipelineRun": {
                    "id": "00000000-0000-4000-8000-000000000001",
                    "status": status,
                    "progress": 50,
                    "pipeline": {"workspace": {"slug": "selected"}},
                }
            }
        if "query Run(" in query:
            status = self.statuses[min(self.polls, len(self.statuses)) - 1]
            return {
                "pipelineRun": {
                    "id": "00000000-0000-4000-8000-000000000001",
                    "status": status,
                    "executionDate": None,
                    "duration": 30,
                    "progress": 100,
                    "hasErrorMessages": False,
                    "version": {"id": "v2-id", "name": None, "versionNumber": 2},
                    "pipeline": {
                        "id": "pipeline-id",
                        "code": "quality",
                        "workspace": {"slug": "selected"},
                    },
                    "datasetVersions": [],
                    "outputs": [],
                }
            }
        if "query Logs" in query:
            return {
                "pipelineRun": {
                    "pipeline": {"workspace": {"slug": "selected"}},
                    "logs": "one\ntwo\nValueError: boom",
                }
            }
        raise AssertionError(query)


@pytest.fixture
def server(monkeypatch):
    fake = Server()
    monkeypatch.setattr(API, "graphql", lambda self, q, v: fake.graphql(q, v))
    monkeypatch.setattr("hexa.api.resources.time.sleep", lambda seconds: None)
    return fake


@pytest.fixture
def config(tmp_path):
    (tmp_path / ".env").write_text(
        "OPENHEXA_URL=https://example.invalid\nOPENHEXA_WORKSPACE=selected\n"
        "OPENHEXA_TOKEN=synthetic-token\n"
    )
    (tmp_path / "hexa.json").write_text(json.dumps({"credential_file": ".env"}))
    return str(tmp_path / "hexa.json")


def test_start_checks_inputs_against_the_version_and_dispatches_once(server):
    client = API(CREDENTIALS)
    invalid = client.start_run("quality", "v2", {"seed": "x", "other": 1})
    assert invalid["exit_code"] == 2
    assert any("other" in d for d in invalid["diagnostics"])
    assert any("seed: expected int" in d for d in invalid["diagnostics"])
    with pytest.raises(SetupError, match="Secret"):
        client.start_run("quality", "v2", {"api_key": "x"})
    planned = client.start_run("quality", "v2", {"seed": 7}, dry_run=True)
    assert planned["defaults_used"] == ["api_key", "mode", "source"]
    assert server.mutations == []
    result = client.start_run("quality", "2", {"seed": 7})
    assert result["run"] == {"id": "run-id", "status": "queued"}
    assert server.mutations == [
        {
            "id": "pipeline-id",
            "versionId": "v2-id",
            "config": {"seed": 7},
            "sendMailNotifications": False,
        }
    ]


def test_uncertain_launch_is_not_retried(server):
    server.failure = APIError("network", "API_FAILURE")
    with pytest.raises(APIError) as caught:
        API(CREDENTIALS).start_run("quality", "v2", {})
    assert caught.value.code == "UNCERTAIN_WRITE"
    assert "hexa runs list --pipeline quality" in str(caught.value)
    assert len(server.mutations) == 1


@pytest.mark.parametrize(
    ("statuses", "exit_code"),
    [
        (["queued", "running", "running", "success"], 0),
        (["running", "failed"], 1),
        (["running", "stopped"], 1),
        (["queued", "running"], 124),
    ],
)
def test_wait_prints_transitions_and_maps_outcomes_to_exit_codes(
    server, config, capsys, statuses, exit_code
):
    server.statuses = statuses
    timeout = ["--timeout", "0.001s"] if exit_code == 124 else []
    assert (
        main(
            [
                "runs",
                "wait",
                "00000000-0000-4000-8000-000000000001",
                "--config",
                config,
                *timeout,
            ]
        )
        == exit_code
    )
    captured = capsys.readouterr()
    shown = [line.split("] ")[1].split(" ")[0] for line in captured.err.splitlines()]
    expected = list(dict.fromkeys(statuses))
    assert shown == (expected[: len(shown)] if exit_code == 124 else expected)
    assert ("ValueError: boom" in captured.out) == (exit_code == 1)
    assert ("hexa stopped waiting" in captured.out) == (exit_code == 124)
    assert server.mutations == []


def test_start_requires_an_explicit_mode(server, config, capsys):
    base = ["runs", "start", "quality", "--version", "v2", "--config", config]
    assert main(base) == 2
    assert "Choose --dry-run or --yes" in capsys.readouterr().err
    assert main([*base, "--set", "seed=7", "--dry-run"]) == 0
    output = capsys.readouterr().out
    assert "Pipeline quality (pipeline-id), version v2 (v2-id)" in output
    assert "Defaults used: api_key, mode, source" in output
    assert output.rstrip().endswith("Rerun with --yes to launch.")
    assert server.mutations == []
