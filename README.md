# hexa

`hexa` is a command-line tool for working with an OpenHEXA workspace from your
own machine. You can browse and download data, write a pipeline and run it
locally against real data, then deploy it and start runs on OpenHEXA. It is built
to be used by you or by your coding agent.

## Overview

You work from a project folder linked to one workspace. The link is a `.env` file
with the instance URL, the workspace slug and a workspace token. Every command
runs against that workspace, with that token's permissions.

```text
my-project/
  .env              ← workspace URL, slug and token
  pipelines/
    import_data/pipeline.py
  .hexa/runs/       ← local run logs and outputs
```

Most commands only read. The ones that change something on OpenHEXA (upload a
dataset version, deploy a pipeline, start a run, publish a webapp) are always
explicit, and the riskier ones need `--yes`, with `--dry-run` to preview them.

`hexa run` runs your pipeline code on your machine, in a Podman container that
uses the same image as hosted runs. Your code uses the real workspace, so a local
run can read and write real datasets. Don't confuse it with `hexa runs`,
which lists, starts and follows runs on OpenHEXA.

Add `--json` to any command to get machine-readable output. That's what agents
use.

## Install

You need Python 3.13+ and [uv](https://docs.astral.sh/uv/). From a clone of this
repo:

```bash
uv tool install .
```

Then create a `.env` in your project folder (see [`.env.example`](.env.example)):

```dotenv
OPENHEXA_URL=https://app.openhexa.org
OPENHEXA_WORKSPACE=my-workspace
OPENHEXA_TOKEN=...
```

Keep it out of Git. Check that it works:

```bash
hexa workspace show
```

To run pipelines locally, you also need Podman and the runtime image. See
[local runs](docs/local-runs.md).

## Usage

Find and download data:

```bash
hexa datasets list
hexa datasets show population-estimates
hexa datasets download population-estimates --dir data/
```

Write a pipeline, run it locally, then deploy and run it on OpenHEXA:

```bash
hexa validate pipelines/import_data --parameters params.json
hexa run pipelines/import_data --parameters params.json
hexa pipelines deploy pipelines/import_data --pipeline import-data --version-name v2 --yes
hexa runs start import-data --version v2 --parameters params.json --yes
hexa runs wait RUN_ID
```

Debug a failed run on OpenHEXA:

```bash
hexa runs list --pipeline import-data
hexa runs logs RUN_ID
hexa pipelines source import-data --version VERSION_ID --output ./debug
```

Other commands cover connections, workspace and dataset docs, tags, metadata and
static webapps. Run `hexa --help`, or `--help` on any command, for the full list.

## Output

Some examples of what `hexa` prints. Listing datasets:

```console
$ hexa datasets list
Population Estimates
Slug: population-estimates
Workspace: source-workspace
Annual population estimates by health area, 2015–2025.

Facility Register
Slug: facility-register
Workspace: my-workspace
Public and private health facilities with coordinates and identifiers.

2 datasets
```

Listing pipelines:

```console
$ hexa pipelines list
Import data
Code: import-data
Version: v2
Pulls monthly reports from DHIS2 and publishes them as a dataset.

Build report
Code: build-report
Version: v5
Builds the quarterly coverage report from the latest import.

2 pipelines
```

Listing runs:

```console
$ hexa runs list --pipeline import-data --limit 3
Runs of import-data
e0625760-bca9-49bb-9e44-7642b0eb4a93  success  v2  2026-11-02 17:35  20 s
02bf525b-98eb-48a0-8761-75c83285925e  success  v1  2026-10-31 17:18  30 s
8618d5dc-6089-42eb-9ea7-319278fe5f51  failed   v1  2026-10-13 18:46  1 min 5 s

Showing 3 of 8 runs.
Use --all to see all of them.
```

Showing a run:

```console
$ hexa runs show 8618d5dc-6089-42eb-9ea7-319278fe5f51
Run 8618d5dc-6089-42eb-9ea7-319278fe5f51
Pipeline: import-data, version v1 (adb5bab6-6865-4ad8-b6d2-2cd7143dff32)
Status: failed
Started: 2026-10-13 18:46, took 1 min 5 s (UTC)

Outputs
  import-data/clean_monthly.csv               507 B
  import-data/analytical_monthly.parquet  815.0 KiB

Read the logs with: hexa runs logs 8618d5dc-6089-42eb-9ea7-319278fe5f51
```

Comparing two dataset versions:

```console
$ hexa datasets diff monthly-sales 2026-05 2026-06 --content
~ sales.parquet  (size changed, 1675 B → 1684 B)
2026-05 → 2026-06: 1 differing, 0 same content
```

The end of a local run, with what the pipeline changed on OpenHEXA:

```console
$ hexa run pipelines/import_data --parameters params.json
...
Remote writes (via SDK; other HTTP clients are not tracked): createDatasetVersion → v2 (5b53…); createDatasetVersionFile ×6
Log: .hexa/runs/3f9c…/log.txt
```

An error, as text and as JSON:

```console
$ hexa datasets show populaton-estimates
Error: Dataset "populaton-estimates" was not found or is not accessible.
Hint: Run "hexa datasets list" to see available datasets.

$ hexa datasets show populaton-estimates --json
{"code": "NOT_FOUND_OR_INACCESSIBLE", "message": "Dataset \"populaton-estimates\" was not found or is not accessible.", "exit_code": 3, "target": {"resource": "dataset", "selector": "populaton-estimates"}, "hint": "Run \"hexa datasets list\" to see available datasets."}
```

## Documentation

- [Local runs](docs/local-runs.md): setup, parameters, run records, troubleshooting
- [Datasets](docs/datasets.md): download, compare and publish versions
- [Pipelines](docs/pipelines.md): deploy, start and follow remote runs
- [Webapps](docs/webapps.md): publish a static app

## Coding agents

The [`openhexa` skill](skills/openhexa/SKILL.md) gives an agent instructions for
using OpenHEXA and `hexa`. Ask your agent to read `skills/openhexa/SKILL.md`, or see
[skills/README.md](skills/README.md) to install it.

## Contributing

See [AGENTS.md](AGENTS.md) for the development setup, checks and conventions.
