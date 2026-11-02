"""Dataset, connection, pipeline and remote execution diagnostic commands."""

import sys
import time
from pathlib import Path
from typing import Annotated

import typer
from typer._click.exceptions import MissingParameter, UsageError

from ..config import read_inputs
from ..output import Result
from .common import (
    AllOption,
    ConfigOption,
    JsonOption,
    LimitOption,
    OutputOption,
    VersionOption,
    announce_write,
    api,
    group,
    state,
)

datasets = group(
    "Discover datasets and explicitly create datasets or upload new versions."
)
connections = group("Inspect connection metadata, never credential values.")
pipelines = group(
    "Inspect deployed code and explicitly deploy new versions; never executes code."
)
runs = group(
    "Inspect remote runs; start launches one explicitly, nothing is ever retried."
)


@connections.command(
    "list", epilog="JSON: items[].{id,slug,name,type,fields[].{code,secret}}"
)
def connections_list(
    ctx: typer.Context, config: ConfigOption = None, json_output: JsonOption = False
) -> Result:
    """List connection field codes and secret flags only."""
    return Result(api(ctx, config, json_output).connections(), listing="connections")


@connections.command("show")
def connections_show(
    ctx: typer.Context,
    connection: str,
    config: ConfigOption = None,
    json_output: JsonOption = False,
) -> Result:
    """Resolve an exact workspace connection slug or UUID."""
    return Result(api(ctx, config, json_output).connection(connection))


@datasets.command(
    "list",
    epilog="JSON: items[].dataset.{id,slug,name,workspace.slug,latestVersion.{id,name}}; items[].id is the link ID",
)
def datasets_list(
    ctx: typer.Context,
    limit: LimitOption = 20,
    all_items: AllOption = False,
    config: ConfigOption = None,
    json_output: JsonOption = False,
) -> Result:
    """List linked datasets with separate link and source-owner identities."""
    return Result(
        api(ctx, config, json_output).datasets(None if all_items else limit),
        listing="datasets",
    )


@datasets.command(
    "show",
    epilog="JSON: {id,slug,name,workspace.slug,permissions,latestVersion.{id,name},link_id}",
)
def datasets_show(
    ctx: typer.Context,
    dataset: str,
    config: ConfigOption = None,
    json_output: JsonOption = False,
) -> Result:
    """Resolve slug/UUID or source-workspace/slug, rejecting ambiguity."""
    return Result(api(ctx, config, json_output).dataset(dataset))


@datasets.command("create", epilog="JSON: dataset.{id,slug,latestVersion}")
def dataset_create(
    ctx: typer.Context,
    name: str,
    description: Annotated[str, typer.Option("--description")] = "",
    config: ConfigOption = None,
    json_output: JsonOption = False,
) -> Result:
    """Create a dataset in the configured workspace, without uploading files."""
    client = api(ctx, config, json_output)
    announce_write(client, "workspace")
    return Result(client.create_dataset(name, description), dataset_write=True)


@datasets.command(
    "upload",
    epilog="JSON: version.{id,name}, files[].{id,path,size}, uploaded_file_count, verified",
)
def dataset_upload(
    ctx: typer.Context,
    dataset: str,
    files: Annotated[
        list[Path] | None,
        typer.Argument(
            help="Readable local files; distinct basenames become remote filenames."
        ),
    ] = None,
    name: Annotated[
        str | None, typer.Option("--name", help="Required new version name.")
    ] = None,
    directory: Annotated[
        Path | None,
        typer.Option(
            "--dir",
            help="Upload every file under DIR, keeping relative paths as remote paths.",
        ),
    ] = None,
    last: Annotated[
        str | None,
        typer.Option(
            "--last", help="With --dir: relative path to upload last (a marker)."
        ),
    ] = None,
    changelog: Annotated[str | None, typer.Option("--changelog")] = None,
    config: ConfigOption = None,
    json_output: JsonOption = False,
) -> Result:
    """Create a NEW version containing ONLY supplied files, not a copy of the previous version.

    With --dir, .git, __pycache__, .venv, node_modules and .hexa are skipped; dotenv
    files, the credential file and files containing the API token are refused.
    Uploads are non-atomic. On failure inspect the reported exact version. Repeating
    upload creates another version; it does not resume or roll back the first one.
    """
    if name is None:
        option = next(p for p in ctx.command.params if p.name == "name")
        raise MissingParameter(ctx=ctx, param=option)
    if directory is None and not files:
        argument = next(p for p in ctx.command.params if p.name == "files")
        raise MissingParameter(ctx=ctx, param=argument)
    if directory is not None and files:
        raise UsageError("Give FILES or --dir, not both", ctx)
    if last is not None and directory is None:
        raise UsageError("--last applies only with --dir", ctx)
    invocation = state(ctx, config, json_output)
    client = invocation.api()
    selected = announce_write(client, "datasets", dataset)
    assert selected is not None
    if directory is not None:
        assert invocation.project is not None
        result = client.upload_dataset_directory(
            selected,
            name,
            directory,
            changelog,
            last,
            invocation.project.credential_file,
        )
    else:
        assert files is not None
        result = client.upload_dataset_version(selected, name, files, changelog)
    return Result(result, dataset_write=True)


