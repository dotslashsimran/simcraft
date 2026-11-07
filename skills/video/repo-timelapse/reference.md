# Repo timelapse reference

## Files

- `scripts/extract.py` reads the git history into `history.json` and prints the story report. Python 3.9 or newer, standard library only.
- `scripts/render.mjs` loads the engine in headless Chromium, steps the frames and pipes them to ffmpeg.
- `engine/timelapse.html` is the renderer. `window.render(t)` draws the frame at `t` seconds on a 2D canvas.
- `engine/fonts/` holds Geist and Geist Mono (SIL Open Font License), so the video looks the same on any machine.

## How the history is read

`extract.py` follows the first-parent chain of the chosen branch (`git log --first-parent --diff-merges=first-parent -M --numstat --summary`). A merge commit is diffed against the main line before it, so the lines added by a merged branch arrive in one step with the pull request title as the caption. This keeps the replayed line counts exact. At the end, the replayed tree is compared with `git diff --numstat` of the real tree, and the report prints "Replay check: exact" or how far off it is.

- **Sizes** are line counts per file. Binary files get a small fixed weight and are left out of the line counter.
- **Renames** keep the file's identity, so the tile flies to its new folder instead of vanishing and reappearing. Reorganising a repo turns into a visible moment, not a fake burst of new code.
- **Date range.** With `--since`, the starting tree is the last commit before that date, read with one `git diff` against the empty tree. The video starts from that tree instead of from nothing.
- **Path filter.** `--path` limits both the log and the tree. The renderer strips the folder every file shares, so the map starts inside it.
- **Big repos.** With more than `--max-events` (1500) commits, neighbouring commits are squashed into one event. The caption is the best message in the group and the meta line shows how many commits it holds. A 2,500-commit repo with 34,000 files extracts in about 20 seconds; most of that is git itself.

### Skipped by default

Lockfiles (`package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`, `Cargo.lock`, `poetry.lock`, `uv.lock`, `go.sum` and others), vendored and build folders (`node_modules`, `vendor`, `third_party`, `dist`, `build`, `.next`, `target`, `__pycache__`, `.venv`, `coverage`), minified files, source maps, generated protobuf code, snapshots and recorded HTTP test cassettes. Patterns match at any depth, so `dist/**` also skips `web/dist/app.js`. `--include-default-noise` keeps them.

### The story report

- **Activity by month** shows where the work happened.
- **Busiest 30 days** gives ready-made `--since` and `--until` flags.
- **Biggest folders** shows what will dominate the map. If docs or tests are most of the area, exclude them or film `--path src`.
- **Bulk commits** are commits that add more than 2,000 lines and over a fifth of the final size. They are usually imports, generated code or fixtures. In one test repo, two commits of recorded test cassettes added 1,000,000 lines and hid the real code entirely.

## How the video is built

- **Treemap.** Each folder is a box and each file a tile sized by its lines. Children keep the order they first appeared in, so old files stay put and new work grows in at the end. The layout is computed once per commit and each tile moves smoothly from its old place to its new one, so the frame never jumps.
- **Growth.** The map's area follows the total size, from a small square at the first commit to the full frame at the largest point. `--grow fill` turns this off.
- **Colour.** Each language has a fixed hue in OKLCH with matching lightness, so no language shouts. Docs, config and assets are low-saturation grey so the code stands out. Tiles vary slightly in lightness so neighbours read as separate files.
- **Touch.** A changed file flashes bright and fades over about a second. New files glow strongest, moved files glow softly, deleted files turn red and shrink away.
- **Time.** Each gap between commits gets screen time from half an equal share and half the log of the real gap. A one-minute gap and a two-week gap differ by a few times, not by twenty thousand. The date in the corner moves with real time, so it races through quiet weeks.
- **Captions** are picked in windows of about 1.15 seconds. The message scoring prefers longer, specific messages over "wip", "fix", "update" and merge boilerplate.
- **Sparkline.** Lines changed over real time, square-root scaled so one huge commit does not flatten the rest. The playhead follows the date.
- **Cards.** The title card shows the name, date range and commit count. The end card shows the final line count, commits, days, files, people and the language mix.

## Layout and safe areas

All sizes are in units of 1px at 1080p and scale with the output size, so `--preview` and 4K renders match. In 9:16, the header starts 150px down and the sparkline ends 380px above the bottom, to stay clear of platform buttons and captions. The caption wraps to two lines in 9:16 and one in 16:9.

## Rendering

`render.mjs` sets the canvas to the exact output size, calls `window.frame(t)` for six frames at a time, and sends JPEG frames (quality 0.94) to ffmpeg. The final encode is x264, `-preset slow -crf 16`, yuv420p, faststart. A 1080p frame takes about 45 ms, so 30 seconds at 60fps renders in about 80 seconds. Preview is half size, 30fps, veryfast.

Every frame is a pure function of `t`, so frames can be rendered in any order. `--stills` uses this to check single moments. Add `--list-times` to print the video time of every commit, which helps pick still times around a key commit.

## history.json format

```
{ "name": "repo", "authors": ["..."], "files": [["path", "Language"], ...],
  "baseline": [[fileId, lines, isBinary], ...],
  "events": [{ "t": unix, "tz": minutes, "h": "abc1234", "a": authorIndex, "m": "message",
               "add": n, "del": n, "n": commitsInEvent, "s": captionScore,
               "ch": [[fileId, linesAfter, "A"|"M"|"D", isBinary, movedFromFileId?], ...] }] }
```

The file holds commit subjects, author names (or aliases) and file paths, but no code and no email addresses.

## Limits

- Only the first-parent line is shown. Commits inside merged branches appear as one step at the merge, with the merge's author. For repos that squash-merge or commit straight to main, this changes nothing.
- Line counts are text lines, including blank lines and comments.
- A file renamed in from a skipped path is counted from git's real size at that commit, which costs one extra git call per such file.
- Very deep or very wide trees (tens of thousands of visible files) still render, but most tiles are smaller than a pixel. Film a folder instead.
- No audio. Add music in the editor.
