"""Bounded verified-HTTPS transport, independent of CLI and local execution."""

import json
import os
from http.client import HTTPException
from typing import Any, BinaryIO
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ..config import Credentials


class APIError(RuntimeError):
    """A safe diagnostic with a stable machine-readable category."""

    def __init__(
        self,
        message: str,
        code: str = "API_FAILURE",
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.details = details


class NoRedirect(HTTPRedirectHandler):
    """Never forward API authorization or signed URLs through redirects."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise APIError("HTTP redirect refused")


class Transport:
    """Explicitly injected credentials; no prompts, retries or credential discovery."""

    def __init__(
        self, credentials: Credentials, response_limit: int = 16 * 1024 * 1024
    ):
        if not 1 <= response_limit <= 64 * 1024 * 1024:
            raise APIError(
                "response_limit must be between 1 byte and 64 MiB", "INVALID_INPUT"
            )
        origin = urlsplit(credentials.origin)
        if (
            origin.scheme != "https"
            or not origin.hostname
            or origin.username
            or origin.password
            or origin.path
            or origin.query
            or origin.fragment
        ):
            raise APIError(
                "Client requires a verified HTTPS API origin", "INVALID_INPUT"
            )
        self.credentials = credentials
        self.response_limit = response_limit
        self.opener = build_opener(NoRedirect())

    def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """POST a bounded GraphQL response, withholding remote error bodies."""
        request = Request(
            self.credentials.origin + "/graphql/",
            data=self.graphql_body(query, variables),
            headers={
                "Authorization": "Bearer " + self.credentials.token,
                "Content-Type": "application/json",
            },
        )
        try:
            with self.opener.open(request, timeout=60) as response:
                raw = response.read(self.response_limit + 1)
        except HTTPError as error:
            if error.code in (401, 403):
                raise APIError(
                    "API authentication/permission rejected", "PERMISSION_DENIED"
                ) from None
            if error.code != 400:
                raise APIError(
                    "API HTTP request rejected; check target and schema"
                ) from None
            with error:
                raw = error.read(self.response_limit + 1)
        except (URLError, TimeoutError, OSError, HTTPException):
            raise APIError("API network/TLS request failed") from None
        if len(raw) > self.response_limit:
            raise APIError(
                "API response exceeds the client byte limit", "RESPONSE_TOO_LARGE"
            )
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeError):
            raise APIError("API returned invalid JSON") from None
        if isinstance(body, dict) and body.get("errors"):
            errors = body["errors"]
            if any(
                any(
                    marker in str(e.get("message", ""))
                    for marker in (
                        "Cannot query field",
                        "Unknown type",
                        "Unknown argument",
                    )
                )
                for e in errors
                if isinstance(e, dict)
            ):
                raise APIError(
                    "Requested capability is not exposed by this API schema",
                    "UNSUPPORTED_CAPABILITY",
                )
        if (
            not isinstance(body, dict)
            or body.get("errors")
            or not isinstance(body.get("data"), dict)
        ):
            raise APIError(
                "GraphQL request rejected or returned no data; permission/schema details withheld"
            )
        return body["data"]

    @staticmethod
    def graphql_body(query: str, variables: dict[str, Any]) -> bytes:
        """Serialize request data consistently for transport and upload budgets."""
        return json.dumps({"query": query, "variables": variables}).encode()

    @staticmethod
    def mutation_query(field: str, input_type: str, selection: str = "") -> str:
        """Build the single-dispatch mutation used by `mutate`."""
        return f"mutation Change($input:{input_type}!){{{field}(input:$input){{success errors {selection}}}}}"

    def mutate(
        self, field: str, input_type: str, values: dict[str, Any], selection: str = ""
    ) -> dict[str, Any]:
        """Dispatch once; failures may have changed state and must not be retried blindly."""
        target = {
            k: v
            for k, v in values.items()
            if k
            in {
                "id",
                "datasetId",
                "versionId",
                "slug",
                "workspaceSlug",
                "objectKey",
                "targetId",
            }
        }
        details = {"operation": field, "target": target}
        try:
            result = self.graphql(
                self.mutation_query(field, input_type, selection),
                {"input": values},
            ).get(field)
        except APIError as error:
            if error.code in {"UNSUPPORTED_CAPABILITY", "PERMISSION_DENIED"}:
                raise
            raise APIError(
                "Write outcome uncertain; inspect the exact target before retrying",
                "UNCERTAIN_WRITE",
                details,
            ) from error
        if not isinstance(result, dict):
            raise APIError(
                "Mutation returned no result; inspect target before retrying",
                "UNCERTAIN_WRITE",
                details,
            )
        if not result.get("success") or result.get("errors"):
            errors = result.get("errors") or []
            codes = (
                ", ".join(str(e) for e in errors) if isinstance(errors, list) else ""
            )
            raise APIError(
                f"Mutation rejected{f' ({codes})' if codes else ''}; no automatic retry attempted",
                "WRITE_REJECTED",
                {
                    **details,
                    **{
                        key: result[key]
                        for key in ("errors", "details")
                        if result.get(key)
                    },
                },
            )
        return result

    @staticmethod
    def validate_storage_url(url: str) -> None:
        """Require HTTPS, even for explicit URL delivery to another client."""
        try:
            parsed = urlsplit(url)
            invalid = (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.fragment
                or parsed.port == 0
            )
        except ValueError:
            raise APIError(
                "Invalid storage URL; URL withheld", "INVALID_INPUT"
            ) from None
        if invalid:
            raise APIError(
                "Storage URL must be HTTPS without embedded authorization",
                "INVALID_INPUT",
            )

    def storage_request(
        self,
        url: str,
        data: bytes | BinaryIO | None = None,
        headers: dict[str, str] | None = None,
    ):
        """Open storage without API authorization, with verified TLS and no redirects."""
        self.validate_storage_url(url)
        if headers and any(
            k.lower() in {"authorization", "cookie", "host"} for k in headers
        ):
            raise APIError("Unsafe storage upload headers rejected", "INVALID_INPUT")
        upload_headers = dict(headers or {})
        if data is not None and not isinstance(data, bytes):
            # Signed providers need a length, not urllib's default chunked transfer.
            upload_headers["Content-Length"] = str(
                os.fstat(data.fileno()).st_size - data.tell()
            )
        request = Request(
            url,
            data=data,
            headers=upload_headers,
            method="GET" if data is None else "PUT",
        )
        try:
            return self.opener.open(request, timeout=60)
        except HTTPError as error:
            code = "API_FAILURE"
            if data is not None:
                code = (
                    "STORAGE_REJECTED"
                    if 400 <= error.code < 500 and error.code != 408
                    else "UNCERTAIN_WRITE"
                )
            raise APIError("Storage HTTP request failed; URL withheld", code) from None
        except (URLError, TimeoutError, OSError, HTTPException):
            raise APIError(
                "Storage network/TLS request failed; URL withheld",
                "UNCERTAIN_WRITE" if data is not None else "API_FAILURE",
            ) from None
