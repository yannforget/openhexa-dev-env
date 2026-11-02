"""Shared invocation state, injected into commands by the root CLI."""

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

import typer

from ..api import API
from ..config import Credentials, Project, load_credentials
from ..output import Result, safe_value

ConfigOption = Annotated[
    Path | None,
    typer.Option(
        "--config", help="Project JSON; default: ./hexa.json, no parent search."
    ),
]
JsonOption = Annotated[
    bool, typer.Option("--json", help="One plain JSON result/error on stdout.")
]
LimitOption = Annotated[
    int,
    typer.Option("--limit", min=1, max=10000, help="Maximum displayed items."),
]
AllOption = Annotated[
    bool,
    typer.Option("--all", help="Read all pages up to the internal safety ceiling."),
]
VersionOption = Annotated[
    str | None,
    typer.Option(
        "--version",
        help="Exact version name/number/UUID; omitted means disclosed latest/current.",
    ),
]
OutputOption = Annotated[
    Path | None,
    typer.Option("--output", help="New local destination; never overwrite."),
]
FileOption = Annotated[
    Path,
    typer.Option("--file", help="UTF-8 document to replace the selected field/object."),
]


@dataclass
class Invocation:
    """One lazily loaded target shared by all command groups."""

    config: Path | None = None
    json_mode: bool = False
    credentials: Credentials | None = field(default=None, repr=False)
    project: Project | None = None

    def load(self) -> Project:
        """Load only project/auth settings, not local execution requirements."""
        if self.project is None:
            self.project = Project.load(self.config)
            self.credentials = load_credentials(self.project.credential_file)
        return self.project

    @property
    def token(self) -> str:
        return self.credentials.token if self.credentials else "\x00not-a-token\x00"

    def api(self) -> API:
        """Build a Python client without loading the SDK or invoking Podman."""
        self.load()
        assert self.credentials is not None
        return API(self.credentials)


def state(ctx: typer.Context, config: Path | None, json_output: bool) -> Invocation:
    """Apply explicit leaf options over root settings to the shared invocation."""
    invocation: Invocation = ctx.find_root().obj
    if config is not None:
        invocation.config = config
    invocation.json_mode |= json_output
    return invocation


def api(ctx: typer.Context, config: Path | None, json_output: bool) -> API:
    """Retrieve the selected command's explicit client."""
    return state(ctx, config, json_output).api()


def announce_write(
    client: API, resource: str, selector: str | None = None
) -> str | None:
    """Disclose source ownership before dispatch and pin the resolved identity."""
    identity = client.write_target(resource, selector)
    # The origin is validated configuration, not a signed URL, and it is what tells
    # instances apart; other values keep diagnostic URL redaction.
    shown = {
        **safe_value(identity, client.credentials.token, redact_urls=True),
        "origin": client.credentials.origin,
    }
    print("Explicit write target: " + json.dumps(shown), file=sys.stderr)
    return None if resource == "workspace" else identity["id"]


def document_result(document: dict, output: Path | None) -> Result:
    """Keep provenance in JSON and document-only payload on stdout."""
    return Result(document, document.get("text") or "", output)


def group(help_text: str) -> typer.Typer:
    """Create a consistent non-writing resource group that shows help when bare."""
    return typer.Typer(
        help=help_text,
        no_args_is_help=True,
        add_completion=False,
        rich_markup_mode=None,
        pretty_exceptions_enable=False,
        context_settings={"help_option_names": ["-h", "--help"]},
    )
