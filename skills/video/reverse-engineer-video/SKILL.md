---
name: reverse-engineer-video
description: Break down a reference or viral video clip so it can be remade. Finds every cut and dissolve, times each shot, measures pacing, works out the camera move in each shot (static, pan, tilt, zoom or push in, roll, handheld shake, with speed), finds beats and loudness jumps and checks whether the cuts land on them, and reads on-screen text and speech when the tools are installed. Makes a contact sheet, a timeline strip, breakdown.md with a beat map and a remake plan, and breakdown.json. Also makes side-by-side or stacked comparison videos of a remake against the reference. Use when the user says "break down this video", "reverse engineer this clip", "how was this edited", "shot list from this video", "why does this go viral", "recreate/remake this video", "what camera moves are in this", "do the cuts hit the beat", or "compare my remake with the original".
---

# Reverse engineer a video

Scripts live in `scripts/`. They run on `python3` with numpy. For better tracking of very fast moves, run `sh setup.sh` once and use `.venv/bin/python` instead (it adds OpenCV; `sh setup.sh --whisper` also adds speech-to-text). On-screen text needs `tesseract` on the PATH. Every optional part is skipped cleanly when its tool is missing, and the report says so.

## Steps

1. **Analyse.** Write the output somewhere other than the video's own folder:
   ```
   python3 scripts/analyze.py <clip> -o <out_dir>
   ```
   It writes `breakdown.md`, `breakdown.json`, `contact_sheet.jpg`, `shots_detail.jpg` and `timeline.png`.

2. **Check the cuts before trusting anything else.** Open `timeline.png` and `contact_sheet.jpg`. If two shots in the sheet look like the same shot, a cut was false: run again with `--sensitivity 0.7`. If a sheet frame shows two shots blended or a shot changes inside one tile, a cut was missed: use `--sensitivity 1.4`. Fast flashes shorter than `--min-shot` (0.1 s) are listed as flash frames, not shots.

3. **Look, then fill in the gaps.** Read `shots_detail.jpg` (start, middle and end frame of each shot, with the measured numbers). Then edit `breakdown.md`:
   - fill the "what happens" column of the beat map, one short line per shot,
   - write the hook read (see below),
   - write the remake plan. Use the measured times and camera numbers; do not round them into guesses.

4. **Remake, then compare at the same timestamps.** You cannot watch video, so compare stills first:
   ```
   python3 scripts/compare.py <ref> <remake> --stills compare.jpg --breakdown <out_dir>/breakdown.json
   ```
   Fix the biggest difference, repeat. When it is close, make a video for the user:
   ```
   python3 scripts/compare.py <ref> <remake> -o compare.mp4 --mode side     # or stack for vertical posts
   python3 scripts/compare.py <ref> <remake> -o overlay.mp4 --mode blend    # lines up horizons and edges
   ```
   Use `--ref-start` / `--remake-start` if one clip has a lead-in. A shorter remake holds its last frame, or repeats with `--loop-remake`. Output is always re-encoded.

## Reading the hook (first 2 seconds)

The "Hook" section lists measured facts: when the first cut comes, how many cuts fall in the first 2 s, whether sound starts at frame one, the first words and text, and how busy the opening is compared to the rest (`motion first 2s vs rest`, above 1 means busier). Use them to answer three questions in plain words:
- **What is the promise in frame one?** A strong first frame shows the payoff or a clear question (a finished result, something odd, a face mid-sentence). An establishing wide shot or a logo is a weak start.
- **What changes before 0.5 s?** A cut, a move starting, a sound hit or text appearing. If nothing changes, say so: viewers decide in this window.
- **Why stay to 2 s?** Usually an open loop: text or speech that asks a question, or a move that has not finished.

## Camera numbers, and rebuilding a shot in 3D

Each shot gets a label (static, pan, truck/arc, tilt, zoom in/out, push in/pull out, roll, handheld). Long shots with several moves are split into phases, such as `push in > tilt up`. Each label comes with numbers: pan speed in % of frame width per second, tilt in % of frame height per second, total zoom factor, shake in % of frame width, and parallax. Directions describe the camera, not the picture. "Zoom" means the picture scaled with no parallax (a lens zoom, a digital scale, or a very far scene). "Push in" means there was parallax, so the camera really moved. `breakdown.json` has a camera path per shot (pan, tilt and zoom over time) to use as keyframes.

To rebuild a scene in three.js or a 3D tool, measure the geometry on a sharp frame from `shots_detail.jpg`:
- **Horizon height** (where the vanishing points sit) tells you camera pitch: a horizon on the frame's centre line means a level camera; above centre means it tilts down.
- **Lens**: take two vanishing points of lines that are at right angles in the real world. The focal length in pixels is `sqrt(-(v1 - c)·(v2 - c))`, where c is the frame centre. Vertical field of view is `2·atan(H / 2f)`.
- **Camera height**: if the horizon crosses a person standing on the same floor at their eye line, the camera is at eye height (about 1.6 m). Where it crosses them gives the ratio.
The full method, with worked numbers, is in [reference.md](reference.md).

## Rules

- Never write into the reference video's folder, and never change the reference file.
- Trust measured times over what you think you see. Quote cut times from `breakdown.json`.
- Say clearly which parts were skipped (no transcript, no text reading) instead of guessing them.
- In the beat map, "on beat" means within the tolerance shown (default 1.5 frames, at least 50 ms). Always quote the chance rate next to the hit rate: 60% of cuts on onsets means little if random cuts would score 50%.

How each measurement works, the limits, the test results and all options: [reference.md](reference.md).
