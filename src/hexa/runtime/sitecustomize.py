"""Fail-closed, development-only SDK local reporting startup."""

import importlib
import json
import logging
import os
import re
import sys
from importlib.metadata import version
from typing import Any

# SDK version the adapter was written against; others run with a notice, since
# the shape check on `CurrentRun._connected` below is what actually guards the patch.
WRITTEN_FOR_SDK = "2.23.0"
adapter_ready = False
# Mutations that only prepare a transfer and change nothing visible on their own.
NON_PUBLISHING = frozenset({"prepareVersionFileDownload", "generateDatasetUploadUrl"})
# Result objects whose identifying fields are recorded.
RECORDED_OBJECTS = ("dataset", "version", "file", "pipeline")


def mutation_field(body: object) -> str | None:
    """Return the top-level field of a GraphQL mutation request body, if it is one."""
    if not isinstance(body, dict) or not isinstance(body.get("query"), str):
        return None
    match = re.match(r"\s*mutation\b[^{]*\{\s*(\w+)", body["query"])
    return match[1] if match else None


def ledger_entry(field: str, data: object) -> dict:
    """Summarise one mutation result as its success flag and object identities only."""
    result = data.get(field) if isinstance(data, dict) else None
    entry: dict = {"operation": field, "success": None}
    if isinstance(result, dict):
        entry["success"] = result.get("success")
        for name in RECORDED_OBJECTS:
            item = result.get(name)
            if isinstance(item, dict):
                entry[name] = {
                    key: item[key]
                    for key in ("id", "slug", "name")
                    if isinstance(item.get(key), str)
                }
    return entry


def _safe_ledger(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _safe_ledger(item) for key, item in value.items()}
    if isinstance(value, str):
        keys = json.loads(os.environ.get("HEXA_LOCAL_CONNECTION_KEYS", "[]"))
        secrets = [
            os.environ.get("HEXA_TOKEN", ""),
            *(
                os.environ.get(key, "")
                for key in keys
                if any(key.startswith(root + "_") for root in keys if root != key)
            ),
        ]
        for secret in list(secrets):
            if secret.lstrip().startswith("{"):
                try:
                    parsed = json.loads(secret)
                except ValueError:
                    continue
                if isinstance(parsed, dict):
                    secrets.extend(
                        item for item in parsed.values() if isinstance(item, str)
                    )
        secrets.extend(line for secret in list(secrets) for line in secret.splitlines())
        for secret in sorted(set(secrets), key=len, reverse=True):
            if secret:
                value = value.replace(secret, "[withheld]")
        value = re.sub(
            r"/[A-Za-z0-9_-]{43}(?=/|[\s'\"<>]|$)(?:/[^\s'\"<>]*)?",
            "[LOCAL URL withheld]",
            value,
        )
        return re.sub(
            r"https?://[^\s\"<>]+", "[URL withheld]", value, flags=re.IGNORECASE
        )
    return value


def record_write(field: str, response: Any) -> None:
    """Append one ledger line to the tool-owned record mount, if the launcher set one."""
    directory = os.environ.get("HEXA_RECORD_DIR")
    if not directory or field in NON_PUBLISHING:
        return
    try:
        data = response.json().get("data")
    except ValueError:
        data = None
    # One short append per line keeps concurrent task processes from interleaving.
    with open(os.path.join(directory, "writes.jsonl"), "a", encoding="utf-8") as ledger:
        ledger.write(json.dumps(_safe_ledger(ledger_entry(field, data))) + "\n")


class AdapterRefused(RuntimeError):
    """A deliberate, explained refusal to start."""


if os.environ.get("HEXA_CONNECTED_LOCAL") == "1":
    try:
        if os.environ.get("HEXA_ENVIRONMENT") != "LOCAL_PIPELINE":
            raise AdapterRefused("connected-local startup requires LOCAL_PIPELINE")
        found = version("openhexa.sdk")
        if found != WRITTEN_FOR_SDK:
            print(
                f"hexa: image has openhexa.sdk {found}; the local reporting adapter "
                f"was written against {WRITTEN_FOR_SDK}",
                file=sys.stderr,
            )

        import requests
        from openhexa.sdk.pipelines.run import CurrentRun
        from openhexa.sdk.utils import Environment, get_environment

        original_property = CurrentRun.__dict__["_connected"]
        if (
            not isinstance(original_property, property)
            or original_property.fget is None
        ):
            raise AdapterRefused(
                f"openhexa.sdk {found}: CurrentRun._connected is not a property"
            )
        original_connected = original_property.fget

        def connected(self: CurrentRun) -> bool:
            """Suppress reporting only in this launcher's local mode."""
            if (
                os.environ.get("HEXA_CONNECTED_LOCAL") == "1"
                and get_environment() == Environment.LOCAL_PIPELINE
            ):
                return False
            return original_connected(self)

        CurrentRun._connected = property(connected)

        # SDK 2.23.0's requests-based reads otherwise have no timeout and follow redirects.
        original_request = requests.Session.request

        def request(self: requests.Session, method, url, *args, **kwargs):
            """Keep SDK HTTP requests bounded, verified and redirect-free.

            GraphQL mutations are also recorded in the run's write ledger.
            """
            kwargs["allow_redirects"] = False
            kwargs["verify"] = True
            if kwargs.get("timeout") is None:
                kwargs["timeout"] = 60
            response = original_request(self, method, url, *args, **kwargs)
            if 300 <= response.status_code < 400:
                response.close()
                raise RuntimeError("HTTP redirect refused by connected-local startup")
            field = (
                mutation_field(kwargs.get("json"))
                if str(method).upper() == "POST"
                and str(url).rstrip("/").endswith("/graphql")
                else None
            )
            if field is not None:
                record_write(field, response)
            return response

        requests.Session.request = request
        if "HEXA_LOCAL_CONNECTION_KEYS" in os.environ:
            importlib.import_module("local_connections").restore()
        if "HEXA_LOCAL_DATASETS" in os.environ:
            importlib.import_module("local_datasets").restore()
        adapter_ready = True
    except AdapterRefused as error:
        print(f"hexa: startup adapter refused: {error}", file=sys.stderr)
        os._exit(78)
    except Exception:
        logging.getLogger(__name__).exception(
            "Connected-local startup failed; execution refused"
        )
        os._exit(78)
