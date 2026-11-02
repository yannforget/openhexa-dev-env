# Datasets

## Find and download

```bash
hexa datasets list
hexa datasets show population-estimates
hexa datasets versions population-estimates
hexa datasets files population-estimates --version 2025-06
```

You can refer to a dataset by slug or UUID. For a dataset shared from another
workspace, use `source-workspace/slug`. Without `--version`, you get the latest
version.

Download one file, or a whole version:

```bash
hexa datasets download population-estimates estimates.parquet
hexa datasets download population-estimates --version 2025-06 --dir data/
```

`hexa` never overwrites a file. With `--dir`, it skips files that are already
there with the right size, so you can rerun after a failure (remove the partial
file first). Add `--json` to get a list of files with their SHA-256, which works
as a record of what you used.

## Compare two versions

```bash
hexa datasets diff population-estimates 2025-05 2025-06
hexa datasets diff population-estimates 2025-05 2025-06 --content
```

By default this only compares file names and sizes. With `--content`, files with
the same size are hashed to check whether their content changed. Nothing is written
to disk.

## Publish a new version

```bash
hexa datasets create "Monthly sales" --description "Sales by region."
hexa datasets upload monthly-sales sales.csv regions.csv --name 2026-06 --changelog "June update"
hexa datasets upload monthly-sales --dir build/ --name 2026-06
```

Before you upload:

- **Each upload creates a new version with only the files you give it.** Files
  from the previous version are not copied.
- **Uploads aren't atomic.** If an upload fails, the new version may contain
  only some of the files. The error lists the files that were uploaded. Check
  the version with `hexa datasets files` before you try again.
- **Running upload again creates another version.** It does not continue the
  failed upload.

With `--dir`, files keep their relative paths, and `hexa` refuses `.env` files
or anything that contains your token. Use `--last FILE` to upload a file last,
for example a file that signals the upload is complete.

## Descriptions and metadata

```bash
hexa datasets description get population-estimates
hexa datasets description set population-estimates --file description.md
hexa datasets metadata list population-estimates
hexa datasets metadata set population-estimates source --value "National census"
```

Metadata can target a version (`--version`) or a file (`--file`). For a shared
dataset, these changes affect the original dataset, not just your workspace.
