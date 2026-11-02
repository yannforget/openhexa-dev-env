"""Shared fixtures: isolated installs, JSON shapes and opt-in integration config."""

import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from hexa import config

ROOT = Path(__file__).resolve().parents[1]
INTEGRATION_KEYS = (
    "credential_file",
    "dataset",
    "version",
    "file",
    "connection",
    "pipeline",
)


@pytest.fixture(scope="session")
def integration() -> dict[str, Any]:
    """Read the explicit integration config; skip API tests when none is given."""
    option = os.environ.get("HEXA_INTEGRATION_CONFIG")
    if not option:
        pytest.skip("Set HEXA_INTEGRATION_CONFIG (see tests/integration.example.json)")
    path = Path(option).resolve()
    data = json.loads(path.read_text())
    missing = [key for key in INTEGRATION_KEYS if not data.get(key)]
    assert not missing, f"Integration config {path} lacks {missing}"
    data["credential_file"] = (path.parent / data["credential_file"]).resolve(
        strict=True
    )
    return data


@pytest.fixture(scope="session")
def podman_mode() -> dict[str, str]:
    """hexa.json engine setting for container tests; unset keeps hexa's auto-detection."""
    mode = os.environ.get("HEXA_TEST_PODMAN")
    return {"podman": mode} if mode else {}


@pytest.fixture(scope="session")
def podman_command(podman_mode: dict[str, str]) -> tuple[str, ...]:
    """Inspect containers through the same command `hexa run` uses."""
    return config.podman_command(podman_mode.get("podman", "auto"))


@pytest.fixture(scope="session")
def installed_hexa(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Install a built wheel without changing the user's tools or shell configuration."""
    directory = tmp_path_factory.mktemp("installed-hexa")
    distributions = directory / "dist"
    subprocess.run(
        ["uv", "build", "--offline", "--out-dir", str(distributions)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    wheel = next(distributions.glob("*.whl"))
    subprocess.run(
        ["uv", "tool", "install", "--offline", "--python", sys.executable, str(wheel)],
        env={
            **os.environ,
            "UV_TOOL_DIR": str(directory / "tools"),
            "UV_TOOL_BIN_DIR": str(directory / "bin"),
            "UV_PYTHON_DOWNLOADS": "never",
        },
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return directory / "bin" / "hexa"


def _split(text: str) -> list[str]:
    parts, depth, current = [], 0, ""
    for char in text:
        if char == "," and depth == 0:
            parts.append(current.strip())
            current = ""
            continue
        depth += {"{": 1, "}": -1}.get(char, 0)
        current += char
    return [*parts, current.strip()] if current.strip() else parts


def _paths(shape: str) -> list[str]:
    """Expand `a.{b,c[].d}` notation into dotted paths such as `a.c[].d`."""
    result = []
    for part in _split(shape):
        part = re.sub(r"\s*\(.*\)$", "", part)
        if part.startswith("{") and part.endswith("}"):
            result.extend(_paths(part[1:-1]))
            continue
        head, brace, rest = part.partition("{")
        if brace:
            result.extend(head + item for item in _paths(rest[:-1]))
        else:
            result.append(part)
    return result


def _help_shape(help_text: str, marker: str) -> str:
    """Return the shape after `marker` in help output, ignoring trailing notes."""
    text = " ".join(help_text.split()).split(marker, 1)[1]
    return text.split(";", 1)[0]


def _missing(data: Any, path: str) -> bool:
    """Whether `path` is absent from `data`; empty lists match anything below them."""
    value, *rest = path.split(".", 1)
    many = value.endswith("[]")
    key = value.removesuffix("[]")
    if not isinstance(data, dict) or key not in data:
        return True
    item = data[key]
    if many:
        if not isinstance(item, list):
            return True
        return bool(rest) and any(_missing(entry, rest[0]) for entry in item)
    return bool(rest) and _missing(item, rest[0])


def _check(help_text: str, data: Any, marker: str = "JSON:") -> None:
    """Fail when a path documented after `marker` does not exist in `data`."""
    shape = _help_shape(help_text, marker)
    absent = [path for path in _paths(shape) if _missing(data, path)]
    assert not absent, absent


@pytest.fixture
def json_shape() -> Callable[..., None]:
    """Check the `JSON:` line of command help against a real command result."""
    return _check
