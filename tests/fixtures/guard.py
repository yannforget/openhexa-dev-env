"""Integration-only fail-closed operation guard; never used by the launcher."""

import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import requests

ALLOWED = {
    "getConnection",
    "getDataset",
    "getDatasetVersions",
    "getDatasetFile",
    "getDownloadUrl",
}


def guard(method, url, kwargs):
    """Record only operation names and PIDs, and reject unexpected operations."""
    target = urlsplit(os.environ["HEXA_SERVER_URL"])
    parsed = urlsplit(str(url))
    if (parsed.scheme, parsed.netloc, parsed.path) == (
        target.scheme,
        target.netloc,
        "/graphql/",
    ):
        payload = kwargs.get("json")
        if payload is None:
            payload = json.loads(kwargs.get("content", "{}"))
        match = re.search(r"\b(query|mutation)\s+(\w+)", payload.get("query", ""))
        operation = match[2] if match else "unrecognized"
        permitted = (
            match is not None
            and operation in ALLOWED
            and (match[1] == "query" or operation == "getDownloadUrl")
        )
    else:
        operation = "STORAGE_GET"
        headers = kwargs.get("headers") or {}
        permitted = (
            method.upper() == "GET"
            and parsed.scheme == "https"
            and not any(key.lower() == "authorization" for key in headers)
        )
    descriptor = os.open(
        Path(os.environ["WORKSPACE_FILES_PATH"]) / "operations.jsonl",
        os.O_WRONLY | os.O_CREAT | os.O_APPEND,
        0o600,
    )
    try:
        os.write(
            descriptor,
            (
                json.dumps(
                    {"pid": os.getpid(), "operation": operation, "allowed": permitted}
                )
                + "\n"
            ).encode(),
        )
    finally:
        os.close(descriptor)
    if not permitted:
        raise RuntimeError("Integration guard blocked an unexpected network operation")


original_requests = requests.Session.request


def request(self: requests.Session, *args, **kwargs):
    """Check operation identity before dispatch."""
    method = args[0] if args else kwargs["method"]
    url = args[1] if len(args) > 1 else kwargs["url"]
    guard(method, url, kwargs)
    return original_requests(self, *args, **kwargs)


requests.Session.request = request
original_httpx = httpx.Client.request


def httpx_request(self: httpx.Client, *args, **kwargs):
    """Check the generated client's serialized GraphQL request."""
    method = args[0] if args else kwargs["method"]
    url = args[1] if len(args) > 1 else kwargs["url"]
    guard(method, url, kwargs)
    return original_httpx(self, *args, **kwargs)


httpx.Client.request = httpx_request
