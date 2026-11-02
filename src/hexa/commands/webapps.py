"""Static webapp inspection and deliberately consented immediate publication."""

import sys
from pathlib import Path
from typing import Annotated

import typer

from ..api.resources import WEBAPP_WARNING
from ..config import SetupError
from ..output import Result, safe_value
from .common import (
    AllOption,
    ConfigOption,
    JsonOption,
    LimitOption,
    api,
    group,
    state,
)

webapps = group(
    "Inspect webapps and publish explicit static directories (immediate overlay)."
)
DirectoryOption = Annotated[
    Path,
    typer.Option(
        "--directory", help="Explicit built static directory; never runs a build."
    ),
]
DryRunOption = Annotated[
    bool,
    typer.Option(
        "--dry-run", help="Local validation and read-only target checks; no mutation."
    ),
]
YesOption = Annotated[
    bool,
    typer.Option(
        "--yes",
        help="Approve immediate publication; agents still need operator approval.",
    ),
]


@webapps.command(
    "list",
    epilog="JSON: items[].{id,slug,name,isPublic,allowedOperations,source.publishedVersion}",
)
def webapps_list(
    ctx: typer.Context,
    limit: LimitOption = 20,
    all_items: AllOption = False,
    config: ConfigOption = None,
    json_output: JsonOption = False,
) -> Result:
    """List configured-workspace apps without preview URLs or remote files."""
    return Result(
        api(ctx, config, json_output).webapps(None if all_items else limit),
        listing="webapps",
    )


@webapps.command("show")
def webapp_show(
    ctx: typer.Context,
    app: str,
    config: ConfigOption = None,
    json_output: JsonOption = False,
) -> Result:
    """Inspect an exact UUID, slug or unique name in the configured workspace."""
    return Result(api(ctx, config, json_output).webapp(app))


def _publish(
    ctx: typer.Context,
    selector: str,
    directory: Path,
    scopes: list[str] | None,
    dry_run: bool,
    yes: bool,
    config: Path | None,
    json_output: bool,
) -> Result:
    invocation = state(ctx, config, json_output)
    if dry_run and yes:
        raise SetupError("Choose --dry-run or --yes, not both")
    if not dry_run and not yes:
        raise SetupError(
            "Publication requires --yes; first review with --dry-run. No interactive prompts."
        )
    client = invocation.api()
    project = invocation.load()
    print(safe_value(WEBAPP_WARNING, invocation.token), file=sys.stderr)
    if scopes is not None:
        result = client.create_webapp(
            selector,
            directory,
            tuple(scopes),
            credential_file=project.credential_file,
            dry_run=dry_run,
        )
    else:
        result = client.deploy_webapp(
            selector,
            directory,
            credential_file=project.credential_file,
            dry_run=dry_run,
        )
    return Result(result)


@webapps.command(
    "create",
    epilog="JSON: target, dry_run, paths[], payload_bytes, webapp.{id,slug,source.publishedVersion}",
)
def webapp_create(
    ctx: typer.Context,
    name: str,
    directory: DirectoryOption,
    scope: Annotated[
        list[str] | None,
        typer.Option(
            "--scope",
            help="Repeat: USER_READ, PIPELINES_READ, PIPELINES_RUN (real remote actions), FILES_READ, DATASETS_READ, DATABASE_READ. Default: none.",
        ),
    ] = None,
    dry_run: DryRunOption = False,
    yes: YesOption = False,
    config: ConfigOption = None,
    json_output: JsonOption = False,
) -> Result:
    """Create and immediately publish a private static app with explicit scopes."""
    return _publish(
        ctx, name, directory, scope or [], dry_run, yes, config, json_output
    )


@webapps.command(
    "deploy",
    epilog="JSON: target, dry_run, paths[], payload_bytes, webapp.{id,slug,source.publishedVersion}",
)
def webapp_deploy(
    ctx: typer.Context,
    app: str,
    directory: DirectoryOption,
    dry_run: DryRunOption = False,
    yes: YesOption = False,
    config: ConfigOption = None,
    json_output: JsonOption = False,
) -> Result:
    """Publish content only; overlay paths, retaining remote-only files and settings."""
    return _publish(ctx, app, directory, None, dry_run, yes, config, json_output)
