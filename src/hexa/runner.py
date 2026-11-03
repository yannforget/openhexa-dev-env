"""Host Podman orchestration and merged-output redaction."""

import json
import shutil
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import PACKAGE_DIR, Config, Credentials, SetupError
from .io import combined_sha256
from .parameters import inspect_pipeline
from .security import Redactor, Writer

# Container exit statuses set by the bundled runtime before user code starts.
UNREADABLE_MOUNT = 77
ADAPTER_REFUSED = 78
# Directories left out of the source hash: tooling state, not pipeline code.
UNHASHED = frozenset({".git", "__pycache__", ".venv", ".hexa"})
TRACKING_LIMIT = "via SDK; other HTTP clients are not tracked"


class _Diagnostics:
    """Forward redacted output to the terminal and log, keeping `hexa:` lines."""

    def __init__(self, *destinations: Writer):
        self.destinations = destinations
        self.lines: list[str] = []

    def write(self, text: str) -> None:
        if text.startswith("hexa: "):
            self.lines.append(text.strip())
        for destination in self.destinations:
            destination.write(text)

    def flush(self) -> None:
        for destination in self.destinations:
            destination.flush()


def source_digest(directory: Path) -> str:
    """Combined SHA-256 over the relative paths and contents of regular source files."""
    return combined_sha256(
        (path.relative_to(directory).as_posix(), path.read_bytes())
        for path in sorted(directory.rglob("*"))
        if not UNHASHED & set(path.relative_to(directory).parts)
        and not path.is_symlink()
        and path.is_file()
    )


def _recorded_parameters(config: Config) -> dict[str, Any]:
    secrets = {
        item["code"]
        for item in inspect_pipeline(config.source.path / config.entrypoint)[
            "parameters"
        ]
        if item["type"] == "Secret"
    }
    return {
        code: "[withheld]" if code in secrets else value
        for code, value in config.parameters.items()
    }


