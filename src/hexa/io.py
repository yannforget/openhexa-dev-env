"""Local payload IO and defensive source archive extraction; never execute source."""

import hashlib
import io
import re
import stat
import zipfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.error import URLError

from .api.client import APIError, Transport
from .config import SetupError

DOCUMENT_LIMIT = 1024 * 1024
ARCHIVE_LIMIT = 10 * 1024 * 1024
EXPANSION_LIMIT = 50 * 1024 * 1024


def read_document(path: Path) -> str:
    """Read bounded UTF-8 text from an explicitly selected local document."""
    with path.open("rb") as source:
        raw = source.read(DOCUMENT_LIMIT + 1)
    if len(raw) > DOCUMENT_LIMIT:
        raise SetupError("Document exceeds the 1 MiB limit")
    return raw.decode("utf-8")


def save_document(path: Path, text: str) -> None:
    """Write a requested document without overwriting an existing destination."""
    with path.open("x", encoding="utf-8") as output:
        output.write(text)


def default_destination(filename: str) -> Path:
    """Derive only a simple safe filename, not a remote directory or basename."""
    if not re.fullmatch(r"[\w.-]+", filename) or filename in {".", ".."}:
        raise SetupError(
            "Remote filename is not a safe local filename; supply --output"
        )
    return Path.cwd() / filename


def combined_sha256(entries: Iterable[tuple[str, bytes]]) -> str:
    """Hash `(relative path, content)` pairs in order, as `run.json` and deploys report."""
    digest = hashlib.sha256()
    for relative, content in entries:
        digest.update(relative.encode() + b"\0")
        digest.update(hashlib.sha256(content).hexdigest().encode() + b"\n")
    return digest.hexdigest()


def file_sha256(path: Path) -> str:
    """Hash a local file in bounded memory."""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def download(
    transport: Transport, url: str, destination: Path, size: int
) -> dict[str, Any]:
    """Stream to an exclusive destination; failures explicitly retain the partial file."""
    count = 0
    digest = hashlib.sha256()
    try:
        with (
            destination.open("xb") as output,
            transport.storage_request(url) as response,
        ):
            while chunk := response.read(1024 * 1024):
                if count + len(chunk) > size:
                    raise APIError(
                        "Download exceeded declared size; partial destination retained"
                    )
                output.write(chunk)
                digest.update(chunk)
                count += len(chunk)
    except FileExistsError:
        raise SetupError("Download destination exists; refusing overwrite") from None
    except (URLError, TimeoutError):
        raise APIError("Storage read failed; partial destination retained") from None
    except APIError as error:
        raise APIError(
            "Storage transfer failed; partial destination retained", error.code
        ) from None
    if count != size:
        raise APIError("Download size mismatch; partial destination retained")
    return {
        "path": str(destination.resolve()),
        "bytes": count,
        "sha256": digest.hexdigest(),
    }


def extract_source(raw: bytes, destination: Path) -> dict[str, Any]:
    """Validate all ZIP entries before extracting privately into a new directory."""
    if len(raw) > ARCHIVE_LIMIT:
        raise SetupError("Source archive exceeds 10 MiB")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            if (
                len(entries) > 2000
                or sum(e.file_size for e in entries) > EXPANSION_LIMIT
            ):
                raise SetupError("Source archive expansion limit exceeded")
            seen: set[str] = set()
            for entry in entries:
                path = PurePosixPath(entry.filename)
                mode = entry.external_attr >> 16
                if (
                    not entry.filename
                    or path.is_absolute()
                    or ".." in path.parts
                    or "\\" in entry.filename
                    or ":" in entry.filename
                    or "\x00" in entry.filename
                    or entry.filename in seen
                    or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR))
                    or entry.flag_bits & 1
                ):
                    raise SetupError(
                        "Unsafe, duplicate or encrypted source archive entry"
                    )
                seen.add(entry.filename)
            destination.mkdir(mode=0o700)
            for entry in entries:
                target = destination / entry.filename
                if entry.is_dir():
                    target.mkdir(parents=True, exist_ok=True, mode=0o700)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    with archive.open(entry) as source, target.open("xb") as output:
                        count = 0
                        while chunk := source.read(1024 * 1024):
                            count += len(chunk)
                            if count > entry.file_size:
                                raise SetupError(
                                    "Source expansion exceeds declared size; partial directory retained"
                                )
                            output.write(chunk)
    except (zipfile.BadZipFile, RuntimeError):
        raise SetupError(
            "Invalid source archive; partial directory may remain"
        ) from None
    return {
        "destination": str(destination.resolve()),
        "archive_sha256": hashlib.sha256(raw).hexdigest(),
        "archive_bytes": len(raw),
        "files": len([e for e in entries if not e.is_dir()]),
        "executed": False,
    }
