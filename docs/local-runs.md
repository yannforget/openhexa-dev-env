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

## Run records

Each run gets a folder in `.hexa/runs/RUN_ID/`:

- `output/`: the files your pipeline wrote to the workspace folder
- `log.txt`: the full output, with the token removed
- `run.json`: source hash, parameters, image, timing and exit code

At the end, `hexa run` lists what the SDK changed on OpenHEXA (for example new
dataset versions) and exits with the pipeline's exit code. It only sees
changes made through the SDK, not other HTTP requests in your code.

Inside the container, your code is at `/source` (read-only), and files go to
`/output` and `/scratch`.

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
