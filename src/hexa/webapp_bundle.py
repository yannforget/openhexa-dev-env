"""Bounded static-directory packaging, without builds or remote file inspection."""

import base64
import json
import stat
from pathlib import Path

from .config import SetupError

PAYLOAD_LIMIT = 5 * 1024 * 1024
_BLOCKED_NAMES = {
    ".git",
    "node_modules",
    "bower_components",
    ".venv",
    "venv",
    "__pycache__",
}


def _raise_walk_error(error: OSError) -> None:
    raise error


def package_directory(
    directory: Path,
    *,
    token: str,
    credential_file: Path | None,
    overhead: int,
) -> tuple[list[dict[str, str]], int]:
    """Read each file once, returning API files and total serialized request bytes.

    Args:
        directory: Explicit static output directory with a nonempty root index.html.
        token: Known API token to exclude from paths and file bytes.
        credential_file: Selected credential file to exclude, including hard links.
        overhead: Serialized request size with an empty files list.
    """
    if directory.is_symlink() or not directory.is_dir():
        raise SetupError("Select a regular static output directory, not a symlink")
    root = directory.resolve(strict=True)
    if root.name in _BLOCKED_NAMES or root.name.lower().startswith(".env"):
        raise SetupError("Selected directory is not a static output directory")
    index = root / "index.html"
    if index.is_symlink() or not index.is_file() or index.stat().st_size == 0:
        raise SetupError("Require a regular, nonempty root index.html")
    credential_stat = credential_file.stat() if credential_file is not None else None
    files: list[dict[str, str]] = []
    size = overhead
    if size > PAYLOAD_LIMIT:
        raise SetupError("Serialized GraphQL payload exceeds the 5 MiB client budget")
    for parent, directories, filenames in root.walk(
        follow_symlinks=False, on_error=_raise_walk_error
    ):
        for name in sorted(directories + filenames):
            path = parent / name
            relative = path.relative_to(root).as_posix()
            mode = path.lstat()
            if (
                path.is_symlink()
                or not (stat.S_ISDIR(mode.st_mode) or stat.S_ISREG(mode.st_mode))
                or not path.resolve(strict=True).is_relative_to(root)
                or "\\" in relative
                or any(ord(char) < 32 for char in relative)
            ):
                raise SetupError(f"Unsafe bundle path: {relative}")
            if name in _BLOCKED_NAMES or name.lower().startswith(".env"):
                raise SetupError(
                    f"Accidental source/credential input rejected: {relative}"
                )
            if token and token in relative:
                raise SetupError("Known API token found in bundle path")
            if stat.S_ISDIR(mode.st_mode):
                continue
            if credential_stat is not None and (mode.st_dev, mode.st_ino) == (
                credential_stat.st_dev,
                credential_stat.st_ino,
            ):
                raise SetupError("Selected credential file cannot be published")
            with path.open("rb") as source:
                raw = source.read(PAYLOAD_LIMIT - size + 1)
            if len(raw) > PAYLOAD_LIMIT - size:
                raise SetupError(
                    "Serialized GraphQL payload exceeds the 5 MiB client budget"
                )
            if relative == "index.html" and not raw:
                raise SetupError("Require a nonempty root index.html")
            if token and token.encode() in raw:
                raise SetupError(f"Known API token found in bundle file: {relative}")
            try:
                content = raw.decode("utf-8")
                encoding = "TEXT"
            except UnicodeDecodeError:
                content = base64.b64encode(raw).decode("ascii")
                encoding = "BASE64"
            item = {"path": relative, "content": content, "encoding": encoding}
            size += len(json.dumps(item).encode()) + (2 if files else 0)
            if size > PAYLOAD_LIMIT:
                raise SetupError(
                    "Serialized GraphQL payload exceeds the 5 MiB client budget (including BASE64/JSON overhead)"
                )
            files.append(item)
    files.sort(key=lambda item: item["path"])
    return files, size
