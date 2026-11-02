# Writing SDK pipelines

## Shape of a pipeline

```python
from openhexa.sdk import (
    Dataset,
    DHIS2Connection,
    current_run,
    parameter,
    pipeline,
    workspace,
)


@pipeline(name="Extract registry", timeout=3600)
@parameter("source", name="Source", type=DHIS2Connection, default="snis")
@parameter(
    "roots",
    name="Org unit roots",
    type=str,
    multiple=True,
    required=False,
    help="Empty means national scope.",
)
@parameter("output", name="Output dataset", type=Dataset, default="registry-raw")
def extract_registry(source, roots, output):
    path = fetch(source, roots)
    publish(path, output)


@extract_registry.task
def fetch(source, roots) -> str:
    ...
    return str(out_path)


@extract_registry.task
def publish(path: str, output: Dataset) -> None: ...


if __name__ == "__main__":
    extract_registry()
```

- **Metadata is parsed statically**, by the platform at upload time and by
  `hexa inspect`. Keep decorator arguments literal: no computed values, no aliased
  imports, no generated decorators. `@pipeline` goes above the `@parameter` decorators.
  The `code=` argument of `@pipeline` is deprecated.
- **Parameter codes** are `snake_case` and match the function arguments. Each needs a
  human-readable `name` for the web form, plus `help` when the format is not obvious.
- **Sibling modules** are imported as plain modules (`from helpers import x`). The
  source directory is on the import path, and relative imports do not work.

## Task semantics

The pipeline function builds a graph. Calling a task returns a **placeholder**, not a
value. Tasks run later in separate worker processes, as soon as their inputs are ready.

- Pass a placeholder directly as an argument to the next task. This is how a dependency
  is declared. Placeholders nested in lists or dicts are not resolved.
- Do not index, iterate over or branch on a placeholder. Branching on parameter values
  is fine.
- Arguments and return values must be serializable. Create HTTP clients and DB
  connections inside the task that uses them.
- Two tasks that share a filename have no dependency between them. To hand off a file,
  return its path from one task and pass it to the other.
- Logging an error does not fail the run. Raise an exception instead.

## Parameter types

| Type | Value received | Default / config form | `multiple` |
| --- | --- | --- | --- |
| `str`, `int`, `float` | value | literal | yes |
| `bool` | value | literal | no |
| `Dataset` | `Dataset` object (resolved via the API) | **slug string** | yes |
| `File` | workspace file object (`.path`) | workspace path | no |
| `DHIS2Connection`, `IASOConnection`, `PostgreSQLConnection`, … | connection object | connection slug | no |
| `Secret` | string | avoid defaults | no |

`required=False` gives `None` for an omitted scalar and `[]` for an omitted
`multiple`. `choices=[...]` must be literal, and is not available for bool,
connection, file or secret parameters.

## Resources inside a pipeline

- **Workspace files:** `Path(workspace.files_path) / "relative/path"`. Storage may be
  FUSE-backed, so do the heavy local work in `workspace.tmp_path` and copy the final
  result over. Put intentional outputs in a run-specific directory, not at the
  workspace root.
- **Connections:** `workspace.dhis2_connection("slug")` returns an object with `.url`,
  `.username` and `.password`, or use a connection parameter. Use
  `workspace.get_connection("slug")` for custom types. Never log these objects.
- **Database:** `workspace.database_url`, used with any PostgreSQL client.
- **Datasets:** `workspace.get_dataset("slug")`, then `.latest_version` (may be `None`)
  and `version.files` / `version.get_file(name)`; files have `.read()` and
  `.download_url`.
- **Publishing a dataset version:**
  1. Call `ds.create_version("vN")`, choosing N after looking at the existing versions.
  2. Call `version.add_file(local_path, filename="sub/dir/name.csv")` for each file.
     `filename` sets the relative path and defaults to the basename.
  3. A new version starts empty, so add every file it should contain.
  4. If publication fails part-way, the version is left incomplete. Inspect it before
     deciding what to do.
- **Run reporting:** `current_run.log_info/log_warning/log_error(...)`.
  `add_file_output(path)` and `add_database_output(table)` register outputs that
  already exist; they do not write anything. `current_run` is `None` in notebooks, so
  for type checkers add `assert current_run is not None` once.

Use the SDK's dataset and connection APIs inside pipelines. Only replace them with
raw HTTP after demonstrating a concrete defect, and tell the user when you do.

## Hosted runs are not local runs

`hexa doctor --workspace` compares the hosted runtime with `hexa run` (image, API URL).
In short: hosted runs get an internal `HEXA_SERVER_URL`, a run-scoped token, the
shared bucket as `workspace.files_path` and the workspace database.

- Never compare `HEXA_SERVER_URL` against a hostname in pipeline code.
- Code that writes to the workspace root locally will write to the shared bucket in
  the cloud.

## Packaging

The platform runs `pipeline.py` from the uploaded ZIP. An optional
`requirements.txt` is installed before import, on top of the image's packages. Bundle
everything the pipeline needs explicitly, including JSON, YAML and other assets. Load
assets relative to `__file__`. Deploy with `hexa pipelines deploy`, which ships assets;
the SDK CLI's `openhexa pipelines push` skips JSON and CSV.

Reference: <https://docs.openhexa.com/writing-pipelines/>,
<https://github.com/BLSQ/openhexa-sdk-python>.
