"""Strict, SDK-independent validation for private local-resource transport."""

import json
import re
from pathlib import PurePosixPath
from urllib.parse import urlsplit

CONNECTION_FIELDS = {
    "DHIS2": {"URL", "USERNAME", "PASSWORD"},
    "IASO": {"URL", "USERNAME", "PASSWORD"},
    "POSTGRESQL": {"HOST", "PORT", "USERNAME", "PASSWORD", "DB_NAME"},
    "S3": {"ACCESS_KEY_ID", "ACCESS_KEY_SECRET", "BUCKET_NAME"},
    "GCS": {"SERVICE_ACCOUNT_KEY", "BUCKET_NAME"},
    "CUSTOM": set(),
}
RESERVED = {
    "PATH",
    "HOME",
    "USER",
    "SHELL",
    "LANG",
    "TZ",
    "DEBUG",
    "HOSTNAME",
    "ENV",
    "VIRTUAL_ENV",
    "TERM",
}
RESERVED_PREFIXES = (
    "HEXA",
    "OPENHEXA",
    "WORKSPACE",
    "PYTHON",
    "LD_",
    "DYLD_",
    "CONDA",
    "BASH",
    "GIT_",
    "SSL_",
    "REQUESTS_",
    "CURL_",
    "HTTP_",
    "HTTPS_",
    "ALL_PROXY",
    "NO_PROXY",
    "PIP_",
    "UV_",
    "XDG_",
    "SUDO_",
    "LC_",
    "NODE_",
)


def _reserved(key: str) -> bool:
    return key in RESERVED or any(
        key == prefix.rstrip("_") or key.startswith(prefix)
        for prefix in RESERVED_PREFIXES
    )


def normalize(identifier: str) -> str:
    """Match SDK 2.23.0 constcase(identifier.lower()) for supported identifiers."""
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", identifier):
        raise ValueError("Invalid connection identifier")
    return identifier.upper().replace("-", "_")


def validate_connections(values: object) -> dict[str, str]:
    """Accept complete connection definitions only, never arbitrary environment keys."""
    if not isinstance(values, dict) or not all(
        isinstance(k, str)
        and isinstance(v, str)
        and v
        and not any(c in v for c in "\x00\r\n")
        for k, v in values.items()
    ):
        raise ValueError("Invalid connection payload")
    for key in values:
        if normalize(key) != key or _reserved(key):
            raise ValueError(f"Reserved or invalid connection key: {key}")
    remaining = dict(values)
    result: dict[str, str] = {}
    for root in sorted(values, key=lambda key: (len(key), key)):
        if root not in remaining:
            continue
        kind = remaining.pop(root)
        if kind not in CONNECTION_FIELDS:
            raise ValueError(f"Unsupported connection type for {root}")
        fields = {
            key[len(root) + 1 :]: remaining.pop(key)
            for key in list(remaining)
            if key.startswith(root + "_")
        }
        aliases = {
            "POSTGRESQL": ("DATABASE_NAME", "DB_NAME"),
            "S3": ("SECRET_ACCESS_KEY", "ACCESS_KEY_SECRET"),
        }
        if kind in aliases:
            alias, canonical = aliases[kind]
            if alias in fields:
                if canonical in fields:
                    raise ValueError(f"Duplicate field alias for {root}")
                fields[canonical] = fields.pop(alias)
        expected = CONNECTION_FIELDS[kind]
        if kind == "CUSTOM":
            if not fields or any(
                not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or key == "IDENTIFIER"
                for key in fields
            ):
                raise ValueError(f"Invalid custom fields for {root}")
        elif fields.keys() != expected:
            raise ValueError(f"Missing or unknown fields for {root} ({kind})")
        if kind in ("DHIS2", "IASO"):
            try:
                url = urlsplit(fields["URL"])
                valid_url = (
                    url.scheme in ("http", "https")
                    and bool(url.hostname)
                    and not any(c.isspace() for c in fields["URL"])
                    and (url.port is None or 1 <= url.port <= 65535)
                )
            except ValueError:
                valid_url = False
            if not valid_url:
                raise ValueError(f"Invalid URL field for {root}")
        if kind == "GCS":
            try:
                service_account = json.loads(fields["SERVICE_ACCOUNT_KEY"])
            except ValueError:
                raise ValueError(f"Invalid service-account JSON for {root}") from None
            if not isinstance(service_account, dict) or not service_account:
                raise ValueError(
                    f"Service-account JSON must be a nonempty object for {root}"
                )
        if kind == "POSTGRESQL" and (
            len(fields["PORT"]) > 5
            or not fields["PORT"].isascii()
            or not fields["PORT"].isdigit()
            or not 1 <= int(fields["PORT"]) <= 65535
        ):
            raise ValueError(f"Invalid port for {root}")
        result[root] = kind
        result.update({f"{root}_{key}": value for key, value in fields.items()})
    return result


def natural_key(name: str) -> tuple:
    """Order digit runs numerically, with deterministic case-sensitive tie breaks."""
    return (
        tuple(
            (1, int(part)) if part.isdigit() else (0, part.casefold())
            for part in re.split(r"([0-9]+)", name)
        ),
        name,
    )


def valid_relative(path: object) -> bool:
    """Reject absolute paths, traversal, empty components and control characters."""
    return (
        isinstance(path, str)
        and bool(path)
        and not PurePosixPath(path).is_absolute()
        and all(
            part not in ("", ".", "..", ".env") and not part.startswith(".env.")
            for part in path.split("/")
        )
        and not any(
            c == "\\" or ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF
            for c in path
        )
    )


def valid_version(name: object) -> bool:
    """A version name is exactly one safe directory component."""
    return valid_relative(name) and "/" not in str(name)


def validate_datasets(value: object) -> list[dict]:
    """Validate the non-secret mounted dataset registry before installing adapters."""
    if not isinstance(value, list):
        raise TypeError("Invalid dataset manifest")
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict) or set(item) != {"slug", "root", "versions"}:
            raise ValueError("Invalid dataset manifest entry")
        slug, root, versions = (item[k] for k in ("slug", "root", "versions"))
        if (
            not isinstance(slug, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug)
            or slug in seen
            or root != f"/local-datasets/{index}"
            or not isinstance(versions, list)
            or not versions
        ):
            raise ValueError("Invalid dataset manifest fields")
        seen.add(slug)
        names = []
        for version in versions:
            if not isinstance(version, dict) or set(version) != {"name", "files"}:
                raise ValueError("Invalid local version")
            name, files = version["name"], version["files"]
            if (
                not valid_version(name)
                or not isinstance(files, list)
                or not all(valid_relative(p) for p in files)
            ):
                raise ValueError("Invalid local version registry")
            if files != sorted(set(files)):
                raise ValueError("Invalid dataset file registry")
            names.append(name)
        if names != sorted(set(names), key=natural_key):
            raise ValueError("Invalid local version ordering")
    return value


def connection_summary(values: dict[str, str]) -> list[dict[str, str]]:
    """Return identifiers and types only, excluding all connection fields."""
    return [
        {"identifier": key, "type": kind}
        for key, kind in values.items()
        if kind in CONNECTION_FIELDS
        and not any(key.startswith(other + "_") for other in values if other != key)
    ]