@datasets.command(
    "versions",
    epilog="JSON: dataset.{id,slug}, items[].{id,name,createdAt} (newest first)",
)
def dataset_versions(
    ctx: typer.Context,
    dataset: str,
    limit: LimitOption = 20,
    all_items: AllOption = False,
    config: ConfigOption = None,
    json_output: JsonOption = False,
) -> Result:
    """List dataset version names and exact UUIDs."""
    return Result(
        api(ctx, config, json_output).versions(dataset, None if all_items else limit),
        listing="dataset_versions",
    )


@datasets.command(
    "files",
    epilog="JSON: version.{id,name}, items[].{id,filename,path,size,contentType}",
)
def dataset_files(
    ctx: typer.Context,
    dataset: str,
    version: VersionOption = None,
    limit: LimitOption = 20,
    all_items: AllOption = False,
    config: ConfigOption = None,
    json_output: JsonOption = False,
) -> Result:
    """List file metadata; resolve omitted latest once and disclose it."""
    return Result(
        api(ctx, config, json_output).files(
            dataset, version, None if all_items else limit
        ),
        listing="files",
    )


@datasets.command(
    "download",
    epilog="JSON: version.{id,name}, file.{id,path,size}, path, bytes, sha256; "
    "with --dir: version.{id,name}, files[].{path,bytes,sha256,status}, downloaded, skipped",
)
def dataset_download(
    ctx: typer.Context,
    dataset: str,
    file: Annotated[
        str | None,
        typer.Argument(help="Relative path or UUID; omit with --dir for all files."),
    ] = None,
    version: VersionOption = None,
    output: OutputOption = None,
    directory: Annotated[
        Path | None,
        typer.Option(
            "--dir", help="Download every file of the version, keeping relative paths."
        ),
    ] = None,
    include: Annotated[
        list[str] | None,
        typer.Option("--include", help="With --dir: glob on relative paths; repeat."),
    ] = None,
    config: ConfigOption = None,
    json_output: JsonOption = False,
) -> Result:
    """Stream one file, or a whole version with --dir, to new local files.

    Existing files are never overwritten. With --dir, a file already present with
    the same size is skipped, so rerunning completes an interrupted download.
    """
    if directory is None and file is None:
        argument = next(p for p in ctx.command.params if p.name == "file")
        raise MissingParameter(ctx=ctx, param=argument)
    if directory is not None and (file is not None or output is not None):
        raise UsageError(
            "--dir downloads every file; do not combine it with FILE or --output", ctx
        )
    if include and directory is None:
        raise UsageError("--include applies only with --dir", ctx)
    client = api(ctx, config, json_output)
    if directory is not None:
        return Result(
            client.download_version(dataset, version, directory, tuple(include or ())),
            version_download=True,
        )
    assert file is not None
    return Result(client.download(dataset, version, file, output))


@datasets.command(
    "diff",
    epilog="JSON: identical, versions[].{id,name}, changes[].{path,change,bytes}",
)
def dataset_diff(
    ctx: typer.Context,
    dataset: str,
    first: Annotated[str, typer.Argument(help="Older version: name, number or UUID.")],
    second: Annotated[str, typer.Argument(help="Newer version: name, number or UUID.")],
    content: Annotated[
        bool,
        typer.Option(
            "--content",
            help="Hash same-size files by streaming them; nothing is written.",
        ),
    ] = False,
    config: ConfigOption = None,
    json_output: JsonOption = False,
) -> Result:
    """Compare two versions by relative path: added, removed, size or content changed.

    Exits 0 when identical and 1 when different, like `diff`. Without --content,
    same-size files are listed as unverified and count as a difference.
    """
    result = api(ctx, config, json_output).diff_versions(
        dataset, first, second, content
    )
    return Result(
        {**result, "exit_code": 0 if result["identical"] else 1}, version_diff=True
    )


@datasets.command("url")
def dataset_url(
    ctx: typer.Context,
    dataset: str,
    file: str,
    version: VersionOption = None,
    config: ConfigOption = None,
    json_output: JsonOption = False,
) -> Result:
    """Print only a temporary credential URL (sensitive) in text mode."""
    return Result(
        api(ctx, config, json_output).url(dataset, file, version), sensitive_url=True
    )


