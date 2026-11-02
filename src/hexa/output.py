"""Literal terminal presentation and fidelity-preserving explicit payload delivery."""

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from rich.console import Console
from rich.text import Text

from .io import save_document
from .security import Redactor


@dataclass
class Result:
    """One command result, optionally carrying an explicitly requested text payload."""

    data: Any
    payload: str | None = None
    output: Path | None = None
    sensitive_url: bool = False
    listing: "ListingKind | None" = None
    dataset_write: bool = False
    logs: bool = False
    version_download: bool = False
    doctor: bool = False
    version_diff: bool = False
    schema_type: bool = False
    pipeline_deploy: bool = False
    run_start: bool = False
    run_wait: bool = False
    run_show: bool = False


def safe_value(value: Any, token: str, redact_urls: bool = False) -> Any:
    """Protect known tokens everywhere; redact URLs only in diagnostics, not payloads."""
    if isinstance(value, str):
        return (
            Redactor(token).text(value)
            if redact_urls
            else value.replace(token, "[TOKEN withheld]")
        )
    if isinstance(value, dict):
        return {
            safe_value(k, token, redact_urls): safe_value(v, token, redact_urls)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [safe_value(v, token, redact_urls) for v in value]
    return value


def render(result: Result, json_mode: bool, token: str) -> None:
    """Render exactly one JSON value or literal text; payloads never enter diagnostics."""
    data = safe_value(result.data, token)
    payload = None if result.payload is None else safe_value(result.payload, token)
    if result.output is not None:
        if payload is None:
            raise ValueError("No document payload")
        save_document(result.output, payload)
        data = {**data, "output": str(result.output.resolve())}
    if json_mode:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    elif result.dataset_write:
        dataset = data["dataset"]
        print(
            f"Dataset: {dataset['workspace']['slug']}/{dataset['slug']} "
            f"({dataset['id']}) — {dataset['name']}"
        )
        if "version" in data:
            version = data["version"]
            print(f"Version: {version['name']} ({version['id']})")
            print(f"Uploaded files: {data['uploaded_file_count']}")
        else:
            print("Dataset created without uploading files.")
            if dataset.get("latestVersion"):
                version = dataset["latestVersion"]
                print(f"Initial version: {version['name']} ({version['id']})")
    elif payload is not None:
        if result.output is None:
            sys.stdout.write(payload)
            sys.stdout.flush()
        if data.get("truncated"):
            print("Document truncated at client limit", file=sys.stderr)
    elif result.sensitive_url:
        print(data["url"])
    elif isinstance(data.get("documents"), list) and "content_notice" in data:
        print(_context(data))
    elif isinstance(data.get("items"), list) and "complete" in data:
        _listing(data, result.listing)
    elif result.logs:
        _logs(data)
    elif result.version_diff:
        _diff(data)
    elif result.doctor:
        _doctor(data)
    elif result.schema_type:
        _schema_type(data)
    elif result.pipeline_deploy:
        _deploy(data)
    elif result.run_start:
        _run_start(data)
    elif result.run_wait:
        _run_wait(data)
    elif result.run_show:
        _run_summary(data)
    elif result.version_download:
        version = data["version"]
        print(
            f"Version {version['name']} ({version['id']}): {data['downloaded']} "
            f"downloaded, {data['skipped']} skipped (same size), "
            f"{_file_size(data['bytes'])} in {data['directory']}"
        )
    else:
        print(_human(data))


def _diff(data: dict[str, Any]) -> None:
    """One line per difference, `diff`-style markers, then a count."""
    old, new = data["versions"]
    markers = {
        "added": "+",
        "removed": "-",
        "size_changed": "~",
        "content_changed": "~",
        "same_size_unverified": "?",
    }
    for change in data["changes"]:
        size = change["bytes"]
        if isinstance(size, list):
            before, after = map(_file_size, size)
            if before == after:
                before, after = (f"{value} B" for value in size)
            shown = f"{before} → {after}"
        else:
            shown = _file_size(size)
        label = change["change"].replace("_", " ")
        print(f"{markers[change['change']]} {change['path']}  ({label}, {shown})")
    summary = f"{old['name']} → {new['name']}: "
    if data["identical"]:
        summary += "identical"
    else:
        summary += f"{len(data['changes'])} differing"
    if data["content_checked"]:
        summary += f", {data['identical_files']} same content"
    elif any(c["change"] == "same_size_unverified" for c in data["changes"]):
        summary += "; same-size files unverified, use --content to hash them"
    print(summary)


def _run_start(data: dict[str, Any]) -> None:
    pipeline, version = data["pipeline"], data["version"]
    print(
        f"Pipeline {pipeline['code']} ({pipeline['id']}), version "
        f"{version['name']} ({version['id']})"
    )
    print(f"Inputs: {', '.join(data['parameters']) or 'none'}")
    print(f"Defaults used: {', '.join(data['defaults_used']) or 'none'}")
    if data["unchecked"]:
        print(f"Not checked offline: {', '.join(data['unchecked'])}")
    if data["dry_run"]:
        print("Dry run: nothing was launched. Rerun with --yes to launch.")
        return
    print(f"Run {data['run']['id']} ({data['run']['status']})")
    print(f"Follow it with: hexa runs wait {data['run']['id']}")


def _run_wait(data: dict[str, Any]) -> None:
    _run_summary(data)
    if "log_tail" in data:
        print("\nLast runtime log lines")
        for line in data["log_tail"]:
            print(f"  {line}")


def _run_summary(data: dict[str, Any]) -> None:
    """A few labelled lines, then outputs and dataset versions, then a next step."""
    pipeline = data.get("pipeline") or {}
    version = data.get("version") or {}
    status = data.get("status") or "?"
    if data.get("timed_out"):
        status += " (hexa stopped waiting; the run continues)"
    started = _date(data.get("executionDate"))
    took = data.get("duration")
    print(f"Run {data['id']}")
    print(
        f"Pipeline: {pipeline.get('code') or pipeline.get('id')}, "
        f"version {_version_name(version)} ({version.get('id')})"
    )
    print(f"Status: {status}")
    if data.get("progress") is not None and status not in ("success", "failed"):
        print(f"Progress: {data['progress']}%")
    print(
        f"Started: {started}"
        + (f", took {_seconds(took)}" if took is not None else "")
        + " (UTC)"
    )
    outputs = [_output_row(item) for item in data.get("outputs") or []]
    if outputs:
        print("\nOutputs")
        _lines(_console(), [[Text(c) for c in row] for row in outputs], (1,), 2)
    versions = data.get("datasetVersions") or []
    if versions:
        print("\nDataset versions")
        print(
            _rows(
                [
                    [item["dataset"]["slug"], f"{item['name']} ({item['id']})"]
                    for item in versions
                ],
                indent=2,
            )
        )
    if (status == "failed" or data.get("hasErrorMessages")) and "log_tail" not in data:
        print(f"\nRead the logs with: hexa runs logs {data['id']}")


def _output_row(item: dict[str, Any]) -> list[str]:
    """Workspace files, other outputs and database tables, in plain words."""
    if "key" in item:
        size = item.get("size")
        return [item["key"], _file_size(size) if size is not None else ""]
    if "tableName" in item:
        return [item["tableName"], "database table"]
    name = item.get("outputName") or item.get("uri") or "?"
    detail = " ".join(str(v) for v in (item.get("type"), item.get("uri")) if v)
    return [name, detail if detail != name else ""]


def _deploy(data: dict[str, Any]) -> None:
    """Manifest and target first, then either the dry-run note or the new identities."""
    target = data["target"]
    pipeline = data["pipeline"]
    if "new" in target:
        print(f'Target: new pipeline "{target["new"]}" in {data["workspace"]}')
    else:
        version = target.get("currentVersion") or {}
        current = version.get("name") or version.get("versionName")
        print(
            f"Target: pipeline {target['code']} ({target['id']}), current version "
            f"{current or 'none'}; {len(target['versions'])} versions"
        )
    print(f"Version name: {data['version_name']}")
    print(
        f"Files ({len(data['files'])}, {_file_size(data['bytes'])}, sent as "
        f"{data['transport']}; request {_file_size(data['payload_bytes'])}):"
    )
    for item in data["files"]:
        print(f"  {item['path']}  ({_file_size(item['bytes'])})")
    print(f"SHA-256 (combined): {data['sha256']}")
    timeout = pipeline["timeout"]
    print(f"Timeout: {f'{timeout} s' if timeout is not None else 'server default'}")
    parameters = ", ".join(
        f"{p['code']}: {p['type']}{'[]' if p['multiple'] else ''}"
        f"{'' if p['required'] else '?'}"
        for p in pipeline["parameters"]
    )
    print(f"Parameters (parsed by the server on upload): {parameters or 'none'}")
    for warning in data["warnings"]:
        print(f"Warning: {warning}")
    if data["dry_run"]:
        print("Dry run: nothing was deployed. Rerun with --yes to deploy.")
        return
    version = data["version"]
    print(f"Deployed pipeline {pipeline['code']} ({pipeline['id']})")
    print(f"Version {version['name']} #{version['versionNumber']} ({version['id']})")


def _schema_type(data: dict[str, Any]) -> None:
    """SDL-like listing: one line per field or value, description as a comment."""
    print(f"{data['kind'].lower().replace('_', ' ')} {data['name']}")
    if data["description"]:
        print(f"  # {' '.join(data['description'].split())}")
    for field in data.get("fields", []):
        args = ", ".join(f"{a['name']}: {a['type']}" for a in field.get("args", []))
        line = f"  {field['name']}{f'({args})' if args else ''}: {field['type']}"
        if "default" in field:
            line += f" = {field['default']}"
        notes = [" ".join(field["description"].split())] if field["description"] else []
        if field.get("deprecated"):
            notes.insert(0, "deprecated")
        print(line + (f"  # {'; '.join(notes)}" if notes else ""))
    for value in data.get("values", []):
        notes = [" ".join(value["description"].split())] if value["description"] else []
        if value.get("deprecated"):
            notes.insert(0, "deprecated")
        print(f"  {value['name']}" + (f"  # {'; '.join(notes)}" if notes else ""))
    if "possible_types" in data:
        print("  = " + " | ".join(data["possible_types"]))


def _doctor(data: dict[str, Any]) -> None:
    """One line per check, then the container contract and any hosted-image notes."""
    for check in data["checks"]:
        print(f"{check['status']:<9} {check['name']}: {check['detail']}")
        if "hint" in check:
            print(f"          Hint: {check['hint']}")
    container = data["container"]
    version = f" = {container['image_version']}" if container["image_version"] else ""
    print(f"\nContainer: {container['image']}{version}")
    print(
        f"  Limits: {container['cpus']} CPUs, {container['memory']} memory; network: {container['network']}"
    )
    print(f"  Token: {container['token']}")
    for mount in container["mounts"]:
        label = (
            f", label {mount['selinux_label']}" if mount.get("selinux_label") else ""
        )
        print(f"  {mount['target']:<14} {mount['host_path']} ({mount['mode']}{label})")
    for key, value in container["environment"].items():
        print(f"  {key}={value}")
    hosted = data.get("hosted_image")
    if hosted:
        print(f"\nHosted image ({hosted['source']}): {hosted['image']}")
        for key in ("note", "tags_url", "dockerfile_url", "server_url"):
            if key in hosted:
                print(f"  {hosted[key]}")
        print(
            f"  Local image: {hosted['local_image']} = {hosted['local_image_version']}"
        )


def _logs(data: dict[str, Any]) -> None:
    """Print log lines verbatim; empty messages stay empty rather than "None"."""
    pipeline = data["pipeline"]
    print(f"Run {data['run_id']} ({pipeline['code']}), {data['kind']}")
    if not data["items"]:
        print("No log messages yet." if data["kind"] == "messages" else "No logs yet.")
    for item in data["items"]:
        if isinstance(item, dict):
            print(
                f"{item.get('timestamp') or ''}  {item.get('priority') or ''}  "
                f"{item.get('message') or ''}".rstrip()
            )
        else:
            print(item)
    if data["truncated"]:
        print("Earlier lines omitted; raise --limit to see more.", file=sys.stderr)


ListingKind = Literal[
    "connections",
    "datasets",
    "dataset_versions",
    "files",
    "pipelines",
    "pipeline_versions",
    "runs",
    "webapps",
]
PAGINATION_KEYS = (
    "complete",
    "pages_read",
    "total_items",
    "pagination",
    "incomplete_reason",
    "coverage",
    "pipelines_complete",
    "pipeline_ceiling",
    "ordering",
)
NOUNS = {
    "connections": "connection",
    "datasets": "dataset",
    "dataset_versions": "version",
    "files": "file",
    "pipelines": "pipeline",
    "pipeline_versions": "version",
    "runs": "run",
    "webapps": "webapp",
}


def _listing(data: dict[str, Any], kind: ListingKind | None = None) -> None:
    """Blocks or one line per item, never a bordered table, then a count sentence."""
    console = _console()
    items = data["items"]
    noun = NOUNS.get(kind or "", "item")
    plural = noun + "s"
    if kind == "datasets":
        _blocks(console, [_dataset_block(i["dataset"]) for i in items], plural)
    elif kind == "pipelines":
        _blocks(console, [_pipeline_block(i) for i in items], plural)
    elif kind == "connections":
        _blocks(console, [_connection_block(i) for i in items], plural)
    elif kind == "webapps":
        _blocks(console, [_webapp_block(i) for i in items], plural)
    elif kind in ("runs", "dataset_versions", "pipeline_versions", "files"):
        heading = _heading(data, kind)
        if heading:
            console.print(Text(heading, style="dim"))
        if kind == "runs" and not data.get("pipeline") and items:
            _runs_by_pipeline(console, items)
        elif items:
            rows = [_line(item, kind) for item in items]
            _lines(console, rows, right=(1,) if kind == "files" else ())
        else:
            console.print(f"No {plural} found.")
    else:
        print(_human({k: v for k, v in data.items() if k not in PAGINATION_KEYS}))
    _summary(data, noun, plural)


def _summary(data: dict[str, Any], noun: str, plural: str) -> None:
    count = len(data["items"])
    total = data.get("total_items")
    if data["complete"]:
        total = count if total is None else total
        if total:
            print(f"\n{total} {noun if total == 1 else plural}")
        return
    if data.get("pipelines_complete") is False or any(
        item.get("total_items") is None for item in data.get("coverage", [])
    ):
        total = None
    shown = f"{count} {noun if count == 1 else plural}"
    print(
        f"\nShowing {shown}; the total is unknown."
        if total is None
        else f"\nShowing {count} of {total} {plural}."
    )
    reason = data.get("incomplete_reason")
    if reason == "page_ceiling":
        print("hexa stops after a fixed number of pages, even with --all.")
    elif reason == "pipeline_ceiling":
        print(
            f"Only the first {data['pipeline_ceiling']} pipelines were checked. "
            "Use --pipeline PIPELINE to see the runs of one pipeline."
        )
    elif reason == "limit":
        print("Use --all to see all of them.")


Block = tuple[str, list[tuple[str, str]], str]


def _blocks(console: Console, blocks: list[Block], plural: str) -> None:
    """Bold title, dimmed labels, one-line dimmed excerpt, blank line between."""
    if not blocks:
        console.print(f"No {plural} found.")
    for index, (title, fields, excerpt) in enumerate(blocks):
        if index:
            console.print()
        console.print(Text(title, style="bold"))
        for label, value in fields:
            console.print(Text.assemble((label + ": ", "dim"), value))
        if excerpt:
            console.print(Text(excerpt, style="dim"), no_wrap=True, overflow="ellipsis")


def _dataset_block(dataset: dict[str, Any]) -> Block:
    owner = (dataset.get("workspace") or {}).get("slug") or "unknown source"
    return (
        dataset.get("name") or dataset["slug"],
        [("Slug", dataset["slug"]), ("Workspace", owner)],
        _description_excerpt(dataset),
    )


def _pipeline_block(pipeline: dict[str, Any]) -> Block:
    fields = [("Code", pipeline.get("code") or pipeline["id"])]
    version = pipeline.get("currentVersion")
    fields.append(("Version", _version_name(version) if version else "none"))
    tags = ", ".join(t["name"] for t in pipeline.get("tags") or [])
    if tags:
        fields.append(("Tags", tags))
    return (
        pipeline.get("name") or pipeline.get("code") or pipeline["id"],
        fields,
        _description_excerpt(pipeline),
    )


def _connection_block(connection: dict[str, Any]) -> Block:
    fields = [("Slug", connection.get("slug") or connection["id"])]
    if connection.get("type"):
        fields.append(("Type", str(connection["type"])))
    codes = ", ".join(f["code"] for f in connection.get("fields") or [])
    if codes:
        fields.append(("Fields", codes))
    return (
        connection.get("name") or connection.get("slug") or connection["id"],
        fields,
        _description_excerpt(connection),
    )


def _webapp_block(app: dict[str, Any]) -> Block:
    fields = [("Slug", app.get("slug") or app["id"])]
    if app.get("type"):
        fields.append(("Type", str(app["type"]).lower()))
    if "isPublic" in app:
        fields.append(("Visibility", "public" if app["isPublic"] else "private"))
    if app.get("serveUrl"):
        fields.append(("URL", app["serveUrl"]))
    return (app.get("name") or app.get("slug") or app["id"], fields, "")


def _heading(data: dict[str, Any], kind: str) -> str:
    """One line naming the parent resource, instead of a nested record."""
    if kind == "runs":
        pipeline = data.get("pipeline")
        return f"Runs of {pipeline['code']}" if pipeline else ""
    if kind == "pipeline_versions":
        pipeline = data.get("pipeline") or {}
        return f"Versions of {pipeline.get('code') or pipeline.get('id')}"
    dataset = data.get("dataset") or {}
    owner = (dataset.get("workspace") or {}).get("slug")
    name = dataset.get("slug") or dataset.get("id") or ""
    name = f"{owner}/{name}" if owner else name
    if kind == "files":
        version = data.get("version") or {}
        if not version:
            return f"Files of {name}"
        return f"Files of {name}, version {version.get('name')} ({version.get('id')})"
    return f"Versions of {name}"


def _version_name(version: dict[str, Any]) -> str:
    name = version.get("name") or version.get("versionName")
    number = version.get("versionNumber")
    if name is None:
        return str(number) if number is not None else "?"
    return name


def _runs_by_pipeline(console: Console, items: list[dict[str, Any]]) -> None:
    """Group workspace-wide history under each pipeline code, most recent first.

    Pipelines are ordered by their latest run, and runs keep the listing's order.
    Columns are aligned across groups so statuses and dates still line up.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        pipeline = item.get("pipeline") or {}
        groups.setdefault(pipeline.get("code") or pipeline.get("id") or "?", []).append(
            item
        )
    rows = {code: [_line(run, "runs") for run in runs] for code, runs in groups.items()}
    every = [row for group in rows.values() for row in group]
    widths = [max(row[i].cell_len for row in every) for i in range(len(every[0]))]
    for index, (code, group) in enumerate(rows.items()):
        if index:
            console.print()
        console.print(Text(code, style="bold"))
        for row in group:
            padded = [
                cell if i == len(row) - 1 else cell + " " * (widths[i] - cell.cell_len)
                for i, cell in enumerate(row)
            ]
            console.print(Text("  ") + Text("  ").join(padded))


def _line(item: dict[str, Any], kind: str) -> list[Text]:
    if kind == "files":
        return [
            Text(item.get("path") or item["filename"]),
            Text(_file_size(item.get("size")), style="dim"),
        ]
    if kind == "runs":
        status = item.get("status") or "?"
        style = {"success": "green", "failed": "red", "stopped": "yellow"}.get(
            status, ""
        )
        cells = [Text(item["id"]), Text(status, style=style)]
        version = item.get("version")
        cells.append(Text(_version_name(version) if version else "?"))
        cells.append(Text(_date(item.get("executionDate")), style="dim"))
        cells.append(Text(_seconds(item.get("duration")), style="dim"))
        return cells
    cells = [Text(item["id"]), Text(_version_name(item))]
    if kind == "pipeline_versions" and item.get("versionNumber") is not None:
        cells.append(Text(f"#{item['versionNumber']}", style="dim"))
    cells.append(Text(_date(item.get("createdAt")), style="dim"))
    return cells


def _console() -> Console:
    return Console(
        markup=False,
        highlight=False,
        force_terminal=sys.stdout.isatty(),
        soft_wrap=True,
    )


def _lines(
    console: Console,
    rows: list[list[Text]],
    right: tuple[int, ...] = (),
    indent: int = 0,
) -> None:
    """Align cells with two spaces; nothing is truncated, long lines just wrap."""
    widths = [max(row[i].cell_len for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        line = Text(" " * indent)
        for index, (cell, width) in enumerate(zip(row, widths, strict=True)):
            if index:
                line.append("  ")
            pad = " " * (width - cell.cell_len)
            last = index == len(row) - 1
            if index in right:
                line.append(pad)
                line.append_text(cell)
            else:
                line.append_text(cell)
                if not last:
                    line.append(pad)
        console.print(line)


def _date(value: Any) -> str:
    """`2026-11-02T17:35:11.678Z` → `2026-11-02 17:35` (UTC, as sent by the API)."""
    if not isinstance(value, str) or len(value) < 16:
        return "?" if value is None else str(value)
    return value[:10] + " " + value[11:16]


def _seconds(value: Any) -> str:
    if value is None:
        return "?"
    seconds = int(value)
    if seconds < 60:
        return f"{seconds} s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min {seconds} s" if seconds else f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if minutes else f"{hours} h"


def _description_excerpt(dataset: dict[str, Any], limit: int = 240) -> str:
    text = (dataset.get("description") or "").strip()
    lines = text.splitlines()
    if lines:
        title = re.sub(r"^#{1,6}\s+|\s+#+$", "", lines[0]).strip()

        def normalize(value: str) -> str:
            return re.sub(r"[\W_]+", " ", value).strip().casefold()

        repeated_title = normalize(title) in {
            normalize(dataset.get(key) or "") for key in ("name", "slug")
        }
        if repeated_title and re.match(r"^#{1,6}\s+", lines[0]):
            lines = lines[1:]
        elif (
            repeated_title
            and len(lines) > 1
            and re.fullmatch(r"\s*(?:=+|-+)\s*", lines[1])
        ):
            lines = lines[2:]
    excerpt = " ".join(" ".join(lines).split())
    if not excerpt:
        return ""
    if len(excerpt) <= limit:
        return excerpt
    shortened = excerpt[: limit - 1]
    if " " in shortened:
        shortened = shortened.rsplit(" ", 1)[0]
    return shortened + "…"


def _file_size(value: int | str | None) -> str:
    if value is None:
        return "Not available"
    size = int(value)
    if size < 1024:
        return f"{size} B"
    scaled = float(size)
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB")
    index = 0
    while scaled >= 1024 and index < len(units) - 1:
        scaled /= 1024
        index += 1
    return f"{scaled:.1f} {units[index]}"


def _label(key: str) -> str:
    special = {"id": "ID", "url": "URL", "dockerImage": "Docker image"}
    return (
        special.get(key)
        or re.sub(r"(?<=[a-z])(?=[A-Z])", " ", key).replace("_", " ").capitalize()
    )


def _scalar(value: Any, key: str = "") -> str:
    if key == "duration" and isinstance(value, (int, float)):
        return _seconds(value)
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if key == "dockerImage" and not value:
        return "Not configured"
    if value is None:
        return "Not available"
    if value == "":
        return "None"
    return str(value)


def _rows(rows: list[list[str]], indent: int = 0) -> str:
    """Align plain-text cells, including multiline values, without terminal markup."""
    widths = [
        max(len(line) for row in rows for line in row[i].split("\n"))
        for i in range(len(rows[0]))
    ]
    lines = []
    for row in rows:
        cells = [cell.split("\n") for cell in row]
        for index in range(max(map(len, cells))):
            lines.append(
                " " * indent
                + "  ".join(
                    (cell[index] if index < len(cell) else "").ljust(width)
                    for cell, width in zip(cells, widths, strict=True)
                ).rstrip()
            )
    return "\n".join(lines)


def _human(value: Any, indent: int = 0) -> str:
    prefix = " " * indent
    if isinstance(value, dict):
        rows = []
        sections = []
        documents = []
        ordered = [key for key in ("name", "slug", "code", "id") if key in value]
        ordered.extend(key for key in value if key not in ordered)
        for key in ordered:
            if key == "duration_unit":
                continue
            item = value[key]
            label = _label(key)
            if key in ("description", "text"):
                text = "(empty)" if item in (None, "") else str(item)
                documents.append(f"{prefix}{label}\n{prefix}{'─' * len(label)}\n{text}")
            elif key == "tags" and isinstance(item, list):
                tags = ", ".join(
                    str(tag["name"]) if isinstance(tag, dict) else str(tag)
                    for tag in item
                )
                rows.append([label, tags or "None"])
            elif isinstance(item, (dict, list)) and item:
                sections.append(f"{prefix}{label}\n{_human(item, indent + 2)}")
            else:
                rows.append(
                    [
                        label,
                        "None"
                        if isinstance(item, (dict, list))
                        else _scalar(item, key),
                    ]
                )
        return "\n\n".join(
            part
            for part in (_rows(rows, indent) if rows else "", *sections, *documents)
            if part
        )
    if isinstance(value, list):
        if not value:
            return prefix + "None"
        if all(isinstance(item, dict) and item for item in value) and all(
            not isinstance(cell, (dict, list))
            for item in value
            for cell in item.values()
        ):
            columns = list(dict.fromkeys(key for item in value for key in item))
            return _rows(
                [[_label(key) for key in columns]]
                + [[_scalar(item.get(key), key) for key in columns] for item in value],
                indent,
            )
        return "\n\n".join(_human(item, indent) for item in value)
    return prefix + _scalar(value)


def _context(data: dict[str, Any]) -> str:
    parts = [
        _human(
            {
                key: value
                for key, value in data.items()
                if key not in ("documents", "content_notice")
            }
        ),
        "Notice: " + data["content_notice"],
    ]
    for document in data["documents"]:
        source = document["source"]
        if not document["present"]:
            parts.append(f"{source}  Not present")
            continue
        metadata = {
            key: document[key]
            for key in ("updated_at", "truncated")
            if document.get(key) is not None
        }
        identity = {
            key: value
            for key, value in document["identity"].items()
            if value is not None and value != data.get(key)
        }
        if identity:
            metadata["identity"] = identity
        parts.append(
            f"{source}\n{'─' * len(source)}\n{_human(metadata)}\n\n"
            + (document["text"] or "(empty)")
        )
    return "\n\n".join(parts)