def _read_ledger(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _identity(entry: dict[str, Any]) -> str:
    """Name the first recorded object of a ledger entry, e.g. ` → v2 (5b53…)`."""
    for name in ("version", "dataset", "file", "pipeline"):
        item = entry.get(name)
        if item:
            label = item.get("name") or item.get("slug")
            short = f"({item['id'][:4]}…)" if item.get("id") else None
            return " → " + " ".join(part for part in (label, short) if part)
    return ""


def summarise_writes(writes: list[dict[str, Any]]) -> str:
    """One line naming each SDK mutation, with identities for single writes."""
    if not writes:
        return f"Remote writes ({TRACKING_LIMIT}): none"
    groups: dict[str, list[dict[str, Any]]] = {}
    for entry in writes:
        groups.setdefault(entry["operation"], []).append(entry)
    parts = []
    for operation, entries in groups.items():
        failed = sum(entry.get("success") is False for entry in entries)
        text = (
            operation + _identity(entries[0])
            if len(entries) == 1
            else f"{operation} ×{len(entries)}"
        )
        parts.append(text + (f" ({failed} failed)" if failed else ""))
    return f"Remote writes ({TRACKING_LIMIT}): " + "; ".join(parts)


def _probe(config: Config, *command: str) -> str:
    """Run a read-only host command through the configured bridge; `unknown` if it fails."""
    bridge = config.podman[:-1]
    try:
        result = subprocess.run(
            [*bridge, *command],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (
        subprocess.CalledProcessError,
        FileNotFoundError,
        subprocess.TimeoutExpired,
    ):
        return "unknown"
    return result.stdout.strip() or "unknown"


def startup_hint(config: Config, status: int, diagnostics: list[str]) -> str | None:
    """Explain a failure that happened before the pipeline's code started."""
    if status == UNREADABLE_MOUNT:
        line = next(
            (item for item in diagnostics if item.startswith("hexa: cannot read ")),
            "",
        )
        target = line.removeprefix("hexa: cannot read ").rsplit(" (", 1)[0]
        mounts = {"/source": config.source}
        mounts.update({"/inputs/" + m.name: m for m in config.inputs})
        if config.local_resources:
            mounts.update(
                {
                    f"/local-datasets/{i}": d.mount
                    for i, d in enumerate(config.local_resources.datasets)
                }
            )
        mount = next(
            (
                m
                for prefix, m in mounts.items()
                if target == prefix or target.startswith(prefix + "/")
            ),
            config.source,
        )
        label = _probe(config, "ls", "-Zd", str(mount.path)).split(" ", 1)[0]
        cause = (
            "the mount is configured with relabel: false; remove it or relabel "
            "the directory yourself"
            if not mount.relabel
            else "a file in the mount is not readable by your user"
        )
        return (
            f"A mount is unreadable inside the container: {line or target}.\n"
            f"SELinux: {_probe(config, 'getenforce')}; host path: {mount.path}; "
            f"label: {label}; label mode: {mount.label_mode}.\n"
            f"Likely cause: {cause}."
        )
    if status == ADAPTER_REFUSED:
        line = next(
            (item for item in diagnostics if item.startswith("hexa: startup adapter")),
            "",
        )
        detail = line.removeprefix("hexa: startup adapter refused: ")
        return (
            "The runtime refused to start local reporting "
            f"({detail or 'see the output above'})."
        )
    return None


# Container resource limits, also reported by `hexa doctor`.
CPUS = 2
MEMORY = "2g"
# Tool-owned mounts created per run: target, directory under the run, mode.
OWNED_MOUNTS = (
    ("/startup", "startup", "ro,Z"),
    ("/output", "output", "rw,Z"),
    ("/scratch", "tmp", "rw,Z"),
    ("/record", "record", "rw,Z"),
)


def container_environment(origin: str, workspace: str) -> dict[str, str]:
    """Environment passed to the container; the token is never part of it."""
    return {
        "HEXA_ENVIRONMENT": "LOCAL_PIPELINE",
        "HEXA_CONNECTED_LOCAL": "1",
        "HEXA_SERVER_URL": origin,
        "HEXA_WORKSPACE": workspace,
        "HEXA_VERIFY_SSL": "True",
        "PYTHON_CPU_COUNT": str(CPUS),
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": "/startup:/source",
        "WORKSPACE_FILES_PATH": "/output",
        "WORKSPACE_TMP_PATH": "/scratch",
        "HEXA_RECORD_DIR": "/record",
    }


def container_mounts(
    config: Config, owned: dict[str, Path]
) -> list[tuple[str, Path, str]]:
    """Return `(target, host path, mode)` for every mount, tool-owned ones first.

    Args:
        config: Selected launcher settings.
        owned: Host directory for each tool-owned target in `OWNED_MOUNTS`.
    """
    mounts = [(target, owned[target], mode) for target, _, mode in OWNED_MOUNTS]
    for mount, target in [
        (config.source, "/source"),
        *((m, "/inputs/" + m.name) for m in config.inputs),
    ]:
        mounts.append((target, mount.path, "ro" + (",z" if mount.relabel else "")))
    if config.local_resources:
        for index, dataset in enumerate(config.local_resources.datasets):
            mounts.append(
                (
                    f"/local-datasets/{index}",
                    dataset.mount.path,
                    "rw" + (",z" if dataset.mount.relabel else ""),
                )
            )
    return mounts


def run(config: Config, credentials: Credentials) -> dict:
    """Run a fresh disposable container, preserving its exit status.

    Source and extra inputs remain live read-only mounts, with a shared SELinux
    label (`:z`) unless configured with `relabel: false`; a shared label lets
    concurrent runs use the same directory. Startup copies and persistent local
    outputs occupy a unique tool-owned directory per invocation, labelled
    privately (`:Z`).
    """
    resources = config.local_resources
    dataset_mounts = tuple(d.mount for d in resources.datasets) if resources else ()
    mounts = (config.source, *config.inputs, *dataset_mounts)
    connection_values = resources.secret_values() if resources else ()
    redactor = Redactor(credentials.token, connection_values)
    if resources:
        print(
            redactor.text(
                "Local resource overrides: " + json.dumps(resources.provenance())
            ),
            file=sys.stderr,
        )
        print(
            "Selected dataset directories are LIVE WRITABLE mounts. Unmatched resources and unrelated remote writes remain connected.",
            file=sys.stderr,
        )
    for mount in mounts:
        if mount.relabel:
            print(
                redactor.text(
                    f"SELinux label {mount.label_mode} (shared) on {mount.path}; "
                    "the first run over a large directory can take a while"
                ),
                file=sys.stderr,
            )
    if config.podman[0] == "distrobox-host-exec":
        for mount in mounts:
            try:
                subprocess.run(
                    ["distrobox-host-exec", "test", "-d", str(mount.path)],
                    check=True,
                    capture_output=True,
                    timeout=10,
                )
            except subprocess.CalledProcessError:
                raise SetupError(
                    f"Selected mount directory is not visible on the host: {mount.path}; "
                    "no container started. Use a path under your home directory, or "
                    'or set "podman": "direct" in hexa.json to use Podman inside this container.'
                ) from None
    try:
        subprocess.run(
            [*config.podman, "image", "inspect", config.image],
            check=True,
            capture_output=True,
            timeout=30,
        )
    except subprocess.CalledProcessError:
        raise SetupError(
            "The runtime image is not in the host Podman cache, and hexa does not "
            f"pull images. Pull it with: podman pull {config.image}"
        ) from None
    run_id = uuid.uuid4().hex
    directory = config.project_dir / ".hexa" / "runs" / run_id
    directory.mkdir(parents=True, mode=0o700)
    startup = directory / "startup"
    shutil.copytree(
        PACKAGE_DIR / "runtime", startup, ignore=shutil.ignore_patterns("__pycache__")
    )
    output = directory / "output"
    temporary = directory / "tmp"
    record = directory / "record"
    output.mkdir()
    temporary.mkdir()
    record.mkdir()
    name = "hexa-" + run_id
    command = [
        *config.podman,
        "run",
        "--name",
        name,
        "--rm",
        "-i",
        "--pull=never",
        "--log-driver=none",
        "--userns=keep-id",
        f"--cpus={CPUS}",
        f"--memory={MEMORY}",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--workdir=/source",
    ]
    for key, value in container_environment(
        credentials.origin, credentials.workspace
    ).items():
        command.extend(["--env", f"{key}={value}"])
    owned = {
        "/startup": startup,
        "/output": output,
        "/scratch": temporary,
        "/record": record,
    }
    for target, local, mode in container_mounts(config, owned):
        command.extend(["--volume", f"{local}:{target}:{mode}"])
    command.extend(
        [
            "--entrypoint=/opt/conda/bin/python",
            config.image,
            "/startup/bootstrap.py",
            "/source/" + config.entrypoint,
        ]
    )
    print(redactor.text(f"Local run {run_id}; outputs: {output}"), file=sys.stderr)
    log_file = directory / "log.txt"
    record_file = directory / "run.json"
    started = datetime.now(UTC)
    clock = time.monotonic()
    with (
        log_file.open("w", encoding="utf-8") as log,
        subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        ) as process,
    ):
        assert process.stdin is not None and process.stdout is not None
        try:
            try:
                process.stdin.write(
                    json.dumps(
                        {
                            "token": credentials.token,
                            "parameters": config.parameters,
                            **(
                                {
                                    "local_resources": {
                                        "datasets": resources.manifest(),
                                        "connections": resources.connections,
                                    }
                                }
                                if resources
                                else {}
                            ),
                        }
                    )
                    + "\n"
                )
                process.stdin.close()
            except BrokenPipeError:
                # Still drain safe startup diagnostics and preserve the actual container status.
                process.stdin = None
            diagnostics = _Diagnostics(sys.stderr, log)
            redactor.stream(process.stdout, diagnostics)
            status = process.wait()
        except (KeyboardInterrupt, SystemExit):
            subprocess.run(
                [*config.podman, "stop", "--time=3", name],
                check=True,
                capture_output=True,
                timeout=30,
            )
            raise
        if status < 0:
            status = 128 - status
        writes = redactor.value(_read_ledger(record / "writes.jsonl"))
        if (record / "writes.jsonl").exists():
            (record / "writes.jsonl").write_text(
                "".join(json.dumps(entry) + "\n" for entry in writes)
            )
        closing = [summarise_writes(writes), f"Log: {log_file}"]
        hint = startup_hint(config, status, diagnostics.lines)
        if hint is not None:
            closing.insert(0, "Hint: " + hint)
        for line in map(redactor.text, closing):
            print(line, file=sys.stderr)
            log.write(line + "\n")
    ended = datetime.now(UTC)
    record_file.write_text(
        json.dumps(
            redactor.value(
                {
                    "run_id": run_id,
                    "source": {
                        "path": str(config.source.path),
                        "sha256": source_digest(config.source.path),
                    },
                    "entrypoint": config.entrypoint,
                    "parameters": _recorded_parameters(config),
                    "image": config.image,
                    "started_at": started.isoformat(),
                    "ended_at": ended.isoformat(),
                    "duration_seconds": round(time.monotonic() - clock, 3),
                    "exit_code": status,
                    "remote_writes": writes,
                    **(
                        {"local_resources": resources.provenance()} if resources else {}
                    ),
                }
            ),
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    result: dict[str, Any] = {
        "exit_code": status,
        "run_id": run_id,
        "output_directory": str(output),
        "image": config.image,
        "log_file": str(log_file),
        "record_file": str(record_file),
        "remote_writes": writes,
    }
    if hint is not None:
        result["hint"] = hint
    return redactor.value(result)
