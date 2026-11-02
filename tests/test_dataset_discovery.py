"""Offline discovery presentation and workspace-safe lookup regressions."""

import json
import sys

import pytest
from rich.text import Text

from hexa.api import API, APIError
from hexa.cli import main
from hexa.config import Credentials

CREDENTIALS = Credentials("https://example.invalid", "selected", "synthetic-token")
DATASET_ID = "11111111-1111-4111-8111-111111111111"
OTHER_ID = "22222222-2222-4222-8222-222222222222"


def workspace_page(items):
    """Return a complete workspace-scoped link page."""
    return {
        "workspace": {
            "datasets": {
                "pageNumber": 1,
                "totalPages": 1,
                "totalItems": len(items),
                "items": items,
            }
        }
    }


@pytest.mark.parametrize("terminal", [False, True])
@pytest.mark.parametrize("width", [64, 300])
@pytest.mark.parametrize(
    ("description", "excerpt"),
    [
        (
            "# Population Estimates\n\nAnnual estimates.\nCoverage: national.",
            "Annual estimates. Coverage: national.",
        ),
        (
            "Population Estimates\n====================\n\nAnnual estimates.",
            "Annual estimates.",
        ),
        (
            "# Limitations\n\nUnderreporting is possible.",
            "# Limitations Underreporting is possible.",
        ),
        (None, ""),
        ("  \n ", ""),
        ("coverage " * 100, None),
        ("界" * 300, None),
        (
            "[bold]literal[/bold] synthetic-token",
            "[bold]literal[/bold] [TOKEN withheld]",
        ),
    ],
)
def test_compact_dataset_catalog_uses_bounded_literal_excerpts_without_changing_json(
    terminal, width, description, excerpt, tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "OPENHEXA_URL=https://example.invalid\nOPENHEXA_WORKSPACE=selected\nOPENHEXA_TOKEN=synthetic-token\n"
    )
    listing = {
        "items": [
            {
                "id": "link-id",
                "dataset": {
                    "id": DATASET_ID,
                    "slug": "population-estimates",
                    "name": "Population Estimates",
                    "workspace": {"slug": "source-workspace"},
                    "description": description,
                },
            }
        ],
        "complete": True,
        "pages_read": 1,
        "total_items": 1,
    }
    monkeypatch.setattr(API, "datasets", lambda *args: listing)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: terminal)
    monkeypatch.setenv("COLUMNS", str(width))
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert main(["datasets", "list"]) == 0
    captured = capsys.readouterr()
    lines = Text.from_ansi(captured.out).plain.splitlines()
    assert lines[:3] == [
        "Population Estimates",
        "Slug: population-estimates",
        "Workspace: source-workspace",
    ]
    if excerpt is not None:
        assert lines[3] == excerpt
        assert lines[4:] == (["", "1 dataset"] if excerpt else ["1 dataset"])
    else:
        assert len(lines[3]) <= 240 and lines[3].endswith("…")
        assert Text(lines[3]).cell_len <= width
        assert lines[4:] == ["", "1 dataset"]
    assert captured.out.endswith("1 dataset\n")
    assert captured.err == "" and "synthetic-token" not in captured.out
    assert DATASET_ID not in captured.out and "link-id" not in captured.out
    if terminal:
        assert "\x1b[1mPopulation Estimates\x1b[0m" in captured.out
        assert "\x1b[2mSlug: " in captured.out
    else:
        assert "\x1b[" not in captured.out
    assert main(["datasets", "list", "--json"]) == 0
    captured = capsys.readouterr()
    expected = json.loads(
        json.dumps(listing).replace("synthetic-token", "[TOKEN withheld]")
    )
    assert json.loads(captured.out) == expected and captured.err == ""


@pytest.mark.parametrize(
    ("count", "complete", "total", "reason", "summary"),
    [
        (0, True, 0, None, ""),
        (2, True, None, None, "\n2 datasets"),
        (2, False, 5, "limit", "\nShowing 2 of 5 datasets."),
        (
            2,
            False,
            None,
            "page_ceiling",
            "\nShowing 2 datasets; the total is unknown.",
        ),
    ],
)
def test_dataset_blocks_preserve_spacing_and_report_empty_or_partial_catalogs(
    count, complete, total, reason, summary, tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "OPENHEXA_URL=https://example.invalid\nOPENHEXA_WORKSPACE=selected\nOPENHEXA_TOKEN=synthetic-token\n"
    )
    listing = {
        "items": [
            {
                "dataset": {
                    "slug": f"dataset-{i}",
                    "name": "[bold]Literal name[/bold]" if i else None,
                    "workspace": {"slug": "owner"},
                    "description": "",
                }
            }
            for i in range(count)
        ],
        "complete": complete,
        "total_items": total,
        "incomplete_reason": reason,
    }
    monkeypatch.setattr(API, "datasets", lambda *args: listing)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    assert main(["datasets", "list"]) == 0
    captured = capsys.readouterr()
    expected = (
        (
            "dataset-0\nSlug: dataset-0\nWorkspace: owner\n\n"
            "[bold]Literal name[/bold]\nSlug: dataset-1\nWorkspace: owner\n"
            if count
            else "No datasets found.\n"
        )
        + summary
        + ("\n" if summary else "")
    )
    if reason == "limit":
        expected += "Use --all to see all of them.\n"
    elif reason == "page_ceiling":
        expected += "hexa stops after a fixed number of pages, even with --all.\n"
    assert captured.out == expected and captured.err == ""


