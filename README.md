# simcraft

**Skills that teach Claude how to do hard things well.**

Each skill is a folder with a `SKILL.md`: the method, the judgement calls, and the tools (real, tested scripts) for one kind of work. Claude Code loads the right one when your request matches, and `skill-router` chains two or three together when a job has stages.

They're built from real projects, so they include the parts tutorials skip: what breaks, how to check the result, and how to tell good from good-enough.

---

## 🎬 Video

| Skill | What it does |
|---|---|
| [**perspective-illusion**](skills/video/perspective-illusion/SKILL.md) | Illustrated 3D loops in the Japanese perspective-illusion style. A telephoto-flat scene dolly-zooms into depth, corner views open from flat to 3D, objects swap depth while nobody can tell. Ink line art, two-tone shading, a GPU renderer with motion blur, and a blink checker. |
| [**code-loop**](skills/video/code-loop/SKILL.md) | The engine underneath: any three.js scene or GLSL shader rendered as a seamless loop, with jittered anti-aliasing, adaptive motion blur and automatic seam, freeze and blink checks. |
| [**reverse-engineer-video**](skills/video/reverse-engineer-video/SKILL.md) | Hand it a viral clip and get a remake plan: every cut, shot lengths, camera moves, beats and whether cuts land on them, a contact sheet and a beat map. Plus side-by-side comparisons for the remake. |
| [**scripted-screencast**](skills/video/scripted-screencast/SKILL.md) | Product demos written as a script and rendered frame-perfect: smooth cursor, click ripples, eased auto-zoom, human typing. No retakes, and you re-render when the UI changes. |
| [**repo-timelapse**](skills/video/repo-timelapse/SKILL.md) | A repo's git history as a video of the project growing: a living file map, commits typing in as captions, a lines-of-code counter. For build-in-public posts. |
| [**rough-cut**](skills/video/rough-cut/SKILL.md) | Raw talking footage to a tight first cut: dead air, ums, stutters, false starts and retakes removed, cuts snapped to the real sound, the result checked by listening to it again. |
| [**clean-audio**](skills/video/clean-audio/SKILL.md) | Voice cleanup that measures first: rumble, hum, noise, harsh S sounds, uneven volume, loudness for social. Picks the strongest cleanup that keeps every word clear. |
| [**vox-explainer**](skills/video/vox-explainer/SKILL.md) | Vox-style explainer graphics timed word by word to a voiceover: colour blocks, torn paper cards, highlighter sweeps, real screenshots pinned as evidence. |

## 🧑‍💻 Engineering

| Skill | What it does |
|---|---|
| [**codebase-map**](skills/coding/codebase-map/SKILL.md) | Get to know an unfamiliar repo fast: trace one real request end to end, find hotspots and hidden coupling from git, draw the architecture, write a first-week guide. |
| [**bisect-anything**](skills/coding/bisect-anything/SKILL.md) | Turn any regression (a failing test, changed output, a slowdown, a visual change) into a reliable pass/fail oracle, bisect it, then shrink the bad commit to the exact hunk. |
| [**flaky-test-killer**](skills/coding/flaky-test-killer/SKILL.md) | Find why a test is flaky and fix the cause, never with retries. Measures the flake rate with confidence intervals, applies stressors per cause, finds the polluting test, proves the fix. |
| [**benchmark-honestly**](skills/coding/benchmark-honestly/SKILL.md) | Benchmarks that survive scrutiny: warmup, interleaved runs, bootstrap confidence intervals, and checks for the classic lies (dead code, warm caches, coordinated omission). |
| [**api-design-review**](skills/coding/api-design-review/SKILL.md) | Review an HTTP API, SDK surface or CLI for naming, errors, versioning, pagination, idempotency and ergonomics, with an OpenAPI linter and a breaking-change differ. |
| [**security-threat-model**](skills/coding/security-threat-model/SKILL.md) | Threat-model your own app: a data-flow diagram from the real code, STRIDE threats ranked, a local proof for each real risk, then the fix as a regression test. Covers LLM apps too. |
| [**dx-audit**](skills/coding/dx-audit/SKILL.md) | Try a developer tool exactly like a new user and time it to first success. Logs every point of friction, grades the error messages, and writes a fair teardown with concrete fixes. |

