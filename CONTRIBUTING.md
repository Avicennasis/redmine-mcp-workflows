# Contributing to redmine-mcp-workflows

Thanks for considering a contribution. Bug reports, docs fixes, and small
improvements are all welcome.

## Dev setup

Requires Python 3.11+.

```bash
git clone https://github.com/avicennasis/redmine-mcp-workflows
cd redmine-mcp-workflows
python3 -m venv .venv
.venv/bin/pip install -e .[dev]
```

Or with [uv](https://docs.astral.sh/uv/) (a `uv.lock` is committed):

```bash
uv sync --extra dev
```

## Running the tests

```bash
pytest            # or: uv run pytest
```

Test conventions (see `tests/`):

- **No network in unit tests.** Tool tests use a `FakeClient` stand-in that
  returns canned responses keyed by `(method, path)` — see
  `tests/test_tools/test_issues.py` for the canonical pattern. A response
  value may be a single payload (static) or a list (a queue popped per
  call, last element reused).
- **One module per tool family** under `tests/test_tools/` (e.g.
  `test_issues.py`, `test_time_entries.py`, `test_wiki.py`). Core-layer
  tests (cache, config, validation, schema) live directly in `tests/`.
- **Async is automatic** — `asyncio_mode = "auto"` is set in
  `pyproject.toml`; plain `async def test_*` functions just work.
- **Shared fixtures** live in `tests/conftest.py` (notably `tmp_cache_dir`,
  which redirects `REDMINE_MCP_CACHE_DIR` to a per-test tmp dir).
- Tests that need a **live Redmine instance** must be marked
  `@pytest.mark.integration` (gated; not run by default in CI).

## Code style

[ruff](https://docs.astral.sh/ruff/) is the only linter, configured in
`pyproject.toml` (line length 100, rule sets `E F I N W UP B SIM`):

```bash
ruff check .      # or: uv run ruff check .
```

The tree should be ruff-clean before you open a PR.

## PR flow

1. Open or find an issue describing the change first — for anything beyond
   a trivial fix, agreeing on the approach up front saves everyone time.
2. Branch from `main`, keep the diff focused on one change.
3. Add or update tests; `pytest` must be green locally.
4. `ruff check .` must be clean.
5. Update `README.md` if the public tool surface changed (the tool list in
   the README is documentation of record).
6. Add a `CHANGELOG.md` entry under `[Unreleased]`.
7. Open the PR with a short summary of what changed and why; reference the
   issue.

## Code of Conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md).
Be respectful; assume good faith.
