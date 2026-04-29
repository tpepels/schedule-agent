#WI.md

AGENT RULE — read before exploring this repo:
  Before you reach for grep, find, ripgrep, ls, or Read to locate code,
  run `wi <term>` first. `wi` returns file:line landmarks from the
  repo-local thin index. It exists so agents do NOT scan the tree by
  default. Only fall back to grep/Read if `wi` returns nothing useful.

When to use `wi` (these triggers should fire *before* you grep or Read):
  Looking for a symbol, function, struct, class, method, trait, enum:
    wi IndexRecord
    wi build_index
    wi PromptService
  Looking for a constant, variable, or type by name:
    wi INDEX_SCHEMA_VERSION
  Looking for a CSS class, id, variable, or @keyframes:
    wi .headerNavigation -t css_class
    wi -t css_variable -- --paper-bg
  Looking for an HTML id / class / data attribute / tag:
    wi '#mainHeader' -t html_id
    wi data-testid -t data_attribute
  Looking for a markdown heading, link, checklist, TODO, or FIXME:
    wi 'Tests' -t section
    wi TODO -t todo
  Refine with -t <kind>, -l <ext>, -p <path>, -n <n>, -v (verbose).

Workflow:
  1. Run `build_index` once before exploring, and after structural changes.
  2. Use `wi <term>` to locate code; only Read files `wi` returned.
  3. If `wi` returns nothing, rerun `build_index` once and retry; only
     then fall back to grep/Read.
  4. For terms starting with `-`, use `wi -- <term>`, e.g.
     `wi -- --css-variable`.

If you found yourself reading a whole file to find a name, you should
have run `wi <name>` first. Next time, start with `wi`.

Search the repo-local thin code index and return file/line landmarks

Usage: wi [OPTIONS] <QUERY>

Arguments:
  <QUERY>
          Search term, e.g. HeaderNavigation, PromptService, --css-variable

Options:
  -t <KIND>
          Filter by indexed record kind. Common kinds: class, function, method, css_class, css_variable, html_id, html_class, html_tag, data_attribute, heading, checklist, link, todo, fixme, keyframes

  -l <EXT>
          Filter by file extension/language. Use extension-style values: py, rs, js, jsx, ts, tsx, css, html, md

  -p <PATH>
          Filter by path substring, e.g. src, tests, frontend/components

  -s <SOURCE>
          Filter by index source. Values are usually ctags or extras

  -n <N>
          Limit result count, e.g. -n 10

  -v
          Show verbose output with kind, language, source, and text

  -r <REPO>
          Directory inside the repository
          
          [default: .]

  -h, --help
          Print help

  -V, --version
          Print version