@pytest.mark.parametrize("selector", ["population", "owner/population"])
def test_slug_lookup_filters_workspace_candidates_but_still_requires_an_exact_match(
    selector, monkeypatch
):
    requests = []
    dataset = {
        "id": DATASET_ID,
        "slug": "population",
        "workspace": {"slug": "owner"},
        "description": "Before",
    }
    links = [
        {"id": "link-id", "dataset": dataset},
        {
            "id": "other-link",
            "dataset": {
                "id": OTHER_ID,
                "slug": "population-extra",
                "workspace": {"slug": "owner"},
            },
        },
    ]

    def graphql(self, query, variables):
        requests.append((query, variables))
        assert "workspace(slug:$ws)" in query and "datasetLinkBySlug" not in query
        assert variables == {
            "ws": "selected",
            "query": "population",
            "page": 1,
            "size": 50,
        }
        return workspace_page(links)

    monkeypatch.setattr(API, "graphql", graphql)
    api = API(CREDENTIALS)
    result = api.dataset(selector)
    assert result["id"] == DATASET_ID and result["link_id"] == "link-id"
    assert result["workspace"]["slug"] == "owner" and len(requests) == 1
    dataset["description"] = "After"
    assert api.dataset(selector)["description"] == "After"
    assert len(requests) == 2


@pytest.mark.parametrize("selector", [DATASET_ID, "owner/" + DATASET_ID])
def test_uuid_lookup_checks_lightweight_workspace_inventory_before_fetching_one_dataset(
    selector, monkeypatch
):
    requests = []
    identity = {"id": DATASET_ID, "slug": "population", "workspace": {"slug": "owner"}}
    detail = {**identity, "description": "Full description", "latestVersion": None}

    def graphql(self, query, variables):
        requests.append((query, variables))
        if "query WorkspaceDatasets" in query:
            assert variables == {"ws": "selected", "query": None, "page": 1, "size": 50}
            assert all(
                field not in query
                for field in ("description", "latestVersion", "permissions")
            )
            return workspace_page([{"id": "link-id", "dataset": identity}])
        assert "query Dataset(" in query and variables == {"id": DATASET_ID}
        return {"dataset": detail}

    monkeypatch.setattr(API, "graphql", graphql)
    result = API(CREDENTIALS).dataset(selector)
    assert result == {**detail, "link_id": "link-id"} and len(requests) == 2


@pytest.mark.parametrize(
    ("links", "code"),
    [
        ([], "NOT_FOUND_OR_INACCESSIBLE"),
        (
            [
                {
                    "id": "link-1",
                    "dataset": {
                        "id": DATASET_ID,
                        "slug": "population",
                        "workspace": {"slug": "owner"},
                    },
                },
                {
                    "id": "link-2",
                    "dataset": {
                        "id": OTHER_ID,
                        "slug": DATASET_ID,
                        "workspace": {"slug": "owner"},
                    },
                },
            ],
            "AMBIGUOUS",
        ),
    ],
)
def test_uuid_shortcut_cannot_bypass_workspace_visibility_or_id_slug_ambiguity(
    links, code, monkeypatch
):
    requests = []

    def graphql(self, query, variables):
        requests.append(query)
        assert "query WorkspaceDatasets" in query
        return workspace_page(links)

    monkeypatch.setattr(API, "graphql", graphql)
    with pytest.raises(APIError) as error:
        API(CREDENTIALS).dataset(DATASET_ID)
    assert error.value.code == code and len(requests) == 1


def test_dataset_without_versions_can_be_shown_without_attempting_file_discovery(
    monkeypatch,
):
    requests = []
    dataset = {
        "id": DATASET_ID,
        "slug": "empty",
        "workspace": {"slug": "owner"},
        "latestVersion": None,
    }

    def graphql(self, query, variables):
        requests.append(query)
        assert "query WorkspaceDatasets" in query
        return workspace_page([{"id": "link-id", "dataset": dataset}])

    monkeypatch.setattr(API, "graphql", graphql)
    api = API(CREDENTIALS)
    assert api.dataset("empty")["latestVersion"] is None
    with pytest.raises(APIError) as error:
        api.files("empty")
    assert error.value.code == "NOT_FOUND_OR_INACCESSIBLE" and len(requests) == 2
