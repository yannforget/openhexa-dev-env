"""Select the files under a directory that may be published, refusing credentials."""

import fnmatch
import stat
from dataclasses import dataclass
from pathlib import Path

from .config import SetupError

# Directory names skipped entirely: tooling state, not content.
SKIPPED_DIRECTORIES = frozenset(
    {".git", "__pycache__", ".venv", "node_modules", ".hexa"}
)
# Bytes kept between chunks so a token split across two reads is still found.
_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class SelectedFile:
    """A regular file and its POSIX path relative to the selected directory."""

    path: Path
    relative: str
    size: int


def _contains(path: Path, needle: bytes) -> bool:
    """Search a file for `needle` in bounded memory."""
    overlap = b""
    with path.open("rb") as source:
        while chunk := source.read(_CHUNK):
            if needle in overlap + chunk:
                return True
            overlap = chunk[-(len(needle) - 1) :] if len(needle) > 1 else b""
    return False


def _ignored(relative: Path, patterns: tuple[str, ...]) -> bool:
    """Whether a glob pattern matches the relative path or one of its directories."""
    prefixes = [
        Path(*relative.parts[: i + 1]).as_posix() for i in range(len(relative.parts))
    ]
    return any(
        fnmatch.fnmatchcase(prefix, pattern.rstrip("/"))
        for pattern in patterns
        for prefix in prefixes
    )


def select_files(
    directory: Path,
    *,
    token: str,
    credential_file: Path | None,
    ignore: tuple[str, ...] = (),
) -> list[SelectedFile]:
    """Return every publishable regular file under `directory`, sorted by relative path.

    `.git`, `__pycache__`, `.venv`, `node_modules` and `.hexa` are skipped, as is any
    path matched by an `ignore` glob (on the relative path or one of its parent
    directories). Dotenv files, the selected credential file, files containing the
    API token and symlinks leaving the directory are refused, since publishing them
    would leak secrets.

    Raises:
        SetupError: The directory is not a regular directory, is empty, or contains
            a refused file.
    """
    if directory.is_symlink() or not directory.is_dir():
        raise SetupError(f"Select a regular directory, not a symlink: {directory}")
    root = directory.resolve(strict=True)
    credential = credential_file.stat() if credential_file is not None else None
    selected = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if SKIPPED_DIRECTORIES & set(relative.parts) or _ignored(relative, ignore):
            continue
        name = relative.as_posix()
        if path.is_symlink():
            if not path.resolve().is_relative_to(root):
                raise SetupError(f"Symlink leaves the selected directory: {name}")
            continue
        if any(part.lower().startswith(".env") for part in relative.parts):
            raise SetupError(f"Refusing to publish a dotenv file: {name}")
        mode = path.lstat()
        if not stat.S_ISREG(mode.st_mode):
            continue
        if credential is not None and (mode.st_dev, mode.st_ino) == (
            credential.st_dev,
            credential.st_ino,
        ):
            raise SetupError(f"Refusing to publish the credential file: {name}")
        if token and (token in name or _contains(path, token.encode())):
            raise SetupError(f"API token found in file; refusing to publish: {name}")
        selected.append(SelectedFile(path, name, mode.st_size))
    if not selected:
        raise SetupError(f"No files to publish under {directory}")
    return selected
