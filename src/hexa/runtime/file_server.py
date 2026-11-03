"""Run-local loopback HTTP serving for an explicit registry of mounted files."""

import copy
import importlib
import json
import logging
import mimetypes
import os
import re
import secrets
import selectors
import stat
import subprocess
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Any, BinaryIO
from urllib.parse import quote, unquote

manifest = importlib.import_module(
    f"{__package__}.resource_manifest" if __package__ else "resource_manifest"
)


def open_registered(root: str, relative: str) -> BinaryIO:
    """Open without following symlinks at any level, including replaced directories."""
    parts = (*Path(root).parts[1:], *relative.split("/"))
    if not root.startswith("/") or any(p in ("", ".", "..") for p in parts):
        raise OSError("Invalid registered path")
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            os.close(directory)
            directory = child
        descriptor = os.open(
            parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
    finally:
        os.close(directory)
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(descriptor)
        raise OSError("Registered entry is not a regular file")
    return os.fdopen(descriptor, "rb")


@contextmanager
def directory_fd_relative(root: int, parts: list[str], create: bool = False):
    descriptor = os.dup(root)
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, dir_fd=descriptor)
                except FileExistsError:
                    # Opening with O_NOFOLLOW below still rejects existing symlinks.
                    if not stat.S_ISDIR(
                        os.stat(part, dir_fd=descriptor, follow_symlinks=False).st_mode
                    ):
                        raise
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def directory_fd(root: str, relative: str = ""):
    parts = [*Path(root).parts[1:], *([relative] if relative else [])]
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        with directory_fd_relative(descriptor, parts) as directory:
            yield directory
    finally:
        os.close(descriptor)


class RegistryServer(ThreadingMixIn, HTTPServer):
    """Limit concurrent readers and never translate HTTP paths into filesystem paths."""

    daemon_threads = True
    request_queue_size = 8

    def __init__(self, datasets: list[dict]):
        self.registry: dict[str, tuple[str, str]] = {}
        self.datasets = copy.deepcopy(datasets)
        self.lock = threading.RLock()
        self.prefix = secrets.token_urlsafe(32)
        self.slots = threading.BoundedSemaphore(8)
        for index, item in enumerate(self.datasets):
            for version in item["versions"]:
                version["id"] = secrets.token_hex(12)
                version["files"] = [
                    self.register(index, version, path) for path in version["files"]
                ]
        super().__init__(("127.0.0.1", 0), RegistryHandler)

    def register(self, index: int, version: dict, relative: str) -> dict:
        """Register an exact file; IDs and HTTP routes never translate to host paths."""
        identity = secrets.token_hex(12)
        route = (
            f"/{self.prefix}/download/{identity}/{quote(Path(relative).name, safe='')}"
        )
        self.registry[route] = (
            self.datasets[index]["root"],
            version["name"] + "/" + relative,
        )
        return {"id": identity, "uri": relative, "route": route}

    def process_request(self, request, client_address):
        self.slots.acquire()
        try:
            super().process_request(request, client_address)
        except RuntimeError:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        print("hexa: local file server request failed", file=sys.stderr)


