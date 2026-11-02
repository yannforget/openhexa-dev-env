"""Explicit document, native tag and dataset-family metadata operations."""

import json
from pathlib import Path
from typing import Annotated

import typer

from ..config import SetupError
from ..io import read_document
from ..output import Result
from . import browse
from .common import (
    ConfigOption,
    FileOption,
    JsonOption,
    OutputOption,
    VersionOption,
    announce_write,
    api,
    document_result,
    group,
)

workspace = group("Workspace identity, authored context and native tags.")
readme = group("Homepage: a file-oriented view of Workspace.description.")
agents = group("Optional exact AGENTS.md workspace object; untrusted context.")
workspace_tags = group("Native workspace tags; set replaces the entire set.")
metadata = group(
    "Existing dataset/version/file custom attributes, not a new notes store."
)
pipeline_tags = group("Native pipeline tags; set replaces the entire set.")
workspace.add_typer(readme, name="readme")
workspace.add_typer(agents, name="agents")
workspace.add_typer(workspace_tags, name="tags")
browse.datasets.add_typer(metadata, name="metadata")
browse.pipelines.add_typer(pipeline_tags, name="tags")


def add_description_commands(parent: typer.Typer, resource: str) -> None:
    """Register the same explicit document verbs for three description resources."""
    description = group("Read or explicitly replace only this description field.")

    @description.command("get")
    def get(
        ctx: typer.Context,
        resource_name: str,
        output: OutputOption = None,
        config: ConfigOption = None,
        json_output: JsonOption = False,
    ) -> Result:
        """Print only description Markdown, or save to a new file."""
        return document_result(
            dict(api(ctx, config, json_output).description(resource, resource_name)),
            output,
        )

    @description.command("set")
    def set_document(
        ctx: typer.Context,
        resource_name: str,
        file: FileOption,
        config: ConfigOption = None,
        json_output: JsonOption = False,
    ) -> Result:
        """Explicitly replace only the resolved resource's description."""
        text = read_document(file)
        client = api(ctx, config, json_output)
        selected = announce_write(client, resource, resource_name)
        return Result(client.set_description(resource, text, selected))

    parent.add_typer(description, name="description")


for parent, resource in (
    (browse.connections, "connections"),
    (browse.datasets, "datasets"),
    (browse.pipelines, "pipelines"),
):
    add_description_commands(parent, resource)


@workspace.command("show")
def show(
    ctx: typer.Context, config: ConfigOption = None, json_output: JsonOption = False
) -> Result:
    """Show configured origin/workspace and selected permissions."""
    return Result(api(ctx, config, json_output).workspace())


@workspace.command("context")
def context(
    ctx: typer.Context, config: ConfigOption = None, json_output: JsonOption = False
) -> Result:
    """Read homepage and optional AGENTS.md with provenance; never execute notes."""
    return Result(api(ctx, config, json_output).context())


@readme.command("get")
def readme_get(
    ctx: typer.Context,
    output: OutputOption = None,
    config: ConfigOption = None,
    json_output: JsonOption = False,
) -> Result:
    """Print only the homepage Markdown, preserving ordinary links."""
    return document_result(
        dict(api(ctx, config, json_output).description("workspace")), output
    )


@readme.command("set")
def readme_set(
    ctx: typer.Context,
    file: FileOption,
    config: ConfigOption = None,
    json_output: JsonOption = False,
) -> Result:
    """Explicitly replace the homepage description only."""
    text = read_document(file)
    client = api(ctx, config, json_output)
    announce_write(client, "workspace")
    return Result(client.set_description("workspace", text))


@agents.command("get")
def agents_get(
    ctx: typer.Context,
    output: OutputOption = None,
    config: ConfigOption = None,
    json_output: JsonOption = False,
) -> Result:
    """Read only exact AGENTS.md; optional absence is disclosed in JSON."""
    return document_result(dict(api(ctx, config, json_output).agents()), output)


@agents.command("set")
def agents_set(
    ctx: typer.Context,
    file: FileOption,
    config: ConfigOption = None,
    json_output: JsonOption = False,
) -> Result:
    """Explicitly create or replace only the workspace AGENTS.md object."""
    text = read_document(file)
    client = api(ctx, config, json_output)
    announce_write(client, "workspace")
    return Result(client.set_agents(text))


@workspace_tags.command("list")
def workspace_tags_list(
    ctx: typer.Context, config: ConfigOption = None, json_output: JsonOption = False
) -> Result:
    """Read native workspace tags."""
    return Result(api(ctx, config, json_output).tags("workspace"))


@workspace_tags.command("set")
def workspace_tags_set(
    ctx: typer.Context,
    tags: Annotated[
        list[str] | None, typer.Argument(help="Replacement tags; omit all to clear.")
    ] = None,
    config: ConfigOption = None,
    json_output: JsonOption = False,
) -> Result:
    """Replace the native tag set; requires manageTags permission."""
    client = api(ctx, config, json_output)
    announce_write(client, "workspace")
    return Result(client.set_tags("workspace", tags or []))


@pipeline_tags.command("list")
def pipeline_tags_list(
    ctx: typer.Context,
    pipeline: str,
    config: ConfigOption = None,
    json_output: JsonOption = False,
) -> Result:
    """Read native pipeline tags."""
    return Result(api(ctx, config, json_output).tags("pipelines", pipeline))


@pipeline_tags.command("set")
def pipeline_tags_set(
    ctx: typer.Context,
    pipeline: str,
    tags: Annotated[
        list[str] | None, typer.Argument(help="Replacement tags; omit all to clear.")
    ] = None,
    config: ConfigOption = None,
    json_output: JsonOption = False,
) -> Result:
    """Replace only pipeline tags, leaving scheduling/config untouched."""
    client = api(ctx, config, json_output)
    selected = announce_write(client, "pipelines", pipeline)
    return Result(client.set_tags("pipelines", tags or [], selected))


@metadata.command("list")
def metadata_list(
    ctx: typer.Context,
    dataset: str,
    version: VersionOption = None,
    file: Annotated[
        str | None,
        typer.Option("--file", help="Remote filename/UUID, not a local path."),
    ] = None,
    config: ConfigOption = None,
    json_output: JsonOption = False,
) -> Result:
    """Read attributes at dataset, version or exact file scope."""
    return Result(api(ctx, config, json_output).metadata(dataset, version, file))


@metadata.command("set")
def metadata_set(
    ctx: typer.Context,
    dataset: str,
    key: str,
    value: Annotated[str | None, typer.Option("--value")] = None,
    json_value: Annotated[str | None, typer.Option("--json-value")] = None,
    value_file: Annotated[Path | None, typer.Option("--value-file")] = None,
    version: VersionOption = None,
    file: Annotated[
        str | None, typer.Option("--file", help="Remote filename/UUID.")
    ] = None,
    config: ConfigOption = None,
    json_output: JsonOption = False,
) -> Result:
    """Set one custom key; value forms are mutually exclusive, system keys protected."""
    if sum(v is not None for v in (value, json_value, value_file)) != 1:
        raise SetupError("Choose exactly one of --value, --json-value or --value-file")
    parsed = (
        json.loads(json_value)
        if json_value is not None
        else read_document(value_file)
        if value_file is not None
        else value
    )
    client = api(ctx, config, json_output)
    selected = announce_write(client, "datasets", dataset)
    assert selected is not None
    return Result(client.set_metadata(selected, key, parsed, version, file))
