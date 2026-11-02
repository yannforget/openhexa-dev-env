"""Container acceptance fixture: spawned tasks, mounts, reporting, failure leaks."""

import json
import os
import sys
import time
from pathlib import Path

import httpx
import requests
from openhexa.sdk import current_run, parameter, pipeline, workspace

assert current_run is not None


def reject_network(*args, **kwargs):
    """Offline fixture forbids all HTTP, including cloud reporting."""
    raise RuntimeError("Unexpected network attempt in offline fixture")


requests.Session.request = reject_network
httpx.Client.request = reject_network


@pipeline(name="Offline runner regression")
@parameter("fail", type=bool, default=False)
@parameter("pause", type=int, default=0)
def check(fail: bool, pause: int) -> None:
    current_run.log_info("Parent reporting is local")
    produced = produce(fail, pause)
    consume(produced)


@check.task
def produce(fail: bool, pause: int) -> str:
    time.sleep(pause)
    if fail:
        token = os.environ["HEXA_TOKEN"]
        for fragment in [
            "worker secret: ",
            token[:4],
            token[4:],
            " https://storage.invalid/file?",
            "signature=synthetic-secret\n",
        ]:
            sys.stdout.write(fragment)
            sys.stdout.flush()
        raise ValueError(
            "Synthetic failure "
            + token
            + " https://storage.invalid/?signature=exception-secret"
        )
    assert os.process_cpu_count() == 2
    assert Path("/inputs/data/input.txt").read_text() == "local input\n"
    try:
        Path("/source/forbidden.txt").write_text("must fail")
    except OSError:
        readonly = True
    else:
        readonly = False
    assert readonly
    path = Path(workspace.files_path) / "offline.json"
    path.write_text(
        json.dumps(
            {
                "producer_pid": os.getpid(),
                "cpu_count": os.process_cpu_count(),
                "source_readonly": readonly,
            }
        )
    )
    current_run.log_info("Worker reporting is local")
    current_run.add_file_output(str(path))
    return str(path)


@check.task
def consume(path: str) -> None:
    report = json.loads(Path(path).read_text())
    report["consumer_pid"] = os.getpid()
    Path(path).write_text(json.dumps(report))
    current_run.log_info("Dependent worker reporting is local")


if __name__ == "__main__":
    check()