class RegistryHandler(BaseHTTPRequestHandler):
    """GET/HEAD with single ranges; invalid and multi-ranges return 416."""

    server: Any

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, format, *args):
        return

    def do_HEAD(self):
        self._serve(False)

    def do_GET(self):
        self._serve(True)

    def _index(self, operation: str) -> int:
        prefix = f"/{self.server.prefix}/{operation}/"
        if not self.path.startswith(prefix):
            raise ValueError("Unknown route")
        value = self.path[len(prefix) :].split("/")[0]
        if not re.fullmatch(r"[0-9]{1,6}", value):
            raise ValueError("Invalid dataset index")
        index = int(value)
        if index >= len(self.server.datasets):
            raise ValueError("Unknown dataset index")
        if operation in ("catalog", "versions") and self.path != prefix + value:
            raise ValueError("Unknown route")
        return index

    def _json(self, value: object, body: bool = True):
        data = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if body:
            self.wfile.write(data)

    def _length(self) -> int:
        value = self.headers.get("Content-Length", "")
        if self.headers.get("Transfer-Encoding") or not re.fullmatch(
            r"[0-9]{1,16}", value
        ):
            raise ValueError("Content length required")
        return int(value)

    def do_POST(self):
        try:
            index = self._index("versions")
            length = self._length()
            if length > 4096:
                raise ValueError("Version request too large")
            payload = json.loads(self.rfile.read(length))
            if (
                not isinstance(payload, dict)
                or set(payload) != {"name"}
                or not manifest.valid_version(payload["name"])
            ):
                raise ValueError("Invalid version name")
            name = payload["name"]
            with self.server.lock:
                dataset = self.server.datasets[index]
                with directory_fd(dataset["root"]) as directory:
                    os.mkdir(name, dir_fd=directory)
                version = {"name": name, "id": secrets.token_hex(12), "files": []}
                dataset["versions"].append(version)
                dataset["versions"].sort(
                    key=lambda item: manifest.natural_key(item["name"])
                )
                self._json(version)
        except FileExistsError:
            self._empty(409)
        except (ValueError, OSError):
            self._empty(400)

    def do_PUT(self):
        try:
            index = self._index("files")
            identity = self.path.rsplit("/", 1)[-1]
            if self.path != f"/{self.server.prefix}/files/{index}/{identity}":
                raise ValueError("Invalid upload route")
            relative = unquote(self.headers.get("X-Hexa-URI", ""), errors="strict")
            if not manifest.valid_relative(relative):
                raise ValueError("Invalid upload filename")
            length = self._length()
            with self.server.lock:
                dataset = self.server.datasets[index]
                version = next(
                    (v for v in dataset["versions"] if v["id"] == identity), None
                )
                if version is None:
                    raise ValueError("Unknown local version")
                with (
                    directory_fd(dataset["root"], version["name"]) as root,
                    directory_fd_relative(
                        root, relative.split("/")[:-1], create=True
                    ) as parent,
                ):
                    name = relative.split("/")[-1]
                    descriptor = os.open(
                        name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=parent,
                    )
                    try:
                        with os.fdopen(descriptor, "wb") as destination:
                            remaining = length
                            while remaining:
                                chunk = self.rfile.read(min(65536, remaining))
                                if not chunk:
                                    raise OSError("Incomplete upload")
                                destination.write(chunk)
                                remaining -= len(chunk)
                    except OSError:
                        os.unlink(name, dir_fd=parent)
                        raise
                file = self.server.register(index, version, relative)
                version["files"].append(file)
                version["files"].sort(key=lambda item: item["uri"])
                self._json(file)
        except FileExistsError:
            self._empty(409)
        except (ValueError, OSError):
            self._empty(400)

    def _empty(self, status: int, size: int | None = None):
        self.send_response(status)
        self.send_header("Content-Length", "0")
        if size is not None:
            self.send_header("Content-Range", f"bytes */{size}")
        self.end_headers()

    def _serve(self, body: bool):
        if self.path.startswith(f"/{self.server.prefix}/catalog/"):
            with self.server.lock:
                try:
                    index = self._index("catalog")
                except ValueError:
                    self._empty(404)
                    return
                self._json(self.server.datasets[index]["versions"], body)
            return
        entry = self.server.registry.get(self.path)
        if entry is None:
            self._empty(404)
            return
        try:
            source = open_registered(*entry)
        except OSError:
            self._empty(404)
            return
        with source:
            size = os.fstat(source.fileno()).st_size
            start, end = 0, size - 1
            requested = self.headers.get("Range")
            if requested is not None:
                match = re.fullmatch(r"bytes=([0-9]{0,20})-([0-9]{0,20})", requested)
                if not match or not any(match.groups()) or size == 0:
                    self._empty(416, size)
                    return
                first, last = match.groups()
                if first:
                    start = int(first)
                    end = min(int(last), size - 1) if last else size - 1
                else:
                    start = max(0, size - int(last))
                if start > end or start >= size:
                    self._empty(416, size)
                    return
            self.send_response(206 if requested else 200)
            self.send_header(
                "Content-Type",
                mimetypes.guess_type(entry[1])[0] or "application/octet-stream",
            )
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Accept-Ranges", "bytes")
            if requested:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if body:
                source.seek(start)
                remaining = end - start + 1
                try:
                    while remaining > 0:
                        chunk = source.read(min(65536, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
                except (TimeoutError, BrokenPipeError, ConnectionResetError):
                    return


@contextmanager
def serving(datasets: list[dict]):
    """Start a credential-free subprocess and await a bounded explicit readiness signal."""
    environment = {
        key: os.environ[key] for key in ("PATH", "LANG") if key in os.environ
    }
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    with subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve())],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env=environment,
    ) as process:
        assert process.stdin is not None and process.stdout is not None
        try:
            try:
                process.stdin.write(json.dumps(datasets) + "\n")
                process.stdin.close()
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    if not selector.select(timeout=10):
                        raise RuntimeError("Local file server readiness timed out")
                ready = json.loads(process.stdout.readline())
                if (
                    not isinstance(ready, dict)
                    or set(ready) != {"endpoint"}
                    or not isinstance(ready["endpoint"], str)
                    or not re.fullmatch(
                        r"http://127\.0\.0\.1:[0-9]+/[A-Za-z0-9_-]+", ready["endpoint"]
                    )
                ):
                    raise RuntimeError("Invalid local file server readiness signal")
            except (BrokenPipeError, ValueError):
                raise RuntimeError("Local file server failed to start") from None
            yield ready["endpoint"]
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def main():
    """Serve only files in the private stdin registry; no URL-bearing access logs."""
    datasets = json.loads(sys.stdin.readline())
    with RegistryServer(datasets) as server:
        port = server.server_address[1]
        print(
            json.dumps({"endpoint": f"http://127.0.0.1:{port}/{server.prefix}"}),
            flush=True,
        )
        server.serve_forever()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logging.getLogger(__name__).exception("hexa: local file server failed")
        sys.exit(1)
