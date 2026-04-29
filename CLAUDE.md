@AGENTS.md

## Repository search

<!-- thinindex-repo-search-block: v1 -->

`wi` is an index of *named* things in this repo — functions, classes, methods, CSS classes/variables, HTML ids, section headings, TODO/FIXME — not full text or paths. Use `wi` whenever you'd grep for a name; use grep/rg/find directly for free text or paths.

- Run `wi --help` before your first repository search and treat its output as part of these instructions.
- Run `build_index` before broad discovery and after structural changes.
- Use `wi <name>` to find symbols, definitions, CSS classes, HTML ids, section headings, etc.
- For free text (string literals, comments, prose) or paths, use grep/rg/find directly — that's the right tool, not a fallback.
- Read only files returned by `wi` unless the result is insufficient.
- If `wi` misses a name you expect to exist, rerun `build_index` once and retry before grepping.
