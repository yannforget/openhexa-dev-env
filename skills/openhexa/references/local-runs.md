# Running pipelines locally with `hexa run`

`hexa run DIR` runs `DIR/pipeline.py` in a disposable Podman container, using the
runtime image pinned by `hexa`. The SDK runs in local-pipeline mode against your
workspace. Nothing is deployed and no cloud run is created, but API calls (dataset
reads and writes) and connection access are **real**.

## Container contract

`hexa doctor DIR` shows the mounts, environment, limits and image a run would use, and
checks the local prerequisites without starting anything. `/output` is
`workspace.files_path` and is fresh for each run; the token goes in on stdin and is
redacted from output. Network access is unrestricted.

Logs stream to stderr and are saved to `.hexa/runs/RUN_ID/log.txt`; `run.json` beside
it records the source hash, parameters, image, timing and exit code. At the end,
`hexa run` lists the remote writes the SDK made (datasets, versions, files) and exits
with the pipeline's exit code. Writes through other HTTP clients are not tracked.

## Parameters

- `--parameters FILE` is a JSON object of parameter values, e.g.
  `{"output": "my-dataset-test", "roots": ["abc", "def"]}`. Datasets and connections
  are given by slug.
- `--set CODE=JSON` overrides one value and can be repeated: `--set end_year=2026`,
  `--set 'roots=["abc"]'`, `--set 'name="text"'`. Strings need JSON quotes.
- Parameters you leave out use the SDK defaults. Do not put Secret values in files or
  flags.
- Run `hexa validate DIR --parameters FILE` first for a fast offline check of types and
  required values. It does not check that the datasets exist.

## Explicit local resources

```bash
hexa run pipelines/example \
  --set 'dataset="population"' \
  --local-dataset population=.hexa/datasets/population \
  --connections-file .hexa/connections.env
```

- `--local-dataset SLUG=DATASET_DIR` is repeatable. Select the dataset root, with
  immediate version subdirectories such as `v2/` and `v10/`; do not pass a version
  directory. No versions is an error, empty versions are valid, loose root files are
  rejected. Natural name ordering determines latest (`v10` > `v2`), not timestamps.
- Typed Dataset parameters/defaults/lists and `workspace.get_dataset()` use the
  override. An explicit different `source_workspace_slug` stays remote. A matched
  dataset needs no remote counterpart; missing files fail locally, without fallback.
- `versions` enumerates ascending natural names. `get_file()` prefers exact relative
  paths then unique basenames; ambiguous basenames fail. File `uri` preserves nested
  paths. IDs start with `local:`, names use the slug, and creation timestamps are
  empty rather than fabricated.
- **Dataset overrides are live writable mounts.** `create_version()` creates a host
  subdirectory and `add_file()` adds local files to any version. Existing names/paths
  cannot be overwritten. There is no remote-lock emulation or rollback; writes
  persist after failures. Creating `v3` leaves `v10` latest. Only selected datasets
  are writable; source/ordinary extra inputs stay read-only. Versions/files are
  registered at startup and by SDK writes; out-of-band additions appear next run.
- `file.read()` and HTTP `download_url` work, including SDK spawn tasks, requests,
  streaming, pandas and single byte ranges. Invalid/multi-range requests return 416.
  URLs are run-local container loopback capabilities: no published host ports, no
  external fetchers, HTTPS-only clients or post-run use. Do not retain or print URLs.
- Both flags are execution-only and paths are caller-relative even with `--config`.
  No `hexa.json` keys, auto-discovery or changes to deployment/remote run commands.

The connections file contains literal definitions, for example:

```dotenv
TEST_DHIS2=DHIS2
TEST_DHIS2_URL=https://dhis2.example.test
TEST_DHIS2_USERNAME=developer
TEST_DHIS2_PASSWORD="example-only"
```

Identifiers normalize to uppercase/underscores, matching SDK 2.23.0. Matched local
connections replace all fields; unmatched identifiers retain normal resolution.
Supported types and required suffixes:

- DHIS2/IASO: `URL`, `USERNAME`, `PASSWORD`.
- PostgreSQL (`POSTGRESQL`): `HOST`, `PORT` (1–65535), `USERNAME`, `PASSWORD`, `DB_NAME`
  (alias `DATABASE_NAME`).
- S3: `ACCESS_KEY_ID`, `ACCESS_KEY_SECRET` (alias `SECRET_ACCESS_KEY`), `BUCKET_NAME`.
- GCS: `SERVICE_ACCOUNT_KEY`, `BUCKET_NAME`; single-quote service-account JSON.
- CUSTOM: at least one named field, excluding `IDENTIFIER`.

DHIS2/IASO URLs must be HTTP(S), and GCS material a nonempty JSON object;
authentication itself is not tested. Values must be nonempty single-line literals. Quotes preserve whitespace and `#`,
without escape processing or interpolation; matching quotes cannot occur inside.
Unquoted values have no whitespace; ` #` starts an inline comment. Blank/comment
lines are accepted. No shell evaluation, command substitution, imports or credential
searches. Duplicate normalized keys, both aliases together, unknown/missing typed
fields and runtime-control variables fail closed. Do not use one connection identifier
as a prefix of another (`TEST` and `TEST_OTHER`). Keep the file private and outside
source/input/dataset mounts; aliases, symlinks and unsafe dataset entries are rejected.
Dataset SELinux labeling follows `source.relabel` (`:z` by default).

**This remains connected execution.** Workspace credentials are required; raw API/
generated-client calls, workspace dataset listing and unmatched resources are unaffected; unrelated remote
writes remain real. An overridden dataset is a local publishing target; use a
separate unmatched slug for intentional remote publishing. Connection targets can be
production. Nothing starts their services or changes networking: container localhost
is not the host.

Connection values go privately over stdin and into process environments, not Podman
launch options, mounts or startup files. Logs/records best-effort redact known secrets
and capability URLs, not arbitrary encodings or malicious code. User output files
are unsanitized. `run.json.local_resources` records initial host paths, version/file
names and connection identifiers/types, never values; paths are still private metadata.
Live mounts are not reproducible snapshots and records are not a local-write ledger.

## Project layout

```text
project/
  .env               # credentials, git-ignored
  hexa.json          # optional: {"credential_file": ".env", "source": {...}, "inputs": [...]}
  pipelines/<name>/pipeline.py
  parameters/<name>-test.json
```

Never pass a source/input/dataset directory that contains `.env` or the selected
connections file. Use test datasets for remote publishing during local runs, or select
an explicit local override when writes should remain in your dataset directory.

## When a run fails before your code starts

`hexa` prints the cause and a `Hint:` line; `hexa doctor DIR` reports what is missing.
If the image is not cached, ask the user to pull it: `hexa` never pulls.

When the code itself fails, the traceback is the pipeline's own. Files written
before the failure stay in `.hexa/runs/RUN_ID/output/` for inspection. Writes to
local dataset overrides instead remain in the selected host dataset directories.
