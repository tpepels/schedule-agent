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

<!-- thinindex-repo-search-block: v2 -->

`wi` ("where is") is an index of *named* things in this repo — functions, classes, methods, CSS classes/variables, HTML ids, section headings, TODO/FIXME — not full text or paths. Use `wi <name>` whenever you'd grep for a name; use grep/rg/find directly for free text or paths.

- Run `wi --help` before your first repository search and treat its output as part of these instructions.
- Run `build_index` before broad discovery and after structural changes.
- Read only files returned by `wi` unless the result is insufficient.
- If `wi` misses a name you expect to exist, rerun `build_index` once and retry before grepping.
