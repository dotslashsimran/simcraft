---
name: vox-explainer
description: Make Vox-style explainer graphics or a full faceless explainer video, in the user's chosen "style C" (navy and red colour blocks, torn cream paper cards, condensed uppercase headlines, serif quotes, yellow highlighter, black mono label strips, light grain). Every beat, highlight and list item is timed to the exact word in the voiceover. Use when asked for a Vox-style video, explainer graphics, animated quotes or stats over a voiceover, or "make this look like Vox".
---

# Vox explainer

Setup, once: `cd ~/simcraft && npm i`. It also needs rough-cut's Python env (`sh ~/simcraft/skills/video/rough-cut/setup.sh`) for word timings, and ffmpeg.

## Steps

0. **Brand first.** If the video is for, with or about a company (a sponsor, a client, "a Devin video"), run the **brand-kit** skill for that company before anything else, show the user its board, then put `"brand": "<slug>"` in the scene. Everything switches to their colours, fonts and shapes. Build without a brand only for the user's own videos, which use style C.
1. **Voice first.** Get the voiceover before designing anything. It can be her recorded take (run rough-cut and clean-audio on it first) or a narration file. The picture is timed to the voice, never the other way round.
2. **Beat map.** Read the transcript and write one beat per idea, about 2.5–3.5 s each. For each beat, name its point in one clause. If you can't, the beat is filler. Show the beat list to the user before building.
3. **Write the scene file** (format below), next to the voiceover.
4. **Check layouts with stills** at a few seconds, and look at them:
   `python3 scripts/build.py scene.json --stills 2,6.5,11`
5. **Preview** (half size, fast). It ends with a motion check that fails if anything sits still for 0.5 s or more:
   `python3 scripts/build.py scene.json --preview`
6. After the user approves, render the **final** at full size: `python3 scripts/build.py scene.json`

Add `--words <file>` to reuse word timings you already have, such as rough-cut's `audio/words.json` or verify's `cuts/verify_vN_words.json` for a cut. Otherwise the voiceover is transcribed.

## Scene file

```json
{ "audio": "vo.mp4", "format": "16:9",
  "beats": [
    {"at": "Did", "device": "big", "bg": "red", "text": "$0", "sub": "What startups spent on SOC 2", "submark": "SOC 2", "submark_at": "SOC2"},
    {"at": "today", "device": "photo", "url": "https://example.com", "caption": "example.com, today",
     "tags": [{"text": "Same product", "at": "selling", "yellow": true, "x": 62, "y": 78}]},
    {"at": "Then", "device": "card", "label": "Their security team", "quote": true, "text": "Where's the SOC 2 report?", "mark": "SOC 2 report", "mark_at": "SOC"}
  ] }
```

- `at` is the word (or a few words) in the voiceover where the beat starts. Each beat searches after the previous one. A number means seconds.
- `mark`, `headmark` and `submark` are the phrase to highlight. It sweeps in when its first word is spoken; use `*_at` if the transcript spells it differently ("SOC2"). **Only one highlight per beat.**
- `brand` is a brand-kit slug (or folder). Leave it out for style C.
- `bg` is `a`, `b`, `paper` or `accent` (`navy`/`red` still work for style C). Alternate them. The diagonal split changes on its own each beat. Use `"flat": true` for a single colour.
- `format` is `16:9` or `9:16`. `"twos": false` turns off the 12 fps stepped motion.
- `tags` are black mono label strips (`yellow: true` for the accent). `x` and `y` are in % of the frame, and `at` is the word that brings the tag in.

| Device | Use for | Fields |
|---|---|---|
| `big` | One huge statement or number | `text`, `sub`, `mark`/`submark` |
| `card` | Torn paper card: a quote, a question, a claim | `label`, `head`, `text`, `quote`, `mark`/`headmark`, `width`, `tilt` |
| `stat` | A number counting up | `value`, `prefix`, `suffix`, `sub` |
| `photo` | Real evidence: a screenshot, document or photo, pinned like a print with a slow push-in | `img` or `url` (captured automatically), `caption`, `width`, `ratio`, `tilt` |
| `cutout` | An object dropped onto the colour blocks | `img` (transparent PNG; make one with the image-gen skill using `--cutout`), `size`, `x`, `tilt` |
| `list` | Numbered points, each landing on its word | `items`, `items_at`, `label` |
| `chart` | A line that draws itself | `points`, `label`, `end_label`, `draw_ms` |

## Rules (her taste plus the research)

With a brand kit, the brand's colours, fonts and edges replace style C's. The rules below about pace, pictures, evidence and motion still apply.

- **One idea per frame. Huge type.** Strict palette: navy #1E2B3A, red #C7302B, cream #F2EAD8, ink, yellow #F5C518. No purple, no cute illustrations, no old-Mac frames.
- **Mix in pictures.** All-text reads as a slideshow, and she has called that out before. Use a `photo` with a real screenshot or document every few beats. For things that can't be screenshotted, make cut-outs with image-gen. Generated images never contain text; all type is set in code.
- **Real evidence must be real.** Never present a generated image as a document, a screenshot or a quote.
- **Nothing freezes.** Every beat slowly pushes in, and the motion check enforces it.
- No whooshes. If sound effects are added, use only small paper or UI sounds.

Research, design reasoning and what each choice fixes: [reference.md](reference.md).
