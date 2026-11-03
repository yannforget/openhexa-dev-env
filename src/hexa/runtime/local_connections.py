"""Resolve selected connections without merging ambient or remote fields."""

import json
import os

from openhexa.sdk.workspaces.current_workspace import CurrentWorkspace
from openhexa.utils import stringcase


def install(values: dict[str, str]) -> None:
    """Use only the private validated assignments for matched identifiers."""
    original = CurrentWorkspace.get_connection_from_env
    roots = [
        key
        for key in values
        if not any(key.startswith(other + "_") for other in values if other != key)
    ]

    def get_connection_from_env(self, identifier: str):
        prefix = stringcase.constcase(identifier.lower())
        if prefix in roots:
            return (
                {
                    key[len(prefix) + 1 :].lower(): value
                    for key, value in values.items()
                    if key.startswith(prefix + "_")
                },
                values[prefix],
            )
        return original(self, identifier)

    CurrentWorkspace.get_connection_from_env = get_connection_from_env


def restore() -> None:
    """Reinstall from private process environment, including in SDK spawn workers."""
    keys = json.loads(os.environ.get("HEXA_LOCAL_CONNECTION_KEYS", "[]"))
    if keys:
        install({key: os.environ[key] for key in keys})
