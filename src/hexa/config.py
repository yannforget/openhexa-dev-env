"""Literal credential parsing and non-secret launcher configuration."""

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

# Version tag and SDK of the pinned digest, as published on Docker Hub.
PINNED_IMAGE_VERSION = "1.18.1 (openhexa.sdk 2.23.0)"
PINNED_IMAGE = "docker.io/blsq/openhexa-blsq-environment@sha256:623fbfea3707f708bc6007cc0485276dc0982585907f9a4edad8807befc63cbe"
PACKAGE_DIR = Path(__file__).resolve().parent


class SetupError(ValueError):
    """An actionable, non-secret setup error."""


PODMAN_MODES = ("auto", "host", "direct")


def in_distrobox() -> bool:
    """Whether hexa runs inside a Distrobox container that can reach host Podman."""
    return (
        Path("/run/.containerenv").exists()
        and shutil.which("distrobox-host-exec") is not None
    )


def podman_command(mode: str) -> tuple[str, ...]:
    """Podman invocation for a `podman` setting; `auto` uses host Podman in Distrobox."""
    if mode not in PODMAN_MODES:
        raise SetupError("podman must be 'auto', 'host' (Distrobox) or 'direct'")
    if mode == "host" or (mode == "auto" and in_distrobox()):
        return ("distrobox-host-exec", "podman")
    return ("podman",)


@dataclass(frozen=True, repr=False)
class Credentials:
    """Credentials loaded exclusively from the selected file."""

    origin: str
    workspace: str
    token: str = field(repr=False)


def load_credentials(path: Path) -> Credentials:
    """Read three literal dotenv settings, without evaluation or fallback.

    Unrelated variable assignments are ignored. OpenHEXA values must be
    optionally quoted single-line literals; quotes do not enable escapes or
    interpolation.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise SetupError(
            f"Credential file not found: {path}\n\n"
            "Create this file with your OpenHEXA workspace credentials:\n\n"
            "  OPENHEXA_URL=<your-openhexa-https-url>\n"
            "  OPENHEXA_WORKSPACE=my-workspace\n"
            "  OPENHEXA_TOKEN=your-workspace-token\n\n"
            "Keep this file private and out of version control.\n"
            "To use an existing credential file, set credential_file in hexa.json."
        ) from error
    except PermissionError as error:
        raise SetupError(
            f"Cannot read credential file: {path}\n\n"
            "Check that you have permission to read this file."
        ) from error
    keys = {"OPENHEXA_URL", "OPENHEXA_WORKSPACE", "OPENHEXA_TOKEN"}
    values: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*", line)
        if not match:
            raise SetupError(f"Unsupported credential assignment at line {number}")
        key, value = match.groups()
        if key not in keys:
            continue
        if key in values:
            raise SetupError(f"Duplicate credential key: {key}")
        if value.startswith(("'", '"')):
            quoted = re.fullmatch(r"(['\"])(.*?)\1\s*(?:#.*)?", value)
            if not quoted:
                raise SetupError(f"Unsupported quoting at line {number}")
            value = quoted[2]
        else:
            value = value.split(" #", 1)[0].rstrip()
            if any(c.isspace() for c in value) or any(c in value for c in "'\"`;"):
                raise SetupError(f"Unsupported literal at line {number}")
        if not value or any(c in value for c in "\\$\x00\r\n"):
            raise SetupError(f"Empty or unsupported literal at line {number}")
        values[key] = value
    missing = keys - values.keys()
    if missing:
        raise SetupError(f"Missing credential keys: {', '.join(sorted(missing))}")
    url = urlsplit(values["OPENHEXA_URL"])
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.path not in ("", "/", "/graphql", "/graphql/")
    ):
        raise SetupError(
            "OPENHEXA_URL must be an HTTPS origin or its /graphql/ endpoint"
        )
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", values["OPENHEXA_WORKSPACE"]):
        raise SetupError("OPENHEXA_WORKSPACE must be a workspace slug")
    return Credentials(
        f"{url.scheme}://{url.netloc}",
        values["OPENHEXA_WORKSPACE"],
        values["OPENHEXA_TOKEN"],
    )


@dataclass(frozen=True)
class Mount:
    """An explicitly selected host directory.

    `relabel` mounts it with a shared SELinux label (`:z`), which Podman ignores
    on hosts without SELinux.
    """

    path: Path
    relabel: bool
    name: str = "source"

    @property
    def label_mode(self) -> str:
        """The SELinux label option shown to users: `:z` or `none`."""
        return ":z" if self.relabel else "none"


@dataclass(frozen=True)
class Project:
    """Non-secret project settings; execution is validated only on demand."""

    path: Path | None
    base: Path
    data: dict[str, Any]
    credential_file: Path

    @classmethod
    def load(cls, path: Path | None = None) -> "Project":
        """Discover only `hexa.json` in the caller's directory, never parents."""
        if path is None and Path("hexa.json").is_file():
            path = Path("hexa.json")
        data = {}
        if path is not None:
            try:
                text = path.read_text(encoding="utf-8")
            except FileNotFoundError as error:
                raise SetupError(
                    f"Project configuration file not found: {path.resolve()}\n\n"
                    "Check the --config path, or omit --config to use ./hexa.json when present."
                ) from error
            except PermissionError as error:
                raise SetupError(
                    f"Cannot read project configuration file: {path.resolve()}\n\n"
                    "Check that you have permission to read this file."
                ) from error
            data = json.loads(text)
        keys = {
            "credential_file",
            "image",
            "source",
            "entrypoint",
            "parameters",
            "inputs",
            "podman",
        }
        if not isinstance(data, dict) or data.keys() - keys:
            unknown = sorted(data.keys() - keys) if isinstance(data, dict) else []
            raise SetupError(
                "Project config must be an object with supported launcher keys"
                + (f"; unknown: {', '.join(unknown)}" if unknown else "")
                + f". Allowed: {', '.join(sorted(keys))}"
            )
        base = Path.cwd() if path is None else path.resolve().parent
        credential = data.get("credential_file")
        if credential is not None and not isinstance(credential, str):
            raise SetupError("credential_file must be a path string")
        return cls(
            path,
            base,
            data,
            (base / (credential or ".env")).resolve(),
        )


