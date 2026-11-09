<div align="center">

# simcraft

**Skills that teach Claude how to do hard things well.**

24 skills for Claude Code, built from real projects, with the parts tutorials skip.

[![MIT License](https://img.shields.io/badge/license-MIT-black?style=flat-square)](LICENSE)
[![Skills](https://img.shields.io/badge/skills-24-black?style=flat-square)](#skills)
[![Claude Code](https://img.shields.io/badge/for-Claude%20Code-d97757?style=flat-square)](https://claude.com/claude-code)
[![Stars](https://img.shields.io/github/stars/dotslashsimran/simcraft?style=flat-square&color=black)](https://github.com/dotslashsimran/simcraft/stargazers)

[Install](#install) · [Video](#-video) · [Engineering](#-engineering) · [Learning](#-learning) · [Design](#-design) · [Planning](#-planning)

</div>

<br>

Each skill is a folder with a `SKILL.md`: the method, the judgement calls, and real, tested scripts for one kind of work. Claude Code loads the right one when your request matches. You don't call them by name, you just ask.

What makes them different: every skill covers **what breaks, how to check the result, and how to tell good from good enough.**

<br>

## Install

```bash
git clone https://github.com/dotslashsimran/simcraft ~/simcraft
mkdir -p ~/.claude/skills
for s in ~/simcraft/skills/*/*/; do ln -s "${s%/}" ~/.claude/skills/; done
cd ~/simcraft && npm i
```

Then open Claude Code and ask for something, like *"make a perspective-illusion loop of a rainy night street"*.

<details>
<summary>More setup details</summary>

<br>

- Claude Code finds skills one folder deep, so the loop links each one into `~/.claude/skills`. The categories are only for browsing. To install a single skill, link just that folder.
- A few skills have a one-time setup script in their folder (for example `setup.sh` for Whisper in rough-cut). Each SKILL.md says so at the top.
- **Needs:** Python 3.9+, Node 18+ (for skills that drive a browser or render video), and ffmpeg on your PATH or set as `FFMPEG=/path/to/ffmpeg`.
- **API keys** (only image-gen uses them) are read from your environment or `~/.config/simcraft/keys.env`. They never go in the repo.

</details>

<br>

## Skills

### 🎬 Video

| Skill | What it does |
|:--|:--|
| [**perspective-illusion**](skills/video/perspective-illusion/SKILL.md) | Illustrated 3D loops in the Japanese perspective-illusion style: flat scenes that open into depth. |
| [**code-loop**](skills/video/code-loop/SKILL.md) | Any three.js scene or shader rendered as a seamless loop, checked for seams, freezes and blinks. |
| [**reverse-engineer-video**](skills/video/reverse-engineer-video/SKILL.md) | Hand it a viral clip, get a remake plan: every cut, camera move and beat. |
| [**scripted-screencast**](skills/video/scripted-screencast/SKILL.md) | Product demos written as a script and rendered frame-perfect. No retakes. |
| [**repo-timelapse**](skills/video/repo-timelapse/SKILL.md) | Your git history as a video of the project growing. |
| [**rough-cut**](skills/video/rough-cut/SKILL.md) | Raw talking footage to a tight first cut: pauses, ums and retakes removed. |
| [**clean-audio**](skills/video/clean-audio/SKILL.md) | Voice cleanup that measures first, then keeps every word clear. |
| [**vox-explainer**](skills/video/vox-explainer/SKILL.md) | Vox-style explainer graphics timed word by word to a voiceover. |

### 🧑‍💻 Engineering

| Skill | What it does |
|:--|:--|
| [**codebase-map**](skills/coding/codebase-map/SKILL.md) | Learn an unfamiliar repo fast by tracing one real request end to end. |
| [**bisect-anything**](skills/coding/bisect-anything/SKILL.md) | Find the exact commit behind any regression, then the exact lines. |
| [**flaky-test-killer**](skills/coding/flaky-test-killer/SKILL.md) | Fix why a test is flaky, never with retries, and prove the fix. |
| [**benchmark-honestly**](skills/coding/benchmark-honestly/SKILL.md) | Benchmarks that survive scrutiny, with confidence intervals. |
| [**api-design-review**](skills/coding/api-design-review/SKILL.md) | Review an API, SDK or CLI, and catch breaking changes. |
| [**security-threat-model**](skills/coding/security-threat-model/SKILL.md) | Threat-model your own app from its real code, LLM apps included. |
| [**dx-audit**](skills/coding/dx-audit/SKILL.md) | Try a developer tool like a brand-new user and time it to first success. |

### 🧠 Learning

| Skill | What it does |
|:--|:--|
| [**paper-to-runnable**](skills/learning/paper-to-runnable/SKILL.md) | A research paper turned into the smallest honest implementation of its idea. |
| [**system-design-from-scratch**](skills/learning/system-design-from-scratch/SKILL.md) | Build a tiny real system, load-test it, break it, write up the trade-offs. |
| [**from-scratch**](skills/learning/from-scratch/SKILL.md) | Learn anything by building the smallest version of it, one step at a time. |
| [**teach-back**](skills/learning/teach-back/SKILL.md) | You explain it, Claude finds the gaps, you get a one-page cheat sheet. |

### 🎨 Design

| Skill | What it does |
|:--|:--|
| [**image-gen**](skills/design/image-gen/SKILL.md) | Images with your own Gemini or OpenAI key, consistent across a set. |
| [**brand-kit**](skills/design/brand-kit/SKILL.md) | Learn a brand from its live site into a kit the other skills apply. |

### 🧭 Planning

| Skill | What it does |
|:--|:--|
| [**skill-router**](skills/planning/skill-router/SKILL.md) | Picks the right skill for a task, or a chain of two or three. |
| [**next-step**](skills/planning/next-step/SKILL.md) | A big messy goal turned into one clear next action at a time. |
| [**video-script**](skills/planning/video-script/SKILL.md) | Short video scripts that sound like you, built from your own ramble. |

<br>

## Chains that work well

`skill-router` builds these on its own when a task has stages.

| Start with | Then |
|:--|:--|
| A viral clip | reverse-engineer-video → code-loop or perspective-illusion |
| A research paper | paper-to-runnable → vox-explainer |
| A new SDK | dx-audit → scripted-screencast |
| "It got slower" | benchmark-honestly → bisect-anything |
| A new job | codebase-map → flaky-test-killer |
| Raw footage | rough-cut → clean-audio |

<br>

## Credits

image-gen was inspired by [adamd9/skill-image-gen](https://github.com/adamd9/skill-image-gen), and vox-explainer by Charlie Hills' vox-animation skill (both MIT). Everything here is written from scratch.

## License

[MIT](LICENSE) © Simran Sachdeva
