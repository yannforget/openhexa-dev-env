"""Explicit host-side local inputs, private connection parsing and mount preflight."""

import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .config import Mount, SetupError, load_mount
from .runtime.resource_manifest import (
    connection_summary,
    natural_key,
    normalize,
    valid_relative,
    valid_version,
    validate_connections,
)

if TYPE_CHECKING:
    from .config import Config


@dataclass(frozen=True)
class LocalDataset:
    """One selected live, writable dataset directory."""

    slug: str
    mount: Mount
    versions: tuple[dict, ...]


@dataclass(frozen=True, repr=False)
class LocalResources:
    """Keep private fields separate from the public run manifest."""

    datasets: tuple[LocalDataset, ...] = ()
    connections: dict[str, str] = field(default_factory=dict, repr=False)

    def manifest(self) -> list[dict]:
        """Return container paths, never host credential locations."""
        return [
            {
                "slug": item.slug,
                "root": f"/local-datasets/{index}",
                "versions": list(item.versions),
            }
            for index, item in enumerate(self.datasets)
        ]

    def secret_values(self) -> tuple[str, ...]:
        """Treat every field value as private, but not the declared connection type."""
        roots = {item["identifier"] for item in connection_summary(self.connections)}
        return tuple(
            value for key, value in self.connections.items() if key not in roots
        )

    def provenance(self) -> dict:
        """Describe selections without connection fields or capability URLs."""
        return {
            "datasets": [
                {
                    "slug": item.slug,
                    "path": str(item.mount.path),
                    "versions": list(item.versions),
                }
                for item in self.datasets
            ],
            "connections": connection_summary(self.connections),
            "live_writable_mounts": bool(self.datasets),
        }


def load_connections(path: Path) -> dict[str, str]:
    """Read single-line literal assignments; quotes never enable interpolation or escapes."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise SetupError("Cannot read selected connections file") from error
    values: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"\s*([A-Za-z][A-Za-z0-9_-]*)\s*=\s*(.*?)\s*", line)
        if not match:
            raise SetupError(f"Invalid connection assignment at line {number}")
        key, value = match.groups()
        key = normalize(key)
        if key in values:
            raise SetupError(f"Duplicate normalized connection key at line {number}")
        if value.startswith(("'", '"')):
            quote = value[0]
            end = value.find(quote, 1)
            if end < 0 or (
                value[end + 1 :].strip()
                and not value[end + 1 :].lstrip().startswith("#")
            ):
                raise SetupError(f"Invalid connection quoting at line {number}")
            value = value[1:end]
        else:
            value = value.split(" #", 1)[0].rstrip()
            if any(c.isspace() or c in "'\"" for c in value):
                raise SetupError(f"Invalid connection literal at line {number}")
        if not value or "\x00" in value:
            raise SetupError(f"Empty or invalid connection value at line {number}")
        values[key] = value
    try:
        return validate_connections(values)
    except ValueError as error:
        raise SetupError(str(error)) from None


def _private_mounts(mounts: tuple[Mount, ...], private: tuple[Path, ...]) -> None:
    for mount in mounts:
        if any(path.is_relative_to(mount.path) for path in private):
            raise SetupError(
                "Selected mount includes a private credential or connections file"
            )
        for path in mount.path.rglob("*"):
            if path.is_file() and any(path.samefile(secret) for secret in private):
                raise SetupError("Selected mount contains an alias of a private file")


def load_resources(
    config: "Config", assignments: tuple[str, ...], connections_file: Path | None
) -> LocalResources:
    """Resolve flags from the caller, without discovery, API access or service starts."""
    private = (config.credential_file,)
    connections: dict[str, str] = {}
    if connections_file is not None:
        selected = connections_file.resolve(strict=True)
        private += (selected,)
        _private_mounts((config.source, *config.inputs), private)
        connections = load_connections(selected)
    datasets: list[LocalDataset] = []
    seen: set[str] = set()
    for assignment in assignments:
        slug, separator, directory = assignment.partition("=")
        if (
            not separator
            or not directory
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug)
        ):
            raise SetupError("Use --local-dataset SLUG=DATASET_DIR with a dataset slug")
        if slug in seen:
            raise SetupError("Duplicate local dataset slug")
        seen.add(slug)
        selected = Path(directory).absolute()
        if any(path.is_symlink() for path in (selected, *selected.parents)):
            raise SetupError("Local dataset paths cannot contain symlinks")
        mount = load_mount(
            {"path": directory, "relabel": config.source.relabel},
            Path.cwd(),
            config.credential_file,
            "dataset",
        )
        if mount.path.name == ".hexa":
            raise SetupError(
                "Select a dataset directory, not the entire .hexa tooling directory"
            )
        versions = sorted(mount.path.iterdir(), key=lambda path: natural_key(path.name))
        if not versions or any(
            not path.is_dir() or not valid_version(path.name) for path in versions
        ):
            raise SetupError(
                "A local dataset must contain version subdirectories only, and at least one version"
            )
        if not os.access(mount.path, os.W_OK):
            raise SetupError("Local dataset directory must be writable")
        for path in (mount.path, *sorted(mount.path.rglob("*"))):
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise SetupError(
                    "Local datasets must contain only regular files and directories; no symlinks"
                )
            if stat.S_ISREG(mode) and path.stat().st_nlink != 1:
                raise SetupError("Local datasets cannot contain hard-linked files")
            if not os.access(path, os.R_OK) or (
                stat.S_ISDIR(mode) and not os.access(path, os.X_OK | os.W_OK)
            ):
                raise SetupError(
                    "Local dataset entry is unreadable or a directory is not writable"
                )
            if (
                not valid_relative(path.relative_to(mount.path).as_posix())
                and path != mount.path
            ):
                raise SetupError("Unsupported local dataset file path")
        datasets.append(
            LocalDataset(
                slug,
                mount,
                tuple(
                    {
                        "name": version.name,
                        "files": sorted(
                            path.relative_to(version).as_posix()
                            for path in version.rglob("*")
                            if path.is_file()
                        ),
                    }
                    for version in versions
                ),
            )
        )
    _private_mounts(
        (config.source, *config.inputs, *(d.mount for d in datasets)), private
    )
    return LocalResources(tuple(datasets), connections)
