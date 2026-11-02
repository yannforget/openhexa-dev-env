# Pipelines

To write a pipeline and try it on your machine, see [local runs](local-runs.md).
This page is about pipelines on OpenHEXA.

## Deploy

Preview first, then deploy:

```bash
hexa pipelines deploy pipelines/import_data --pipeline import-data --version-name v4 --dry-run
hexa pipelines deploy pipelines/import_data --pipeline import-data --version-name v4 --yes
```

To create a new pipeline instead of adding a version:

```bash
hexa pipelines deploy pipelines/import_data --new "Import data" --version-name v1 --yes
```

The dry run shows the target, the files that will be sent and the parameters
OpenHEXA will see. It changes nothing.

Every file in the folder is sent, except `.git`, `.venv`, `__pycache__`,
`node_modules` and `.hexa`. To exclude other files, list glob patterns in a
`.hexaignore` file in the pipeline folder. The total must stay under 5 MiB, so put
data in a dataset rather than in the code. `.env` files and files containing your
token are refused.

`hexa` does not retry a failed deploy. If a deploy fails, run
`hexa pipelines versions PIPELINE` to check whether the version was created
before you try again.

## Start a run

```bash
hexa runs start import-data --version v4 --parameters params.json --dry-run
hexa runs start import-data --version v4 --parameters params.json --yes
hexa runs wait RUN_ID
```

`start` needs an exact version, and checks your parameters against it before
sending anything. Parameters work like in [local runs](local-runs.md#parameters).

`wait` prints status changes until the run ends. It exits 0 if the run succeeded
and 1 if it failed or was stopped, showing the last log lines. If it times out
(`--timeout`, 30 minutes by default), the run continues on OpenHEXA.

## Past runs

```bash
hexa runs list --pipeline import-data
hexa runs show RUN_ID
hexa runs logs RUN_ID
hexa runs logs RUN_ID --kind runtime --limit 80
```

Without `--pipeline`, `runs list` groups runs by pipeline. `runs show` gives the
status, the outputs and the dataset versions a run wrote, and tells you which code
version it used. To read that code:

```bash
hexa pipelines source import-data --version VERSION_ID --output ./debug
```

This extracts the files into a new folder. The code is not executed.

## Other commands

```bash
hexa pipelines list
hexa pipelines show import-data
hexa pipelines versions import-data
hexa pipelines description get import-data
hexa pipelines tags set import-data monthly dhis2
```

`tags set` replaces all tags at once.
