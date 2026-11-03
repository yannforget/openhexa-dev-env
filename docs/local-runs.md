# Local runs

`hexa run` runs your pipeline on your machine, with the same image that OpenHEXA
uses for hosted runs. You can change the code and run it again without deploying.

The code runs on your machine, but it uses the workspace token: it reads and
writes real datasets and uses real connections. This is not a sandbox. Only run
code you trust, and write to test datasets while you develop.

## Setup

You need rootless Podman. Inside a Distrobox, `hexa` calls the host's Podman for
you.

`hexa` uses one specific version of the OpenHEXA runtime image and does not
pull it for you. Run `doctor` to check the setup:

```bash
hexa doctor pipelines/import_data
```

If the image is missing, `doctor` prints the exact `podman pull` command to run.
The image is large (about 9 GB), so the first pull takes a while.

`doctor` doesn't start anything. It checks Podman, the image and the folders,
and shows what the container will get: mounts, environment and limits. Add
`--workspace` to also see which image hosted runs use in your workspace. This
helps when local and hosted runs give different results.

## Project layout

Keep your credentials and outputs out of the pipeline folder:

```text
my-project/
  .env
  pipelines/
    import_data/pipeline.py
    build_report/pipeline.py
  parameters/
    import-test.json
```

`hexa run` refuses a folder that contains `.env`, so point it at a pipeline
folder, not the project root.

## Parameters

A parameter file is a JSON object keyed by parameter code:

```json
{"dataset": "population-test", "years": [2024, 2025], "dry_run": true}
```

Override single values with `--set`. Values are JSON, so strings need quotes:

```bash
hexa run pipelines/import_data --parameters parameters/import-test.json \
  --set dry_run=false --set 'country="BFA"'
```

Before running, you can check your parameters offline. These commands read the
code without running it, and don't need credentials or Podman:

```bash
hexa inspect pipelines/import_data       # list declared parameters
hexa validate pipelines/import_data --parameters parameters/import-test.json
```

`validate` checks names, types and required values. It doesn't check that
datasets or connections actually exist.

Don't put secrets in parameter files or on the command line.

## Local datasets and connections

Select overrides explicitly for one execution:

```bash
hexa run pipelines/import_data \
  --set 'dataset="population"' \
  --local-dataset population=.hexa/datasets/population \
  --connections-file .hexa/connections.env
```

These flags resolve paths from **your current directory**, including when `--config`
selects a project elsewhere. They do not add `hexa.json` settings, discover files,
change deployment, or affect `hexa runs start`. Omit them for the normal connected
runner. Workspace credentials are still required.

### Dataset directories

`--local-dataset SLUG=DATASET_DIR` is repeatable. Point at a **dataset directory**,
not an individual version:

```text
.hexa/datasets/population/
  v2/
    data.csv
  v10/
    data.csv
    metadata/source.json
```

- Each immediate subdirectory is one version; a dataset with no version directories
  is an error. Loose files at the dataset root are rejected. Empty versions are valid.
- Versions enumerate in ascending **natural name order**: `v2` precedes `v10`;
  `latest_version` is the greatest name. ISO dates work too. Text comparison is
  case-insensitive, with original names breaking ties; modification times are ignored.
- The override applies to typed Dataset parameters (including defaults/lists) and
  `workspace.get_dataset("population")`. An omitted source workspace or the selected
  workspace matches; an explicit **different** source workspace stays remote.
- The overridden dataset need not exist remotely. Its versions/files are entirely
  local: no remote lookup, merge or missing-file fallback. `get_file()` prefers exact
  relative paths, then unique basenames; ambiguous basenames fail. Use `file.uri`
  to distinguish nested paths. Metadata is synthetic: `local:` IDs, slug as the name,
  and empty creation timestamps, not remote identities or provenance.
- `create_version("v11")` creates a subdirectory **on your host**. `add_file()` writes
  new files there, including nested paths. Any version accepts new files; existing
  version names and file paths cannot be overwritten. This deliberately does not
  reproduce remote version locking. Natural ordering still determines latest, so
  creating `v3` does not supersede `v10`.

Selected datasets are **live writable mounts**, not snapshots. Writes persist even
if the pipeline fails; there is no rollback. Back up valuable local data first.
Concurrent runs share the directory: exclusive creates prevent overwrites, but these
are not transactional catalogs. Versions/files are registered at startup and through
SDK writes; out-of-band additions are discovered on the next run. Existing registered
file contents stay live. The run record describes the initial selection, not content
hashes or a complete local-write history.

Only explicitly selected dataset directories are mounted at tool-selected paths.
Source and ordinary extra inputs remain read-only. Symlinks, hard-linked files,
unsupported paths, special files and private credential aliases are rejected.
Dataset mounts use shared SELinux `:z` labeling unless `source.relabel` is false.

`file.read()` and `file.download_url` work with local files. URLs are plain HTTP on
container loopback, served by a bounded subprocess that also shares the catalog
between SDK task workers. GET, HEAD, streaming and single byte ranges are supported;
invalid, unsatisfiable and multi-range requests return 416. Requests/pandas URL reads
work; HTTPS-only clients, external fetchers, other network namespaces, and using a URL
after the run are unsupported. No host ports are published.

