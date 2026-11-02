# Contributor guide

## Project structure

- `src/hexa/`: Python package, Typer commands and local runner.
- `src/hexa/api/`: workspace-scoped client, independent of CLI presentation.
- `src/hexa/runtime/`: container bootstrap and SDK reporting adapter.
- `examples/webapp/`: token-free static webapp example.
- `tests/`: offline tests and opt-in API/container integration tests.
- `skills/`: the portable `openhexa` agent skill and its task-specific references.

## Development

Read [PYTHON_GUIDELINES.md](PYTHON_GUIDELINES.md) before changing Python code.
Install development dependencies with `uv sync --locked`. Run the CLI with
`uv run hexa` and build distributions with `uv build`.

For Python changes, run:

```bash
uv run pytest -q
uv run ruff format .
uv run ruff check .
uv run pyrefly check
```

Keep API logic separate from command parsing and presentation. Bundle runtime
helpers in the package and test installed wheels, not only editable imports.
Preserve private token transport, read-only mounts, local reporting, output
redaction and container exit status. Do not add implicit credential searches,
image pulls or service starts.

Preserve existing user changes. Do not commit, push or delete resources unless
asked. This repository is standalone; sibling OpenHEXA checkouts are not required.
Before changing another repository, read its contributor instructions. Do not
start services or change host/container configuration without explaining the
required changes and obtaining approval.

Reusable agent guidance is maintained in `skills/openhexa/`; see
`skills/README.md` for installation. Read the local `SKILL.md`
for OpenHEXA exploration, pipeline authoring, direct API work or webapps rather than
relying on an older globally installed copy. Keep skills portable and load references
only when needed. Do not modify global skills.

## Commit conventions

- Prefer squash-merging pull requests.
- Use semantic commit messages, such as `feat: add workspace listing` or
  `fix: handle missing config`. Scopes are optional.
- While the project is in early development, breaking changes do not need a `!` marker.
- Keep commit messages short and simple. Add a body only to explain intent or the
  “why” when it is not obvious from the code; do not restate what the diff shows.

## Integration tests and remote access

Integration tests are opt-in and never run by default. Copy
`tests/integration.example.json` to an ignored location (e.g. `.hexa/integration.json`),
point `credential_file` at a test-workspace `.env` and name existing resources: a small
dataset file, a connection and a pipeline with at least one run. Then select a suite:

```bash
export HEXA_INTEGRATION_CONFIG=.hexa/integration.json
uv run pytest -m "integration and api and not container" -q   # API only
uv run pytest -m "integration and container and not api" -q   # offline containers
uv run pytest -m integration -q                               # everything
```

Container tests need the pinned runtime image already in the local Podman cache.
They use the same Podman detection as `hexa run`; set `HEXA_TEST_PODMAN=direct` or
`host` to override it. Without
`HEXA_INTEGRATION_CONFIG`, API tests are
skipped. Use only the configured credentials; do not print tokens, connection
secrets or signed URLs in diagnostics.

Remote access is read-only unless the operator approves specific writes.
Documentation, metadata, tags and workspace `AGENTS.md` write tests each require
resource-specific approval. Permissions and historical plans do not authorize
changes. Do not deploy or execute cloud pipelines, bypass authentication, or call
external services merely because a connection exists.

Explain any required host/container configuration changes or service starts
before taking action. Keep local test fixtures under the ignored `.hexa/`
directory and do not modify unrelated resources.
