# Agent working rules

## Never push a red CI

Before `git push`, run `make check` (ruff lint + ruff format --check + pytest) and
confirm it exits 0. If any step fails, fix it locally first — do not push in the
hope of sorting it out later. The GitHub Actions workflow runs the exact same
`make check`, so a clean local run is the contract.

- If `ruff format --check` reports files, run `make format` and re-run `make check`.
- If tests fail, fix the test or the code it covers before pushing.
- If you introduced a new tool/dependency, update `pyproject.toml` and rerun
  `pip install -e ".[dev]"` so CI sees it.

This rule has no exceptions for "small" or "doc-only" changes — the CI job is
cheap to run locally and avoids red main.


## Repository search

See WI.md for repository search/index usage.
