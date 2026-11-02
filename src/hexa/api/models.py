"""Small result contracts used by the Python client and CLI."""

from typing import Any, Literal, NotRequired, TypedDict


class Listing(TypedDict):
    items: list[dict[str, Any]]
    complete: bool
    pages_read: int
    total_items: int | None
    incomplete_reason: NotRequired[Literal["limit", "page_ceiling", "pipeline_ceiling"]]


class Document(TypedDict):
    origin: str
    workspace: str
    source: str
    text: str | None
    present: bool
    truncated: bool
    updated_at: str | None
    identity: dict[str, Any]
