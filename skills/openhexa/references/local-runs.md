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

## Project layout

```text
project/
  .env               # credentials, git-ignored
  hexa.json          # optional: {"credential_file": ".env", "source": {...}, "inputs": [...]}
  pipelines/<name>/pipeline.py
  parameters/<name>-test.json
```

Never pass a directory that contains `.env`. Use test datasets for publishing during
local runs, and point the `Dataset` parameters at them.

## When a run fails before your code starts

`hexa` prints the cause and a `Hint:` line; `hexa doctor DIR` reports what is missing.
If the image is not cached, ask the user to pull it: `hexa` never pulls.

When the code itself fails, the traceback is the pipeline's own. Files written
before the failure stay in `.hexa/runs/RUN_ID/output/` for inspection.