@pipelines.command(
    "list", epilog="JSON: items[].{id,code,name,currentVersion.{id,name,versionNumber}}"
)
def pipelines_list(
    ctx: typer.Context,
    limit: LimitOption = 20,
    all_items: AllOption = False,
    config: ConfigOption = None,
    json_output: JsonOption = False,
) -> Result:
    """List scoped pipeline summaries without logs or source archives."""
    return Result(
        api(ctx, config, json_output).pipelines(None if all_items else limit),
        listing="pipelines",
    )


@pipelines.command(
    "show",
    epilog="JSON: {id,code,name,currentVersion.{id,timeout,parameters},permissions}",
)
def pipeline_show(
    ctx: typer.Context,
    pipeline: str,
    config: ConfigOption = None,
    json_output: JsonOption = False,
) -> Result:
    """Inspect declarations/scheduling; sensitive defaults/config are omitted."""
    return Result(api(ctx, config, json_output).pipeline_show(pipeline))


@pipelines.command(
    "versions",
    epilog="JSON: pipeline.{id,code}, items[].{id,name,versionNumber,createdAt}",
)
def pipeline_versions(
    ctx: typer.Context,
    pipeline: str,
    limit: LimitOption = 20,
    all_items: AllOption = False,
    config: ConfigOption = None,
    json_output: JsonOption = False,
) -> Result:
    """List code version numbers/names and exact UUIDs."""
    return Result(
        api(ctx, config, json_output).pipeline_versions(
            pipeline, None if all_items else limit
        ),
        listing="pipeline_versions",
    )


@pipelines.command(
    "deploy",
    epilog="JSON: dry_run, target, version_name, files[].{path,bytes}, sha256, transport, "
    "pipeline.{name,timeout,parameters[].{code,type}}, warnings; "
    "with --yes: pipeline.{id,code}, version.{id,name,versionNumber}",
)
def pipeline_deploy(
    ctx: typer.Context,
    directory: Annotated[
        Path, typer.Argument(help="Pipeline directory with pipeline.py at its root.")
    ],
    version_name: Annotated[
        str, typer.Option("--version-name", help="New version name, e.g. v3.")
    ],
    new: Annotated[
        str | None,
        typer.Option("--new", help="Create a new pipeline with this name."),
    ] = None,
    description: Annotated[
        str | None,
        typer.Option("--description", help="With --new: pipeline description."),
    ] = None,
    pipeline: Annotated[
        str | None,
        typer.Option("--pipeline", help="Existing pipeline code, name or UUID."),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Check and show the upload; write nothing."),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes", help="Deploy; agents still need operator approval first."
        ),
    ] = False,
    config: ConfigOption = None,
    json_output: JsonOption = False,
) -> Result:
    """Upload DIR as a new pipeline version, or a new pipeline with --new.

    Every file is sent, including JSON/CSV assets, except .git, __pycache__, .venv,
    node_modules, .hexa and globs listed in DIR/.hexaignore; dotenv files, the
    credential file and files containing the token are refused. The request must fit
    in 5 MiB. Parameters are parsed by the server from pipeline.py. The write is sent
    once and never retried; on failure, inspect the pipeline's versions first.
    """
    if dry_run == yes:
        raise UsageError("Choose --dry-run or --yes", ctx)
    if (new is None) == (pipeline is None):
        raise UsageError("Choose --new NAME or --pipeline CODE", ctx)
    if description is not None and new is None:
        raise UsageError("--description applies only with --new", ctx)
    invocation = state(ctx, config, json_output)
    client = invocation.api()
    assert invocation.project is not None
    if yes:
        announce_write(
            client, "workspace" if new is not None else "pipelines", pipeline
        )
    return Result(
        client.deploy_pipeline(
            directory,
            version_name,
            new_name=new,
            description=description,
            pipeline=pipeline,
            credential_file=invocation.project.credential_file,
            dry_run=dry_run,
        ),
        pipeline_deploy=True,
    )


@pipelines.command("source")
def pipeline_source(
    ctx: typer.Context,
    pipeline: str,
    version: VersionOption = None,
    output: OutputOption = None,
    config: ConfigOption = None,
    json_output: JsonOption = False,
) -> Result:
    """Safely extract one uploaded ZIP to a new directory, for inspection only."""
    return Result(api(ctx, config, json_output).source(pipeline, version, output))


@runs.command(
    "list",
    epilog="JSON: items[].{id,status,executionDate,duration,version.{id,name},pipeline.{id,code}}",
)
def runs_list(
    ctx: typer.Context,
    pipeline: Annotated[str | None, typer.Option("--pipeline")] = None,
    limit: LimitOption = 20,
    all_items: AllOption = False,
    config: ConfigOption = None,
    json_output: JsonOption = False,
) -> Result:
    """List history; workspace-wide aggregation inspects at most 20 pipelines."""
    return Result(
        api(ctx, config, json_output).runs(pipeline, None if all_items else limit),
        listing="runs",
    )


