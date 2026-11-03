"""Root Typer application with one safe invocation/error/output boundary."""

import io
import json
import logging
import sys
from dataclasses import replace
from pathlib import Path
from typing import Annotated, Any

import typer
from typer._click.exceptions import ClickException, MissingParameter

from .api import APIError
from .commands import api, browse, context, webapps
from .commands.common import ConfigOption, Invocation, JsonOption, state
from .config import (
    Config,
    Project,
    SetupError,
    load_parameters,
    selected_entrypoint,
)
from .doctor import hosted_image, local_report
from .output import Result, render, safe_value
from .parameters import inspect_pipeline, reject_secret_overrides, validate_parameters
from .resources import load_resources
from .runner import run
from .security import Redactor

app = typer.Typer(
    name="hexa",
    help="Explore a designated OpenHEXA workspace. run: local execution; runs: remote history.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode=None,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)
app.add_typer(context.workspace, name="workspace")
app.add_typer(browse.connections, name="connections")
app.add_typer(browse.datasets, name="datasets")
app.add_typer(browse.pipelines, name="pipelines")
app.add_typer(browse.runs, name="runs")
app.add_typer(webapps.webapps, name="webapps")
app.add_typer(api.schema, name="api")


@app.callback()
def root(
    ctx: typer.Context, config: ConfigOption = None, json_output: JsonOption = False
) -> None:
    """Use one invocation context, with optional explicit leaf overrides."""
    state(ctx, config, json_output)


ParametersOption = Annotated[
    Path | None,
    typer.Option(
        "--parameters",
        help="JSON object replacing configured inputs; caller-relative path.",
    ),
]
SetOption = Annotated[
    list[str] | None,
    typer.Option(
        "--set", help="Unique CODE=JSON replacement; repeat for different codes."
    ),
]


@app.command(
    "run",
    epilog="JSON: exit_code, run_id, output_directory, image, log_file, record_file, remote_writes[].{operation,success}, hint (on startup failure)",
)
def run_command(
    ctx: typer.Context,
    directory: Annotated[
        Path | None,
        typer.Argument(help="Pipeline directory; overrides configured source."),
    ] = None,
    parameters: ParametersOption = None,
    replacements: SetOption = None,
    local_datasets: Annotated[
        list[str] | None,
        typer.Option(
            "--local-dataset",
            help="SLUG=DATASET_DIR; repeatable. Version subdirectories, natural name order; local writes enabled.",
        ),
    ] = None,
    connections_file: Annotated[
        Path | None,
        typer.Option(
            "--connections-file",
            help="Private literal connection dotenv file; caller-relative, never auto-discovered.",
        ),
    ] = None,
    config: ConfigOption = None,
    json_output: JsonOption = False,
) -> Result:
    """Run trusted mounted local code in the cached pinned image, not in the cloud."""
    invocation = state(ctx, config, json_output)
    project = invocation.load()
    assert invocation.credentials is not None
    overrides = tuple(replacements or ())
    settings = Config.from_project(project, directory, parameters, overrides)
    if local_datasets or connections_file is not None:
        settings = replace(
            settings,
            local_resources=load_resources(
                settings,
                tuple(local_datasets or ()),
                connections_file,
            ),
        )
    codes = (
        set(settings.parameters)
        if parameters is not None
        else {item.partition("=")[0] for item in overrides}
    )
    reject_secret_overrides(settings.source.path / settings.entrypoint, codes)
    return Result(run(settings, invocation.credentials))


@app.command(
    "doctor",
    epilog="JSON: ok, checks[].{name,status,detail}, container.{image,mounts,environment,cpus,memory}",
)
def doctor_command(
    ctx: typer.Context,
    directory: Annotated[
        Path | None,
        typer.Argument(help="Pipeline directory; overrides configured source."),
    ] = None,
    workspace: Annotated[
        bool,
        typer.Option(
            "--workspace",
            help="Also load credentials and describe the image hosted runs use.",
        ),
    ] = False,
    config: ConfigOption = None,
    json_output: JsonOption = False,
) -> Result:
    """Check what `hexa run` needs and show the container contract; starts nothing.

    Without --workspace no credentials are read and nothing is sent over the network.
    Exits 2 when a required local prerequisite is missing.
    """
    invocation = state(ctx, config, json_output)
    project = Project.load(invocation.config)
    report = local_report(project, directory)
    if workspace:
        invocation.load()
        assert invocation.credentials is not None
        report["hosted_image"] = hosted_image(
            invocation.credentials, invocation.api().workspace()
        )
    return Result({**report, "exit_code": 0 if report["ok"] else 2}, doctor=True)


@app.command("inspect")
def inspect_command(
    ctx: typer.Context,
    directory: Annotated[
        Path, typer.Argument(help="Pipeline directory, relative to the caller.")
    ],
    config: ConfigOption = None,
    json_output: JsonOption = False,
) -> Result:
    """Inspect literal SDK declarations offline, without importing or executing source."""
    invocation = state(ctx, config, json_output)
    project = Project.load(invocation.config)
    return Result(inspect_pipeline(selected_entrypoint(project, directory)))


@app.command("validate")
def validate_command(
    ctx: typer.Context,
    directory: Annotated[
        Path, typer.Argument(help="Pipeline directory, relative to the caller.")
    ],
    parameters: ParametersOption = None,
    replacements: SetOption = None,
    config: ConfigOption = None,
    json_output: JsonOption = False,
) -> Result:
    """Check supported inputs offline; no resource lookup or execution safety guarantee."""
    invocation = state(ctx, config, json_output)
    project = Project.load(invocation.config)
    path = selected_entrypoint(project, directory)
    overrides = tuple(replacements or ())
    values = load_parameters(project, directory, parameters, overrides)
    codes = (
        set(values)
        if parameters is not None
        else {item.partition("=")[0] for item in overrides}
    )
    reject_secret_overrides(path, codes)
    return Result(validate_parameters(inspect_pipeline(path), values))


def _json_requested(args: list[str]) -> bool:
    before_separator = args[: args.index("--")] if "--" in args else args
    return "--json" in before_separator


def _emit_error(detail: dict[str, Any], invocation: Invocation) -> int:
    detail = safe_value(detail, invocation.token, redact_urls=True)
    if invocation.json_mode:
        print(json.dumps(detail, ensure_ascii=False))
    else:
        print(f"Error: {detail['message']}", file=sys.stderr)
        for diagnostic in detail.get("diagnostics", []):
            print(diagnostic, file=sys.stderr)
        if "limitations" in detail:
            print(detail["limitations"], file=sys.stderr)
        if "usage" in detail:
            print(detail["usage"], file=sys.stderr)
        if "target" in detail and set(detail["target"]) != {"resource", "selector"}:
            print(
                "Target: " + json.dumps(detail["target"], ensure_ascii=False),
                file=sys.stderr,
            )
        if "hint" in detail:
            print("Hint: " + detail["hint"], file=sys.stderr)
    return detail["exit_code"]


def main(argv: list[str] | None = None) -> int:
    """Execute with explicit status categories, safe errors and no traceback locals."""
    args = list(sys.argv[1:] if argv is None else argv)
    invocation = Invocation(json_mode=_json_requested(args))
    try:
        result = app(
            args=args,
            prog_name="hexa",
            standalone_mode=False,
            color=sys.stdout.isatty(),
            obj=invocation,
        )
        if isinstance(result, Result):
            if result.data.get("exit_code", 0) and "code" in result.data:
                return _emit_error(result.data, invocation)
            render(result, invocation.json_mode, invocation.token)
            return int(result.data.get("exit_code", 0))
        return 0 if result is None else int(result)
    except ClickException as error:
        missing_argument = (
            isinstance(error, MissingParameter)
            and error.param is not None
            and error.param.param_type_name == "argument"
        )
        detail: dict[str, Any] = {
            "exit_code": error.exit_code,
            "code": "MISSING_ARGUMENT" if missing_argument else "INVALID_USAGE",
            "message": error.format_message(),
        }
        if isinstance(error, MissingParameter) and error.param is not None:
            detail["parameter"] = error.param.name
            if missing_argument and error.param.name:
                detail["message"] = (
                    f"Missing required argument {error.param.name.upper()}."
                )
        context = getattr(error, "ctx", None)
        if context is not None:
            detail["usage"] = context.get_usage()
            detail["hint"] = f'Run "{context.command_path} --help" for help.'
        return _emit_error(detail, invocation)
    except (typer.Abort, KeyboardInterrupt):
        return _emit_error(
            {"code": "INTERRUPTED", "message": "Interrupted", "exit_code": 130},
            invocation,
        )
    except Exception as error:
        buffer = io.StringIO()
        logger = logging.getLogger("hexa")
        logger.propagate = False
        handler = logging.StreamHandler(buffer)
        logger.addHandler(handler)
        logger.exception("Command failed")
        logger.removeHandler(handler)
        status = (
            3
            if isinstance(error, APIError)
            else 2
            if isinstance(error, (SetupError, OSError, ValueError))
            else 1
        )
        code = (
            error.code
            if isinstance(error, APIError)
            else "INVALID_INPUT"
            if status == 2
            else "UNEXPECTED_FAILURE"
        )
        if isinstance(error, (APIError, SetupError)):
            message = str(error)
        elif isinstance(error, OSError):
            message = f"Local IO failed ({type(error).__name__}); check selected paths and permissions. Partial destinations may remain."
        elif isinstance(error, ValueError):
            message = "Invalid input format or value; check JSON and UTF-8 inputs."
        else:
            # Capture, redact and discard the traceback: never expose raw chains/locals.
            print(
                Redactor(invocation.token).text(buffer.getvalue()),
                file=sys.stderr,
                end="",
            )
            message = "Unexpected command failure; see redacted diagnostic."
        detail = {"code": code, "message": message, "exit_code": status}
        if isinstance(error, APIError) and error.details is not None:
            detail["target"] = error.details
            resource = error.details.get("resource")
            groups = {
                "connection": "connections",
                "dataset": "datasets",
                "pipeline": "pipelines",
                "webapp": "webapps",
            }
            group = groups.get(resource) if isinstance(resource, str) else None
            if code == "NOT_FOUND_OR_INACCESSIBLE" and group is not None:
                detail["hint"] = f'Run "hexa {group} list" to see available {group}.'
        return _emit_error(detail, invocation)
