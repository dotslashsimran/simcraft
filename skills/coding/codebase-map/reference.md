# Codebase map: reference

Depth for each step of SKILL.md: what each signal tells you, how the scripts decide things and where they are wrong, tracing recipes per stack, and the MAP.md template.

## Contents
- [Why this order](#why-this-order)
- [Orientation signals](#orientation-signals)
- [Reading the data model](#reading-the-data-model)
- [deps.py: how it works and its limits](#depspy-how-it-works-and-its-limits)
- [hotspots.py: how it works and its limits](#hotspotspy-how-it-works-and-its-limits)
- [Patterns to look for in the results](#patterns-to-look-for-in-the-results)
- [Tracing recipes](#tracing-recipes)
- [Load-bearing and dead code](#load-bearing-and-dead-code)
- [Building the glossary](#building-the-glossary)
- [MAP.md template](#mapmd-template)
- [Rendering the diagram](#rendering-the-diagram)
- [Time budget](#time-budget)

## Why this order

Each step costs more than the one before it and makes the next one cheaper.

1. Orientation signals take minutes and tell you the shape: language, framework, how it starts, what it depends on.
2. The data model tells you the nouns. With the nouns, function names start to mean something.
3. Imports tell you the structure the code actually has, which is often not the structure the folder names suggest.
4. Git history tells you where people spend their time. In most repos a handful of files take most of the changes and most of the bugs. Those are the files worth reading closely.
5. Running one path proves or breaks everything above. Static reading misses dependency injection, decorators that register handlers, middleware, plugin loading, monkey patching and configuration that picks the code path at runtime. One run shows the truth.

Skipping straight to step 5 works for small repos. On a large repo it is the steps before it that tell you which path is worth tracing.

## Orientation signals

| Signal | What it tells you | Watch out for |
|---|---|---|
| README, CONTRIBUTING, AGENTS.md, CLAUDE.md, ARCHITECTURE.md | Intended purpose, setup, conventions | Often out of date. Trust CI over README for commands |
| CI config (`.github/workflows`, `.gitlab-ci.yml`) | The build, lint and test commands that are known to pass, supported versions, release process | Matrix builds hide which version is the main one |
| Package manifests (`package.json`, `pyproject.toml`, `go.mod`, `Cargo.toml`) | Frameworks, entry scripts, workspaces (monorepo), test runner | A dependency listed is not a dependency used. `deps.py` shows what is actually imported |
| Lockfiles | Exact versions | Do not read them; only check which package manager they imply |
| Dockerfile, compose files, Procfile | How it runs in production: base image, ports, start command, the services next to it (database, cache, queue) | Dev compose files may differ from production |
| Deploy config (`vercel.json`, `fly.toml`, `serverless.yml`, Helm, Terraform) | Where it runs, cron jobs, regions, env per stage | Infrastructure may live in another repo |
| Migrations | The real database schema and its history | Some teams edit the schema by hand; check for a schema dump too |
| Routes and CLI commands | The public surface: everything a user can trigger | File-system routing (Next.js `app/`, SvelteKit `src/routes`) has no route table in code |
| `.env.example` and env reads in code | Configuration and outside services | A var read in code but missing from the template is a classic first-day failure |
| Tests | How behaviour is specified, which areas are trusted | Few tests near a hotspot means changes there are risky |

`entrypoints.py` finds most of these by regex. It misses routes registered in loops or from config, handlers wired by dependency injection, and jobs defined in another repo or in a cloud console. When the route list is empty for an app that obviously serves HTTP, look for a router file by name and grep for the framework's registration call.

## Reading the data model

- Start from the **oldest migration** to see the original core tables, then read the **newest five** to see what is changing now.
- For each core entity, write: what it represents in plain words, its key fields, what owns it (foreign keys), and which code writes it (grep for the model name next to `save`, `create`, `insert`, `update`).
- No database? The data model is still there: request and response shapes (pydantic, zod, TS types, protobuf), files on disk (JSON, YAML), or state in an outside service. `entrypoints.py` counts data shapes per file to point you at them.
- Draw an ER diagram in Mermaid only if there are more than four entities with relationships. Otherwise a short list is clearer.

## deps.py: how it works and its limits

**Parsing.** Python uses the `ast` module, so imports inside strings and comments are ignored. Each import is tagged as `runtime` (module level), `lazy` (inside a function, often used on purpose to break a cycle) or `type` (inside `if TYPE_CHECKING:`). Files that use syntax newer than the running Python fall back to regexes. JS/TS and Go use regexes after removing comments. TS `import type` is tagged `type`. Dynamic `import()` is tagged `lazy`.

**Resolution.**
- Python: relative imports by package depth; absolute imports against source roots (the parent of every top-level package, plus every `src/`), with the importer's own folder tried first because scripts import their neighbours. Namespace packages (folders of `.py` files with no `__init__.py`) resolve to the folder. A bare name that matches a file somewhere in the repo but cannot be resolved is shown as `local file, sys.path`: the code edits `sys.path` to load it.
- JS/TS: relative paths with extension and `index` probing, TS ESM imports written as `./x.js` that point at `x.ts`, `tsconfig`/`jsconfig` `paths` and `baseUrl` (following relative `extends`), workspace package names from every `package.json`, and a fallback for the common `@/` and `~/` aliases.
- Go: imports that start with a `go.mod` module path become edges to that package folder.

**Collapsing to folders.** The file graph is too big to look at, so files are grouped into folders. Adaptive mode (the default) starts with one box and keeps splitting the biggest folder into its subfolders while the box count stays under `--max-nodes` (18). Folders under 10 files are not split. When a folder has more subfolders than fit, only the biggest are split off and the rest stay in a box labelled `(rest)`. Use `--depth N` for fixed depth, or `--under path` to zoom in.

**Layers.** Folders in a runtime cycle are merged; the remaining graph is a DAG. Level 0 imports nothing internal (foundation code), each higher level only imports lower ones. Many folders on one level with red arrows means one big tangle.

**Instability** = uses / (uses + used by). Close to 0: many things depend on it, so changes ripple. Close to 1: nothing depends on it, so it is safe to change. Foundation code should be near 0 and entry code near 1. A folder near 0 that also shows up as a hotspot is the most expensive place in the repo.

**Limits.** No resolution of runtime-computed imports, plugin entry points, Django `INSTALLED_APPS` strings, Webpack or Vite aliases defined only in JS config, Python `importlib` calls, or Go `replace` directives. Calls between services over HTTP or queues are invisible to an import graph: find them in step 5. Tests are left out by default (`--tests` adds them) because they import everything and hide the layers.

## hotspots.py: how it works and its limits

**Data.** One `git log --numstat -M` over up to 20,000 non-merge commits. Renames are followed: when a commit moves `old` to `new`, all older history of `old` counts for `new`. Pure moves (no lines changed) do not count as a change for coupling.

**Hotspot score** = (revisions in the window / max revisions) x (complexity / max complexity) x 100. Complexity is **indentation complexity**: the sum of indentation levels over non-blank, non-comment lines. Deep nesting (branches inside loops inside callbacks) is what makes code hard to change, and indentation tracks it in any language without a parser. It correlates well with cyclomatic complexity. It is fooled by long data tables and generated code, which `common.py` tries to skip.

**Coupling degree** = shared commits / average revisions of the two files. Commits touching more than `--max-files` (20) files are skipped, and so are commits whose subject looks like a release, version bump, format run or dependency update, otherwise every version file looks coupled to every other. Pairs are sorted cross-folder first because those are the surprising ones. With `--deps`, a pair with no import between them is marked `NO`.

**Ownership** is lines changed in the window per author (`.mailmap` is respected). It measures recent knowledge, not blame. **Knowledge at risk** lists complex files whose all-time main author made no commit in the 90 days before HEAD. Dates are measured from the HEAD commit, not today, so an old clone still gives sensible answers.

**Bug magnets** count commits in the window whose subject matches fix, bug, hotfix, revert, regression and similar words. Squash-merge repos with good PR titles give clean signals; repos with "wip" commits give noise.

**Limits.** Shallow clones have no history (`git fetch --unshallow`). Squash merges hide the real number of revisions. Vendored code under a folder not in the skip list will show up; exclude it with `--under` or by reading the table with judgement. Under 30 commits, treat everything here as a hint only.

## Patterns to look for in the results

| You see | It usually means | Put it in MAP.md as |
|---|---|---|
| One huge file at the top of hotspots with many fix commits | A god object that most features touch | Hotspot, and "add a test before changing" as a first task |
| Several sibling files (`providers/a`, `providers/b`) coupled with no import link | Parallel implementations of one interface; a change to one must be copied to all | Gotcha: "change X, change all of these" |
| Two identical files in different folders coupled | Copy-pasted shared code | Gotcha, and maybe a first task to merge them |
| Client and server files coupled with no import | They share a wire format or API contract by convention | Gotcha, and the place to look when one side breaks |
| A file with high fan-in, near-zero instability, and in the hotspot list | Central and unstable: the riskiest code in the repo | Top of the hotspots table, with its owner |
| Big folder cycle but no file cycle | The folders are one module in practice | Treat as one box in the architecture diagram |
| Shim files with no importers and a deprecation warning | Kept for old import paths used by external users | Not dead. Say who still needs them |
| Complex frozen code whose author is gone | Stable, or abandoned and feared | Knowledge at risk; ask before touching |

## Tracing recipes

The goal is a list of hops, `file:line  what happens`, for one real action, seen in a run.

**Universal trick: dump the stack at the sink.** Find where the action ends (the DB write, the response, the file written, the message sent). Put one line there that prints the call stack, trigger the action once, then read the stack from the bottom. That is the whole path, including middleware and decorators that reading misses. Remove the line afterwards.

| Stack | Stack dump at the sink | Step-through | Whole-run trace |
|---|---|---|---|
| Python | `import traceback; traceback.print_stack()` | `breakpoint()` then `where`, `up`, `down`; or `python3 -m pdb script.py` | `trace_py.py` (below); `python3 -X importtime` for import cost |
| Node / TS | `console.trace("sink")` | `node --inspect-brk app.js`, open `chrome://inspect`; VS Code "JavaScript Debug Terminal" | `node --cpu-prof app.js`, open the `.cpuprofile` in Chrome DevTools |
| Go | `debug.PrintStack()` from `runtime/debug` | `dlv debug ./cmd/app -- args`, `break pkg.Func`, `stack` | `go test -trace` or pprof |
| Java / Kotlin | `Thread.dumpStack()` | IDE breakpoint | async-profiler |
| Browser frontend | `console.trace()` in the event handler | DevTools Sources breakpoint; React DevTools for component tree | Network tab: which requests one click sends |

**trace_py.py** runs a Python command in-process with a profiler hook and prints each repo function on the path, in first-call order, indented by call depth, with `file:line`. Library frames, comprehensions and lambdas are hidden. Threads are included. Run it with the project's own interpreter (`.venv/bin/python $S/trace_py.py --repo . -- -m app.cli sync`) so its dependencies import. For a server: start it under the tracer, send one request with curl, stop it with Ctrl+C; the trace is written on exit. Use `--depth` to trim deep framework recursion.

**See the queries.** The data layer is often where the real work is. Turn on query logging locally: SQLAlchemy `create_engine(..., echo=True)`, Django `django.db.backends` logger at DEBUG, Prisma `new PrismaClient({ log: ['query'] })`, Drizzle `logger: true`, Postgres `SET log_statement = 'all'` on a local database only.

**See the HTTP.** `NODE_DEBUG=http,https`, Python `http.client.HTTPConnection.debuglevel = 1`, or a request id header you can grep for in the logs of each service.

**Stub the edges, run the middle.** If the path calls a paid or external API, patch the call to return a canned response (for Python, a small wrapper script that replaces `urllib.request.urlopen` or `requests.Session.request` and then `runpy.run_path`s the target). The trace then covers all of your code without keys, cost or network. Say in MAP.md that the edge was stubbed.

**Never** trace against production, shared staging databases, or with someone's real credentials. Use the test database or docker compose from the repo.

## Load-bearing and dead code

Load-bearing evidence, strongest first: on the traced path; imported by many files (`deps.py` load-bearing table); a hotspot; covered by many tests.

Dead evidence, strongest first: never executed when the full test suite runs with coverage (`coverage run -m pytest && coverage report`, `c8`/`nyc` for Node, `go test -cover`); no importers (`deps.py`); frozen for over a year (`hotspots.py`); the name appears nowhere as a string (`git grep -n "<name>"`). Specialist tools give a second opinion: `vulture` (Python), `knip` or `ts-prune` (TS), `deadcode` (Go). Report dead code as a list with the evidence for each, and let the team decide.

## Building the glossary

Sources of domain terms: model and table names, route segments, CLI command names, enum values, folder names, and words that recur in commit subjects. For each term write one plain sentence, where it lives (`file:line`), and what it is easily confused with. Ten good terms beat forty shallow ones. Terms that mean something different here than elsewhere (a "job" that is not a background job, a "user" that is really an organisation) are the most valuable.

## MAP.md template

Keep it under about 300 lines. Tables and lists over prose. Every claim points at a file. Mark claims **(ran)** if you saw it happen and **(read)** if it comes from reading only.

````markdown
# <repo name>: codebase map

Snapshot of `<commit sha>` on <date>. Made with the codebase-map skill.

## What it is
One paragraph: who uses it, what goes in, what comes out, what it talks to.

## How to run it
The commands that work (from CI), with the env vars and services they need.

## Architecture
```mermaid
flowchart TD
  ...  (entry points at the top, foundation at the bottom, outside services at the side)
```
Two or three sentences on the layers and anything that breaks them (cycles, shortcuts).

## One request, end to end
What was run, with what input. Then the hops:
1. `path/file.py:12` what happens (ran)
2. ...
Anything surprising on the path.

## Data model
Core entities, key fields, relationships, which code writes them.

## Where the important code lives
Hotspots table (file, why it matters, owner), load-bearing files, probably-dead files with evidence.

## Hidden coupling
Pairs that change together, and why.

## Who knows what
Folder to people, plus knowledge at risk. Anonymise if the map is shared outside the team.

## Glossary
| Term | Meaning here | Where |

## Gotchas
Things that will bite a new person, each with a pointer.

## First tasks to try
Three to five small, safe, real tasks that each teach one part of the map.
````

## Rendering the diagram

- `deps.py --svg out.svg` renders the folder graph with no extra tools: one row per layer, red edges for cycles, dashed for type-only or lazy imports, edge label = number of imports. Wide layers wrap at five boxes. Good for a first look; for MAP.md, a hand-drawn Mermaid diagram that shows runtime flow and outside services is clearer.
- Mermaid to SVG: `npx -y @mermaid-js/mermaid-cli -i diagram.mmd -o diagram.svg` (downloads a headless browser the first time). GitHub renders Mermaid blocks in Markdown on its own.
- Graphviz: `deps.py --dot deps.dot` then `dot -Tsvg deps.dot -o deps.svg`.
- Preview an SVG on macOS without a browser: `qlmanage -t -s 1600 -o . file.svg`.

## Time budget

For a repo of a few hundred files: orientation 15 minutes, data model 10, scripts and reading their output 10, tracing 20 to 40 (most of it getting the thing to run), writing MAP.md 20. If getting it to run takes over an hour, that difficulty is itself the first gotcha: write down every step that was missing from the README.