@runs.command(
    "show",
    epilog="JSON: {id,status,progress,duration,version.{id,name},outputs,datasetVersions}",
)
def run_show(
    ctx: typer.Context,
    run: str,
    config: ConfigOption = None,
    json_output: JsonOption = False,
) -> Result:
    """Inspect exact executed version and output references, without raw config."""
    return Result(api(ctx, config, json_output).run_show(run), run_show=True)


@runs.command(
    "logs", epilog="JSON: items[].{timestamp,priority,message} (messages), truncated"
)
def run_logs(
    ctx: typer.Context,
    run: str,
    kind: Annotated[
        str, typer.Option("--kind", help="messages (default) or runtime.")
    ] = "messages",
    limit: LimitOption = 200,
    config: ConfigOption = None,
    json_output: JsonOption = False,
) -> Result:
    """Explicitly fetch unpaginated diagnostics; display only bounded redacted tail."""
    return Result(api(ctx, config, json_output).run_logs(run, kind, limit), logs=True)


def _duration(text: str) -> float:
    """Parse `90`, `90s`, `15m` or `2h` into seconds."""
    units = {"s": 1, "m": 60, "h": 3600}
    number, unit = (text[:-1], text[-1]) if text[-1:] in units else (text, "s")
    try:
        seconds = float(number) * units[unit]
    except ValueError:
        raise typer.BadParameter("use a number with s, m or h, e.g. 30m") from None
    if seconds <= 0:
        raise typer.BadParameter("must be positive")
    return seconds


@runs.command(
    "start",
    epilog="JSON: dry_run, pipeline.{id,code}, version.{id,name,versionNumber}, parameters, "
    "defaults_used, unchecked; with --yes: run.{id,status}",
)
def run_start(
    ctx: typer.Context,
    pipeline: Annotated[str, typer.Argument(help="Pipeline code, name or UUID.")],
    version: Annotated[
        str,
        typer.Option("--version", help="Exact version name, number or UUID."),
    ],
    parameters: Annotated[
        Path | None,
        typer.Option("--parameters", help="JSON object of inputs by parameter code."),
    ] = None,
    replacements: Annotated[
        list[str] | None,
        typer.Option("--set", help="CODE=JSON input; repeat for different codes."),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Resolve and check inputs; launch nothing."),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes", help="Launch; agents still need operator approval first."
        ),
    ] = False,
    config: ConfigOption = None,
    json_output: JsonOption = False,
) -> Result:
    """Launch one remote run of an exact version, with mail notifications off.

    Inputs are checked offline against that version's declared parameters; omitted
    ones use the version's stored config or declared defaults. Resources such as
    datasets and connections are not looked up. The launch is sent once and never
    retried; follow it with `hexa runs wait RUN`.
    """
    if dry_run == yes:
        raise UsageError("Choose --dry-run or --yes", ctx)
    values = read_inputs(parameters, tuple(replacements or ()))
    client = api(ctx, config, json_output)
    if yes:
        announce_write(client, "pipelines", pipeline)
    return Result(
        client.start_run(pipeline, version, values, dry_run=dry_run), run_start=True
    )


@runs.command(
    "wait",
    epilog="JSON: {id,status,progress,duration,version.{id,name}}, timed_out, "
    "log_tail (on failure)",
)
def run_wait(
    ctx: typer.Context,
    run: str,
    timeout: Annotated[
        str, typer.Option("--timeout", help="Give up after this long, e.g. 30m.")
    ] = "30m",
    interval: Annotated[
        str, typer.Option("--interval", help="Time between status checks.")
    ] = "15s",
    tail: Annotated[
        int,
        typer.Option("--tail", min=0, max=1000, help="Runtime log lines on failure."),
    ] = 40,
    config: ConfigOption = None,
    json_output: JsonOption = False,
) -> Result:
    """Wait for a run to finish, printing status changes; never changes anything.

    Exits 0 on success, 1 when the run failed, was stopped or skipped, and 124 on
    timeout (the run keeps going). On failure the last --tail runtime log lines are
    shown.
    """
    client = api(ctx, config, json_output)
    started = time.monotonic()

    def report(run: dict) -> None:
        elapsed = round(time.monotonic() - started)
        print(
            f"[{elapsed:>5}s] {run['status']} ({run['progress']}%)",
            file=sys.stderr,
            flush=True,
        )

    result = client.wait_run(run, _duration(timeout), _duration(interval), report)
    status = result["status"]
    if result["timed_out"]:
        exit_code = 124
    else:
        exit_code = 0 if status == "success" else 1
    if exit_code == 1 and tail:
        result["log_tail"] = client.run_logs(run, "runtime", tail)["items"]
    return Result({**result, "exit_code": exit_code}, run_wait=True)