### Connection file grammar

The selected file is private; keep it out of source control and mounted directories:

```dotenv
LOCAL_DHIS=DHIS2
LOCAL_DHIS_URL=https://dhis2.example.test
LOCAL_DHIS_USERNAME=developer
LOCAL_DHIS_PASSWORD="example-only"
```

Identifiers normalize to uppercase with hyphens changed to underscores, matching
SDK 2.23.0. For example `workspace.get_connection("local-dhis")` uses `LOCAL_DHIS`.
Normalized duplicate keys are errors. Each identifier declares an uppercase type,
followed by its fields:

| Type | Required field suffixes |
| --- | --- |
| `DHIS2`, `IASO` | `URL`, `USERNAME`, `PASSWORD` |
| `POSTGRESQL` | `HOST`, `PORT`, `USERNAME`, `PASSWORD`, `DB_NAME` |
| `S3` | `ACCESS_KEY_ID`, `ACCESS_KEY_SECRET`, `BUCKET_NAME` |
| `GCS` | `SERVICE_ACCOUNT_KEY`, `BUCKET_NAME` |
| `CUSTOM` | One or more named fields; `IDENTIFIER` is reserved |

`DATABASE_NAME` is an alias for PostgreSQL `DB_NAME`; `SECRET_ACCESS_KEY` is an alias
for S3 `ACCESS_KEY_SECRET`. Supplying both spellings is an error. PostgreSQL ports
must be integers from 1 to 65535. DHIS2/IASO URLs must be HTTP(S); GCS key material
must be a nonempty JSON object. Authentication itself is not tested. All fields must
be nonempty. Unknown fields/types,
missing fields, duplicate keys and runtime/environment-control variables are rejected.
Do not choose identifiers that prefix other definitions (`TEST` and `TEST_OTHER`):
`TEST_OTHER_*` belongs to `TEST` and is not a separate definition.

Assignments are single-line literals. Blank lines and `#` comments are accepted;
unquoted values cannot contain whitespace, and an inline comment starts with ` #`.
Single or double quotes preserve whitespace and `#` characters. The matching quote
cannot appear inside a quoted literal; escapes, interpolation, shell evaluation,
command substitution and imports are never performed. For service-account JSON,
use single quotes around the JSON (backslashes remain literal):

```dotenv
LOCAL_GCS=GCS
LOCAL_GCS_BUCKET_NAME=test-bucket
LOCAL_GCS_SERVICE_ACCOUNT_KEY='{"type":"service_account","private_key":"example-only\\n"}'
```

Matched definitions completely replace remote/ambient connection fields. Undefined
identifiers retain ordinary SDK resolution. The file never becomes a container mount;
validated values travel privately on stdin, then live in process environments for
SDK task inheritance, not in Podman arguments/environment options or startup files.

### Connected execution is still real

These overrides are **not an offline or network/write-restricted sandbox**.
Unmatched datasets/connections, direct generated-client calls, raw GraphQL, workspace dataset listing and
unrelated remote writes remain real. An overridden Dataset object writes locally;
use a different, unmatched slug when intentionally publishing remotely.

Loading a connection does not start a service or make its target safe: it can still
point at production. `localhost` inside the container is not the host. No networking
configuration, service starts or image pulls happen implicitly.

Console output, retained logs and records redact known connection fields, tokens
and capability URLs on a best-effort basis. Pipeline code can read its credentials;
this is not containment against malicious code or arbitrary encodings. User-produced
output files are not sanitized. Run records contain selected host paths, version/file
names and connection identifiers/types only; these are still potentially private
metadata.

## Run records

Each run gets a folder in `.hexa/runs/RUN_ID/`:

- `output/`: the files your pipeline wrote to the workspace folder
- `log.txt`: output with token, known connection values and URLs redacted
- `run.json`: source hash, parameters, image, timing, exit code and, when selected,
  non-secret `local_resources` provenance (initial datasets/versions/files and connection
  identifiers/types)

At the end, `hexa run` lists what the SDK changed on OpenHEXA (for example new
dataset versions) and exits with the pipeline's exit code. It only sees
changes made through the SDK, not other HTTP requests in your code.

Inside the container, your code is at `/source` (read-only), and workspace output
files go to `/output` and `/scratch`. SDK writes to overridden datasets instead
persist directly in the selected host dataset directories.

## Extra input folders

To give the pipeline access to a local folder, add it to a `hexa.json` in the
project folder. It is mounted read-only at `/inputs/NAME`:

```json
{"inputs": [{"name": "survey", "path": "/path/to/survey"}]}
```

## Troubleshooting

If the run fails before your code starts, `hexa` prints a `Hint:` line. Run
`hexa doctor` to see what's missing. The usual causes are an image that isn't
pulled, or a folder Podman can't read.

On SELinux hosts (Fedora, for instance), folders are relabeled so the container
can read them. The first run on a large folder can be slow. `hexa` refuses
to relabel your home folder or system folders.

If your code fails, the traceback comes from your pipeline. Files written before
the failure stay in `.hexa/runs/RUN_ID/output/`.
