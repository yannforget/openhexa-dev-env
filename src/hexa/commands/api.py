"""Read-only schema introspection of the configured target; not a query shell."""

from typing import Annotated

import typer

from ..output import Result
from .common import ConfigOption, JsonOption, api, group

schema = group("Read-only GraphQL schema lookups on the configured target.")


@schema.command(
    "type",
    epilog="JSON: name, kind, description, fields[].{name,type,required,description}; "
    "enums: values[].{name,description}; unions: possible_types",
)
def schema_type(
    ctx: typer.Context,
    name: Annotated[
        str, typer.Argument(help="Exact type name, e.g. CreatePipelineInput.")
    ],
    config: ConfigOption = None,
    json_output: JsonOption = False,
) -> Result:
    """Describe one named type as the target server defines it.

    Shows fields with GraphQL type notation (`!` = required, `[...]` = list) and
    descriptions. Use it before a hand-written query; the deployed schema may differ
    from the upstream source.
    """
    return Result(api(ctx, config, json_output).schema_type(name), schema_type=True)
