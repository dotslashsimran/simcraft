# dx-audit: definitions, rubric, and what testing showed

## Friction tags

| Tag | Use it when | Example |
|---|---|---|
| `missing-step` | The reader must do something the docs never say | "Run `uv`" right after install, but the shell has not picked up the new PATH |
| `wrong-snippet` | A snippet fails or differs from what the tool does now | An import path renamed two versions ago |
| `unclear-error` | An error does not say what happened, why, or how to fix it | `Error: 401` with no word about which key |
| `version-mismatch` | Docs, package, and runtime disagree | Docs show `requires-python >=3.14`, tool writes `>=3.9` |
| `auth-wall` | Progress needs a signup, key, card, phone number, or approval | "Join the waitlist to get an API key" |
| `naming-confusion` | One thing has two names, or the reader must guess a name | A code block with no file name; "project" in the UI, "app" in the SDK |
| `slow-feedback` | Something runs long with no sign of progress | A 90 second install that prints nothing |
| `dead-link` | A link 404s, or an anchor points to a heading that no longer exists | |
| `copy-paste-hazard` | The copied text does not run as is | `$ ` prompt copied, smart quotes, `<YOUR_KEY>` that looks like shell redirection |
| `other` | Anything else; the note says what | |

One step can carry several tags. Tag the step where the reader feels the pain, and point to the cause in the note.

## Severity

| Sev | Meaning | Test |
|---|---|---|
| 1 | Annoying | A careful reader recovers in under a minute from what is on screen |
| 2 | Costs real time | Needs rereading, a second page, or trial and error, a few minutes |
| 3 | Likely to make people quit | Needs knowledge from outside the docs, a support ticket, or waiting on a person |

Be honest about recovery. If the fix was printed a few lines up, it is a 1 even if you missed it.

## Error message rubric

Score each from 0 to 2. Total out of 6: A is 6, B is 5, C is 4, D is 3, F is 2 or less.

| | 0 | 1 | 2 |
|---|---|---|---|
| **What happened** | Missing, or wrong | There, but buried under noise or jargon ("Failed to spawn") | The first line says it in plain words |
| **Why** | Missing, or points at the wrong cause | Partly, or the right cause plus misleading hints | The real cause, specific to this case (which key, which file, which version) |
| **How to fix** | Missing, or the suggestion makes it worse | Points the right way but does not work in one go | A command or change that works when copied, tested by you |

Always test the suggested fix before scoring "How to fix". In the uv run, the hint for a version conflict looked complete but needed a second command.

Pick the three mistakes newcomers make most with this kind of tool:
- **API or SDK:** wrong key, key missing from the environment, old SDK version against a new API.
- **CLI:** typo in a name, wrong folder or missing file, old runtime version.
- **Framework:** missing config file, wrong port or env var, incompatible plugin version.

## Metrics

All computed by `friction.py metrics` from `friction.json`.

- **Time to first success, modelled.** The agent is not a person: it reads a page in a second and waits on its own thinking. So the headline time is modelled for a first-time developer, and the model is printed in every audit:
  - each new docs page: words / 238 per minute (average adult reading speed), at most 6 minutes a page
  - each command: 20 seconds to find, copy and paste it, plus how long it really ran
  - each line of code written: 15 seconds
  - each failure: 1.5, 4 or 10 minutes to work out, for severity 1, 2 or 3 (half that for friction on a step that still worked)
  - override any step with `--human-min` when you have a better estimate, and say why in the note
- **Time to first success, wall clock.** From `init` to the success step. Reported next to the model, never instead of it.
- **Steps, commands.** Counted up to the success step.
- **Pages visited and context switches.** Distinct docs URLs, and how many times the URL changed between steps. Visits outside the docs host are listed separately as "had to leave the docs for".
- **Errors.** Steps whose command exited non-zero, before success and in total. Walls are counted apart.
- **Lines of code.** The sum of `--loc`.
- **Where people give up.** A "stretch" is a run of failed or blocked steps up to the next step that works. Its risk is 2 times its severity total, plus its modelled minutes, plus 6 if it hit a wall, plus 6 if it never recovered. The highest one is reported. It is called "most likely give-up" only at a risk of 8 or more, a wall, or no recovery; below that it is "roughest stretch", so a small stumble is not oversold.