## 🧠 Learning

| Skill | What it does |
|---|---|
| [**paper-to-runnable**](skills/learning/paper-to-runnable/SKILL.md) | A research paper turned into the smallest honest implementation of its core idea, with each equation tied to code, a toy reproduction of the result, and animations of the mechanism at work. |
| [**system-design-from-scratch**](skills/learning/system-design-from-scratch/SKILL.md) | Build a tiny real version of a system (rate limiter, queue, Raft, LSM tree), load-test it for p99s, break it with injected faults, and write up the trade-offs. |
| [**from-scratch**](skills/learning/from-scratch/SKILL.md) | Learn anything by building the smallest version of it, Karpathy style: one step at a time, predict before you run. |
| [**teach-back**](skills/learning/teach-back/SKILL.md) | You explain it and Claude finds the gaps (the Feynman technique), then ends with a one-page cheat sheet and spaced review questions. |

## 🎨 Design

| Skill | What it does |
|---|---|
| [**image-gen**](skills/design/image-gen/SKILL.md) | Images with your own Gemini or OpenAI key: variation sheets, reference images for consistency, clean transparent cut-outs, every prompt saved. |
| [**brand-kit**](skills/design/brand-kit/SKILL.md) | Learn a brand from its live site (colours, real fonts, logo, shapes, image style) into a kit the video and image skills apply automatically. |

## 🧭 Planning

| Skill | What it does |
|---|---|
| [**skill-router**](skills/planning/skill-router/SKILL.md) | Picks the right skill for a task, or a chain of two or three where each one's output feeds the next. Reads whatever skills are installed, so it works with yours too. |
| [**next-step**](skills/planning/next-step/SKILL.md) | A big messy goal turned into one clear next action at a time, never a wall of tasks. |
| [**video-script**](skills/planning/video-script/SKILL.md) | Short video scripts that sound like you: built from your own voice-note ramble, stakes in line one, every fact sourced. |

---

## Chains that work well

`skill-router` builds these on its own when a task needs them.

```
viral clip      →  reverse-engineer-video  →  code-loop / perspective-illusion
research paper  →  paper-to-runnable       →  vox-explainer
new SDK         →  dx-audit                →  scripted-screencast
"it got slower" →  benchmark-honestly      →  bisect-anything
new job         →  codebase-map            →  flaky-test-killer
raw footage     →  rough-cut               →  clean-audio
```

## Install

```
git clone https://github.com/dotslashsimran/simcraft ~/simcraft
mkdir -p ~/.claude/skills
for s in ~/simcraft/skills/*/*/; do ln -s "${s%/}" ~/.claude/skills/; done
cd ~/simcraft && npm i
```

Claude Code finds skills one folder deep, so the loop links each one into `~/.claude/skills`. The categories are only for browsing. To install a single skill, link just that folder.

A few skills have a one-time setup script in their folder (for example `setup.sh` for Whisper in rough-cut). Each SKILL.md says so at the top.

## Needs

- **Python 3.9+**. Most scripts use only the standard library.
- **Node 18+**, for the skills that drive a browser or render video.
- **ffmpeg** on your PATH, or `FFMPEG=/path/to/ffmpeg`.

API keys (only image-gen uses them) are read from your environment or `~/.config/simcraft/keys.env`. They never go in the repo.

## Credits

image-gen was inspired by [adamd9/skill-image-gen](https://github.com/adamd9/skill-image-gen), and vox-explainer by Charlie Hills' vox-animation skill (both MIT). Everything here is written from scratch.
