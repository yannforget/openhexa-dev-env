"""Local preflight report for `hexa run`, without starting a container."""

import subprocess
from pathlib import Path
from typing import Any

from .config import (
    PINNED_IMAGE,
    PINNED_IMAGE_VERSION,
    Credentials,
    Project,
    SetupError,
    load_mount,
    podman_command,
    select_source,
    selected_entrypoint,
)
from .runner import CPUS, MEMORY, OWNED_MOUNTS, container_environment

# Where the platform default runtime image and its pinned packages are published.
DEFAULT_IMAGE_REPOSITORY = "blsq/openhexa-blsq-environment"
DEFAULT_IMAGE_TAGS_URL = "https://hub.docker.com/r/blsq/openhexa-blsq-environment/tags"
IMAGE_DOCKERFILE_URL = (
    "https://github.com/BLSQ/openhexa-docker-images/blob/<tag>/images/base/Dockerfile"
)


def _check(name: str, status: str, detail: str, hint: str | None = None) -> dict:
    check = {"name": name, "status": status, "detail": detail}
    if hint:
        check["hint"] = hint
    return check


def _host(bridge: tuple[str, ...], *command: str) -> str | None:
    """Output of a read-only host command, or `None` when it cannot run."""
    try:
        result = subprocess.run(
            [*bridge, *command],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (
        subprocess.CalledProcessError,
        FileNotFoundError,
        subprocess.TimeoutExpired,
    ):
        return None
    return result.stdout.strip()


def local_report(project: Project, directory: Path | None) -> dict[str, Any]:
    """Check what `hexa run` needs locally; credentials are never read.

    Status is `ok`, `missing` or `unchecked`. Only a `missing` required check
    makes the report fail.
    """
    checks = [
        _check(
            "config",
            "ok",
            str(project.path.resolve()) if project.path else "none (defaults)",
        ),
        _check(
            "credential_file",
            "ok" if project.credential_file.is_file() else "missing",
            str(project.credential_file),
            None
            if project.credential_file.is_file()
            else "Create it with OPENHEXA_URL, OPENHEXA_WORKSPACE and OPENHEXA_TOKEN.",
        ),
    ]
    engine = project.data.get("podman", "auto")
    try:
        podman = podman_command(engine)
    except SetupError as error:
        podman = ("podman",)
        checks.append(_check("podman", "missing", str(error)))
    else:
        checks.append(_check("podman", "ok", f"{' '.join(podman)} ({engine})"))
    bridge = podman[:-1]
    mounts: list[dict[str, Any]] = []
    try:
        entrypoint = selected_entrypoint(project, directory)
    except SetupError as error:
        checks.append(_check("source", "missing", str(error)))
    else:
        source, relabel, _ = select_source(project, directory)
        checks.append(
            _check("source", "ok", f"{source} (entrypoint {entrypoint.name})")
        )
        entries: list[tuple[str, dict[str, Any], str | None]] = [
            ("/source", {"path": str(source), "relabel": relabel}, "source")
        ]
        inputs = project.data.get("inputs", [])
        entries.extend(
            ("/inputs/" + str(item.get("name")), item, None)
            for item in (inputs if isinstance(inputs, list) else [])
            if isinstance(item, dict)
        )
        for target, data, name in entries:
            try:
                mount = load_mount(data, project.base, project.credential_file, name)
            except (SetupError, FileNotFoundError) as error:
                checks.append(
                    _check(
                        "mount " + target,
                        "missing",
                        str(error)
                        if isinstance(error, SetupError)
                        else f"not found: {data.get('path')}",
                    )
                )
                continue
            label = _host(bridge, "ls", "-Zd", str(mount.path))
            readable = _host(
                bridge, "test", "-r", str(mount.path), "-a", "-x", str(mount.path)
            )
            mounts.append(
                {
                    "target": target,
                    "host_path": str(mount.path),
                    "mode": "ro" + (",z" if mount.relabel else ""),
                    "label_mode": mount.label_mode,
                    "selinux_label": label.split(" ", 1)[0] if label else None,
                }
            )
            checks.append(
                _check(
                    "mount " + target,
                    "ok" if readable is not None else "missing",
                    f"{mount.path} (label mode {mount.label_mode})",
                    None
                    if readable is not None
                    else "Not readable from the host; check permissions or the podman bridge.",
                )
            )
    selinux = _host(bridge, "getenforce")
    checks.append(
        _check(
            "selinux",
            "ok" if selinux else "unchecked",
            selinux or "getenforce unavailable (no SELinux, or bridge failed)",
        )
    )
    image = project.data.get("image", PINNED_IMAGE)
    cached = _host(podman, "image", "inspect", "--format", "{{.Id}}", image)
    checks.append(
        _check(
            "image",
            "ok" if cached else "missing",
            image,
            None
            if cached
            else f"Not in the host Podman cache. hexa does not pull images; pull it with: podman pull {image}",
        )
    )
    mounts[:0] = [
        {
            "target": target,
            "host_path": str(project.base / ".hexa" / "runs" / "RUN_ID" / name),
            "mode": mode,
        }
        for target, name, mode in OWNED_MOUNTS
    ]
    return {
        "ok": not any(check["status"] == "missing" for check in checks),
        "checks": checks,
        "container": {
            "image": image,
            "image_version": PINNED_IMAGE_VERSION if image == PINNED_IMAGE else None,
            "mounts": mounts,
            "environment": container_environment(
                "<OPENHEXA_URL origin>", "<OPENHEXA_WORKSPACE>"
            ),
            "token": "sent on stdin, never in the environment or arguments",
            "cpus": CPUS,
            "memory": MEMORY,
            "network": "full access",
        },
    }


def hosted_image(credentials: Credentials, workspace: dict[str, Any]) -> dict[str, Any]:
    """Describe the image hosted runs use, from the workspace override when set."""
    configured = workspace.get("dockerImage")
    if configured:
        return {
            "source": "configured on workspace",
            "image": configured,
            "local_image": PINNED_IMAGE,
            "local_image_version": PINNED_IMAGE_VERSION,
        }
    return {
        "source": "platform default",
        "image": f"{DEFAULT_IMAGE_REPOSITORY}:<tag set per instance>",
        "note": (
            "Hosted runs use the instance's DEFAULT_WORKSPACE_IMAGE setting (`.env` on "
            "single-machine installs); hexa cannot read it. "
            "`hexa runs logs RUN --kind runtime` names the image a past run used."
        ),
        "tags_url": DEFAULT_IMAGE_TAGS_URL,
        "dockerfile_url": IMAGE_DOCKERFILE_URL,
        "local_image": PINNED_IMAGE,
        "local_image_version": PINNED_IMAGE_VERSION,
        "server_url": (
            f"Hosted runs get an internal HEXA_SERVER_URL, not {credentials.origin}; "
            "never compare it against a hostname."
        ),
    }
