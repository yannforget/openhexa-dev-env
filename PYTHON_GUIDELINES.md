# Python tooling & style

Python is the main language we use for data processing.

## Dependencies

- Add dependencies with `uv add <package>` (or `uv add --dev <package>` for dev tools).
  Never use `pip install` or edit dependencies in `pyproject.toml` by hand.

For discussion-only or documentation-only tasks, do not run Ruff or Pyrefly
unless explicitly requested. The checks below apply to Python code changes.

## Linting and formatting: Ruff

- Configuration is in `pyproject.toml` under `[tool.ruff]`. Do not modify it.
- Before finishing a task that changes Python code, run:
  - `uv run ruff format .`
  - `uv run ruff check .`
- Apply safe fixes with `uv run ruff check --fix`. Don't use `--unsafe-fixes`.
- To understand a rule, run `uv run ruff rule <CODE>` (e.g. `ruff rule TRY301`)
  or see https://docs.astral.sh/ruff/rules/

## Type checking: Pyrefly

- For Python code changes, run `uv run pyrefly check`. It must report zero errors.

## Suppressions

- Avoid using `# noqa`, `# ruff: ignore`, `# type: ignore`, or `# pyrefly: ignore`
  without identifying the rule you are ignoring.
- You can use `type: ignore` when working with untyped (or badly typed) libraries,
  but try to avoid it.

## Error handling

- Let exceptions propagate. Do not wrap code in try/except "just in case".
- Catch the narrowest exception type possible.
- Never catch `Exception` or `BaseException`, except at a top-level boundary
  (e.g. CLI entry point). There, use `logger.exception`
  and re-raise or return an explicit error.
- Never swallow errors: no `except: pass`, no `except: continue`, and no
  logging a warning and carrying on as if nothing happened.
- Never return a fallback value (`None`, `{}`, `[]`, `False`) to hide a failure
  unless the task explicitly asks for it.
- Re-raise with bare `raise`, or with `raise NewError(...) from err`.
- Call `subprocess.run(..., check=True)` and always pass a timeout to HTTP calls.

## Docstrings

- Use Google-style docstrings.
- Never repeat type information that is already in the annotations.
  Write `name: The user's display name.`, not `name (str): The user's display name.`
- Only list exceptions in `Raises` that the function raises deliberately.
- Keep it short: a one-line summary is enough for most functions. Add more
  only for non-obvious behavior, side effects, or important constraints.
- Document the parameters and non-obvious behavior of public functions that
  users may call directly.
- Use single backticks for inline code in docstrings.
- Private functions and methods (`_name`) need no docstring when their name
  and a quick read of their body say what they do. Otherwise give them a
  docstring, and rename them if a better name makes it unnecessary. One line
  is usually enough; for functions that branch on their inputs (e.g. by
  type), briefly describe what each case does.
- A function's behaviour, and the reason for any surprising part of it, goes
  in its docstring; inline comments are for a specific line or block.
- Don't describe what a library does by default, such as Pydantic validating
  by alias.
- Module docstrings: one or two sentences on the module's purpose.
  No lists of contents, no usage tutorials except when it contains a script.

## Comments

- Comments explain 'why', never 'what'. Don't comment code that is already readable.
  Module constants are the exception: give each a short comment saying what
  it holds, unless its name already says so.
- Don't write comments that describe your changes, they belong in commit messages
- No decorative section banners (`# ===== Helpers =====`)
- In general, comments are a maintenance burden so they should be used when they provide
  value.

## Data processing

- Use Polars, not Pandas.
- Prefer Parquet files with strict schemas rather than CSVs.

## Tests

Tests are code we'll have to maintain: write few, high-value ones.

- Test behavior through public interfaces, not implementation. Tests should
  survive refactors and fail only when behavior breaks.
- One test per important behavior.
- Don't test trivial code, or third-party libraries.
- Mock only when needed - don't create huge mocks for API requests that don't
  protect us when the upstream API change anyway.
- Mark tests that require a running dev/server instance with `@pytest.mark.integration`.
- Never weaken assertions, delete, or skip tests to make them pass.