@dataclass(frozen=True)
class Config:
    """Launcher settings, independent of pipeline parameters."""

    credential_file: Path
    project_dir: Path
    image: str
    source: Mount
    entrypoint: str
    parameters: dict[str, Any]
    inputs: tuple[Mount, ...]
    podman: tuple[str, ...]

    @classmethod
    def load(cls, path: Path) -> "Config":
        """Read strict JSON; resolve paths relative to the configuration file."""
        return cls.from_project(Project.load(path))

    @classmethod
    def from_project(
        cls,
        project: Project,
        directory: Path | None = None,
        parameter_file: Path | None = None,
        overrides: tuple[str, ...] = (),
    ) -> "Config":
        """Apply tested execution defaults and validate only selected mounts."""
        data: dict[str, Any] = {
            "image": PINNED_IMAGE,
            "podman": "auto",
            "source": {"path": "."},
            "entrypoint": "pipeline.py",
            "parameters": {},
            "inputs": [],
            **project.data,
        }
        base = project.base
        if data["image"] != PINNED_IMAGE:
            raise SetupError("Only the tested pinned runtime digest is supported")
        podman = podman_command(data["podman"])
        if not isinstance(data["parameters"], dict) or not isinstance(
            data["inputs"], list
        ):
            raise SetupError("parameters must be an object; inputs must be a list")
        credential_file = project.credential_file.resolve(strict=True)
        selected, relabel, _ = select_source(project, directory)
        source = load_mount(
            {"path": str(selected), "relabel": relabel},
            base,
            credential_file,
            "source",
        )
        if source.path == PACKAGE_DIR or source.path in PACKAGE_DIR.parents:
            raise SetupError(
                "Select a pipeline-only directory, not the installed package or its parents"
            )
        entrypoint = selected_entrypoint(project, directory).name
        inputs = tuple(
            load_mount(item, base, credential_file, None) for item in data["inputs"]
        )
        if len({item.name for item in inputs}) != len(inputs):
            raise SetupError("Input mount names must be unique")
        return cls(
            credential_file,
            project.base,
            data["image"],
            source,
            entrypoint,
            load_parameters(project, directory, parameter_file, overrides),
            inputs,
            podman,
        )


def select_source(project: Project, directory: Path | None) -> tuple[Path, bool, bool]:
    """Select source without credentials or mount checks.

    Returns the selected path, whether to relabel it and whether it is the
    configured source. Relabelling is the default; an explicit `relabel: false`
    applies to whichever directory is selected.
    """
    source = project.data.get("source", {"path": "."})
    if (
        not isinstance(source, dict)
        or "path" not in source
        or source.keys() - {"path", "relabel"}
        or not isinstance(source["path"], str)
        or not isinstance(source.get("relabel", True), bool)
    ):
        raise SetupError("Source requires path and optional boolean relabel")
    configured = (project.base / source["path"]).resolve()
    selected = configured if directory is None else directory.resolve()
    return selected, source.get("relabel", True), selected == configured