## friction.json

```
{ tool, goal, success, docs, environment, started_at, success_at,
  steps: [ { n, t, elapsed_s, url, action, cmd, output, exit_code, duration_s,
             outcome: ok|fail|blocked|success|info, tags, sev, loc, note, fix, shot, human_min } ],
  error_checks: [ { n, mistake, cmd, exit_code, message, what, why, fix, note, better } ] }
```
Output is clipped to 4000 characters in the JSON; the full text is in `outputs/step-NN.txt`. Everything saved is scrubbed of the home folder, the login name, and any `--redact FROM=TO` pairs (kept in `.redact.json`, which is not for publishing). `friction.py redact <dir> FROM=TO` scrubs an audit after the fact.

## snapshot.mjs details

- Waits for the network to settle, closes cookie banners (and records that one was there), then takes the first-screen screenshot. `--full` adds a whole-page one.
- Text comes from `main`, `article` or `[role=main]`, with headings, lists and code kept, so you read what the reader reads instead of the menu.
- `--links` checks every link with HEAD, then GET if HEAD is refused, 8 at a time. Every failure is then re-checked in the real browser, because many sites refuse scripted requests. Same-page `#anchors` are checked against the ids on the page.
- `--copy` clicks each visible code block's copy button and reads the clipboard. Blocks hidden in closed tabs (other OS, other shell) are skipped.
- `--video` records a slow scroll through the page to `video/<name>.webm`, for a journey video.
- Every risky browser call has a deadline, so one stuck page never hangs the audit.

## Choosing the environment

| Tool kind | Clean environment |
|---|---|
| CLI with a curl installer | Empty `HOME` and bare `PATH` with `env -i`. The installer writes only inside that home. |
| Python package | `python3 -m venv` in the scratch folder, or `docker run --rm python:3.12-slim` |
| Node package | A new folder with its own `npm init -y`; check `node -v` matches what the docs require |
| Hosted API | A new folder plus the key in an env var the user sets. Never paste a key into chat or a file in the audit folder |
| Something that needs Docker or a GPU | Ask the user before starting, and record the requirement as a finding if the docs bury it |

Note anything already on the machine that the sandbox cannot hide (for example the macOS system Python). It can change what the tool does, and readers need to know.

## Tested on (Nov 7, 2026): uv 0.13.7

Full result in `examples/uv-first-project/`. Goal: create a project, add a package, run a script that imports it, from https://docs.astral.sh/uv/ only, on macOS with an empty home and no uv or Homebrew.

- Reached the goal in 11 steps and 3 pages, 15.1 minutes modelled (3.3 minutes agent wall clock), 3 lines of code, no walls.
- Friction: three severity 1 points. The First steps page says to run `uv` straight after installing, which fails in the same shell (`command not found`); `uv init` silently picked the end-of-life system Python 3.9.6 while the guide shows 3.14; the script example has no file name.
- Error messages averaged 3.7 out of 6. A version conflict scored B (clear, but about 50 lines long, and its fix needs a second round). A package-name typo scored D: the right cause, followed by a hint about Python versions and a `--frozen` hint that would write the typo into `pyproject.toml`. A file-name typo scored D: "Failed to spawn" with no "did you mean".
- The scripts worked as intended: 32 copy buttons tested (none copied the prompt), 487 links checked across three pages with no dead ones. One false alarm (`crates.io` refuses scripted HEAD and GET) was cleared by the browser re-check, which is why that re-check exists. A first version of the copy test hung on headless clipboard reads and counted hidden tab blocks; both are fixed.

## Limits

- One run is one developer on one machine. Run it again on another OS before calling a finding universal.
- The time model is a model. It is consistent across audits, which is what makes them comparable, but it is not a stopwatch on a person. When you can, watch one real newcomer do the same goal and compare.
- An agent reads more carefully than most people skim. When in doubt, assume the reader missed the line in the installer output, and say so in the note.
- Dashboards behind a login can only be audited up to the wall unless the user signs in themselves.