def selected_entrypoint(project: Project, directory: Path | None) -> Path:
    """Resolve the selected entrypoint without loading credentials or execution settings."""
    source, _, _ = select_source(project, directory)
    entrypoint = project.data.get("entrypoint", "pipeline.py")
    if not isinstance(entrypoint, str) or not re.fullmatch(r"[\w-]+\.py", entrypoint):
        raise SetupError("entrypoint must be a Python filename within source")
    path = source / entrypoint
    if not source.is_dir() or not path.is_file():
        raise SetupError("Pipeline directory or entrypoint is missing")
    return path


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SetupError("Duplicate JSON key in parameter inputs")
        result[key] = value
    return result


def _invalid_constant(value: str) -> Any:
    raise SetupError("Parameter inputs must use standard JSON values")


def _parameter_json(text: str) -> Any:
    return json.loads(
        text, object_pairs_hook=_unique_object, parse_constant=_invalid_constant
    )


def load_parameters(
    project: Project,
    directory: Path | None,
    parameter_file: Path | None = None,
    overrides: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Select one explicit input dictionary and apply unique `CODE=JSON` replacements.

    File paths resolve from the caller. A file replaces configured inputs; otherwise
    configured inputs apply only to the configured source. SDK defaults stay omitted.
    """
    _, _, same = select_source(project, directory)
    configured = project.data.get("parameters", {}) if same else {}
    return read_inputs(parameter_file, overrides, configured)


def read_inputs(
    parameter_file: Path | None,
    overrides: tuple[str, ...] = (),
    values: Any = None,
) -> dict[str, Any]:
    """Read a JSON object of inputs, or start from `values`, then apply `CODE=JSON` overrides.

    A file replaces `values`; each override replaces one code.
    """
    if parameter_file is not None:
        values = _parameter_json(parameter_file.read_text(encoding="utf-8"))
    values = {} if values is None else values
    if not isinstance(values, dict):
        raise SetupError("Parameters must be a JSON object")
    values = dict(values)
    seen: set[str] = set()
    for override in overrides:
        code, separator, text = override.partition("=")
        if not separator or not code or code.strip() != code:
            raise SetupError("Use --set CODE=JSON for each parameter replacement")
        if code in seen:
            raise SetupError("Repeated --set parameter code")
        seen.add(code)
        values[code] = _parameter_json(text)
    return values


def load_mount(
    data: dict[str, Any], base: Path, credentials: Path, name: str | None
) -> Mount:
    """Validate one mount entry: `source` when `name` is given, otherwise an input.

    Raises:
        SetupError: The directory is missing, too broad, or contains credentials.
    """
    required = {"path"} if name else {"path", "name"}
    if (
        not isinstance(data, dict)
        or required - data.keys()
        or data.keys() - required - {"relabel"}
        or not isinstance(data.get("relabel", True), bool)
    ):
        raise SetupError(
            "Mounts require path, (for inputs) name, and optional boolean relabel"
        )
    relabel = data.get("relabel", True)
    name = name or data["name"]
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]*", name):
        raise SetupError("Invalid input mount name")
    if not isinstance(data["path"], str):
        raise SetupError("Mount path must be a string")
    path = (base / data["path"]).resolve(strict=True)
    if any(
        path.is_relative_to(p)
        for p in (
            Path("/etc"),
            Path("/usr"),
            Path("/opt"),
            Path("/var/lib"),
            Path("/var/run"),
            Path("/run"),
            Path("/sys"),
            Path("/proc"),
            Path("/dev"),
            Path("/boot"),
            Path("/root"),
        )
    ):
        raise SetupError("System and service directories cannot be selected as mounts")
    protected = (
        Path.home(),
        Path("/etc"),
        Path("/usr"),
        Path("/var"),
        Path("/opt"),
        Path("/srv"),
        Path("/tmp"),
        Path("/var/tmp"),
        PACKAGE_DIR,
    )
    if not path.is_dir():
        raise SetupError(f"Mount is not a directory: {path}")
    if any(path == p or path in p.parents for p in protected):
        raise SetupError(
            f"Refusing to mount {path}: it is a home, system or shared directory. "
            "Select a narrower directory, such as a single pipeline directory."
        )
    if credentials.is_relative_to(path) or any(c in str(path) for c in ":,\n\r"):
        raise SetupError(
            "Mount would include credentials or has unsupported path characters; "
            "select a pipeline subdirectory (hexa run pipelines/NAME)"
        )
    for child in path.rglob("*"):
        if child.name == ".env" or child.name.startswith(".env."):
            raise SetupError(
                "Mount contains a dotenv file; select a pipeline subdirectory "
                "(hexa run pipelines/NAME)"
            )
        if child.is_symlink() and not child.resolve().is_relative_to(path):
            raise SetupError("Mount contains an escaping symlink")
    return Mount(path, relabel, name)
