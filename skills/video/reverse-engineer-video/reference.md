# reverse-engineer-video: how it works

## Files

| file | what it does |
|------|--------------|
| `scripts/analyze.py` | the whole breakdown: shots, camera, pacing, audio, text, pictures, `breakdown.md` and `breakdown.json` |
| `scripts/compare.py` | remake against reference: side-by-side, stacked, blended or difference video, or still pairs |
| `scripts/cuts.py` | cut, flash and dissolve detection |
| `scripts/motion.py` | camera motion per shot and the move labels |
| `scripts/audio.py` | onsets, tempo, beats, loudness, cut-to-beat alignment, optional transcript |
| `scripts/sheets.py` | contact sheet, detail sheet, timeline strip |
| `scripts/ocr.py` | on-screen text timing with tesseract (optional) |
| `setup.sh` | optional `.venv` with OpenCV, and Whisper with `--whisper` |

ffmpeg is found through `$FFMPEG`, then the PATH. ffprobe is not needed: clip details are read from `ffmpeg -i`. Labels on the pictures need a font file; common system fonts are searched, or set `FONT=/path/to/font.ttf`. Without one the pictures are made without labels.

## analyze.py options

| option | default | meaning |
|--------|---------|---------|
| `-o DIR` | `./<name>-breakdown` | output folder (refuses the video's own folder) |
| `--sensitivity X` | 1.0 | cut detection. Higher finds more cuts and dissolves, lower finds fewer |
| `--min-shot S` | 0.1 | two cuts closer than this are a flash frame (same shot either side) or are merged |
| `--tol-ms MS` | 1.5 frames, at least 50 | how close a cut must be to a beat or onset to count as "on" it |
| `--motion auto/numpy/opencv` | auto | camera tracker. auto uses OpenCV when this Python has it |
| `--max-fps F` | 30 | analysis frame rate cap. Cut times are exact to 1/F s |
| `--no-transcript`, `--no-ocr` | | skip the slow optional parts |

The video is decoded once at a small size (short side 180 px) and every frame is kept in memory: about 170 KB per frame, so a 60 s clip at 30 fps needs about 300 MB. Analyse long videos in parts with ffmpeg `-ss`/`-t` first.

## Cuts

For every frame the script computes two numbers against the frame before:
- the mean absolute brightness difference on a 0 to 100 scale. This is the same number ffmpeg's `scdet` filter and `select='gt(scene,x)'` start from;
- a colour histogram distance on a 0 to 1 scale (8 levels per channel). It ignores movement inside a shot, so it backs up the first number on busy footage.

A **hard cut** is a frame whose brightness difference is at least 3 times the median of its neighbours within 0.2 s and at least 5, or whose colours change almost completely. Comparing with the neighbours, instead of a fixed threshold like ffmpeg's usual 0.4, is what stops fast camera moves and busy footage from producing false cuts, while still catching cuts between two similar-looking shots.

Two cuts closer than `--min-shot` are checked for a **flash**: if the frames before the first and after the second match, it was a white or black flash (or a one-frame insert) inside one shot, and it is listed under flash frames.

A **dissolve** has no single big change. The test: in a window of 0.1 to 0.6 s, the middle frame of a dissolve is very close to the average of the two end frames, and so are the quarter points. Pans and zooms fail this test because their middle frame is a moved picture, not a mix. A dissolve must also change the colours a fair amount and have no single frame that carries most of the change. A **fade** is a dissolve where one end is black. The cut time reported for a dissolve is its middle.

## Camera motion

For each shot, frames about 1/10 s apart are compared (3 frames at 30 fps). Comparing next-door frames would make slow moves add up wrong, because tiny shifts are measured with a slight pull toward zero.

Each frame pair gets one global motion: shift (pan and tilt), scale (zoom) and rotation (roll), about the frame centre.
- **numpy tracker:** phase correlation of the whole frame finds the big shift, then phase correlation of 32 px blocks on a grid finds the local shifts, and a weighted least-squares fit that drops outliers turns them into shift, scale and rotation. Flat blocks with no texture are ignored.
- **OpenCV tracker:** corners tracked with pyramidal Lucas-Kanade, kept only if tracking them back lands on the start point, then `estimateAffinePartial2D` with RANSAC. When a pair has too few good corners it falls back to the numpy tracker for that pair.

Frames inside a dissolve are left out of both shots.

Labels and thresholds:

| label | rule |
|-------|------|
| pan left/right | at least 3% of frame width per second, mostly in one direction, at least 4% in total |
| tilt up/down | the same with frame height |
| zoom in/out, push in/pull out | total scale change at least 4% and at least 2% per second. Push or pull when parallax is above 0.25 |
| roll | at least 3 degrees in total |
| handheld | shake at least 0.3% of frame width (0.12% gives "slight handheld") |
| whip / very fast move | most points lost and a big shift: the picture is too blurred to track |
| static | none of the above |

Directions are the camera's: if the picture slides left, the camera panned right.

**Shake** is how far the frame wanders from a smoothed copy of its own path (0.5 s smoothing), as RMS in % of frame width.

**Parallax** is how badly the points fit one flat motion, compared with how much they moved. A lens zoom or a 2D scale of a picture fits perfectly (near 0). A camera that physically moves through a 3D scene makes near things move more than far things, which the flat model cannot fit. People moving in frame also raise it, so read it next to the frames.

**Speed profile** compares the move speed in the first, middle and last third of the shot: steady, speeds up, slows down, or eases in and out. Copy this into the remake's easing curve.

The camera path in `breakdown.json` (`shots[].camera.path`) gives, every 1/10 s, the total pan (% width), tilt (% height) and zoom since the start of the shot. For a three.js remake, turn pan into yaw with `yaw = atan(pan% / 100 · 2 · tan(hfov / 2))` when the camera rotates in place.

### Measuring the lens and camera height from a frame

You need this when the remake is a 3D scene that has to line up with the reference.

1. Pick a sharp frame. On it, draw two lines along each of two real-world directions that meet at right angles (for example the left and right edges of a building, floor tiles, a table). Each pair meets at a vanishing point, v1 and v2, in pixels.
2. Let c be the frame centre. The focal length in pixels is `f = sqrt(-(v1 - c)·(v2 - c))` (dot product). This only works when both vanishing points are inside a few frame widths; if one direction is parallel to the frame, use a third direction or the known size of an object instead.
3. Vertical field of view: `vfov = 2·atan(H / (2f))`, with H the frame height in pixels. This is the number three.js `PerspectiveCamera` takes. As a 35 mm equivalent (full frame, 24 mm tall): `24 / (2·tan(vfov / 2))` mm.
4. The horizon is the line through v1 and v2. If it is on the centre line the camera is level. If it sits d pixels above centre the camera is pitched down by `atan(d / f)`. A tilted horizon is roll.
5. Camera height: the horizon passes through every object at the camera's own height. If it cuts a standing person at their eyes, the camera is at about 1.6 m. If it cuts them at half their height, the camera is at about 0.85 m. Without people, use a door (about 2 m) or a table (about 0.75 m).
6. A worked check: a 1080x1920 vertical frame, c = (540, 960), v1 = (-900, 1000), v2 = (2400, 980). (v1 - c)·(v2 - c) = (-1440)(1860) + (40)(20) = -2677600, so f = 1636 px, vfov = 2·atan(1920 / 3272) = 60.8 degrees, about a 21 mm lens equivalent. The horizon is about 30 px below centre, so the camera pitches up about 1 degree.

Telephoto shots (long lens, flat look) have vanishing points far outside the frame and a small field of view; a strong push in with little parallax that still looks flat is usually a long lens zoom, not a dolly.

## Audio

- **Onsets:** spectral flux (frame-to-frame rise in a log-magnitude spectrum, 46 ms frames, 11.6 ms hop), peaks above a moving average. Each onset is then snapped to the actual attack in the waveform (where a 1 ms energy envelope reaches half its local peak), so times are good to a few milliseconds.
- **Tempo and beats:** autocorrelation of the onset curve, weighted toward 120 BPM, then the dynamic-programming beat tracker from Ellis (2007). `pulse clarity` (0 to 1) says how regular the pulse is. Below 0.1 no tempo is reported, which is normal for speech.
- **Loudness:** ffmpeg `ebur128`: momentary (400 ms) and short-term (3 s) loudness every 0.1 s, integrated loudness and range. **Loudness jumps** are points where the momentary loudness rises 6 LU or more above the level of the previous second: drops, impacts, a voice coming in.
- **Cuts on the beat:** for each cut, the signed distance to the nearest onset and beat. Negative means the cut comes before the sound. The **chance rate** is the share of the whole timeline that lies within the tolerance of an onset, which is what random cuts would score. Editors often cut one or two frames before the hit; a steady negative typical offset shows that.
- **Transcript:** used if this Python can import `mlx_whisper`, `faster_whisper` or `whisper` (in that order). `WHISPER_MODEL` picks the model. Words are placed into the shots they are spoken in.

## On-screen text

Only when `tesseract` is on the PATH (or `$TESSERACT` is set). Frames are read 4 times a second at 1280 px, words under 60% confidence are dropped, and readings that match (75% similar) are merged into spans with a start and end time. Text that types on keeps its longest reading. Stylised captions with outlines or busy backgrounds are often missed; check the frames.

## compare.py

| option | meaning |
|--------|---------|
| `-o FILE --mode side/stack/blend/diff` | comparison video. side is left/right at the same height, stack is top/bottom at the same width, blend is a 50/50 overlay, diff shows brightness difference (black where they match) |
| `--stills FILE` | an image of frame pairs. Times come from `--at`, or from the middle of each shot with `--breakdown`, or 8 even times |
| `--ref-start`, `--remake-start` | seconds to skip at the start of each clip |
| `--duration` | default: the rest of the reference |
| `--fps` | both clips are put on this rate (default: the reference's), so they cannot drift apart |
| `--size` | panel height (side) or width (stack) |
| `--audio ref/remake/none` | which sound to keep |
| `--loop-remake` | repeat a shorter remake instead of holding its last frame |

Each panel has its label and its own clock burned in, so a frozen or late remake shows at a glance. The output is always re-encoded with libx264. Never loop or join clips with `-stream_loop` plus `-c copy`: copied streams keep their old timestamps and the join breaks.

## Tested

A known-answer test clip made with ffmpeg: 1280x720, 30 fps, 12.5 s, with a click track at 120 BPM (a click every 0.5 s). Shots: static 2 s, pan (picture moving 400 px/s), zoom in 1.0 to 1.5, tilt (300 px/s), handheld jitter, static, then a 0.4 s dissolve into a zoom out (1.6 to 1.3).

| check | truth | measured |
|-------|-------|----------|
| hard cuts | 2.000, 4.000, 6.000, 7.267, 9.033 | the same, to the frame |
| dissolve | 10.3 to 10.7, middle 10.5 | middle 10.533, 12 frames long |
| pan | right, 31.25% width/s | pan right 31.3% width/s, steady |
| zoom in | x1.50 | x1.467 (numpy), x1.474 (OpenCV), parallax 0.05 |
| tilt | down, 41.7% height/s | tilt down 40.9 to 41.3% height/s |
| handheld | wobble of about 15 px at 1280 wide, 3 to 9 Hz | handheld, shake 0.72%, direction flips on most samples |
| zoom out (measured part) | x0.844 | x0.866 (numpy), x0.853 (OpenCV) |
| tempo | 120 BPM | 120.1 BPM, onsets at exactly 0.5 s steps |
| cuts on onsets | 5 of 6 (the 7.267 cut is 233 ms early on purpose) | 83.3%, chance 19.2%, offset of the miss -233 ms |

Both trackers labelled every shot correctly. The test clip takes about 5 to 20 s on a laptop.

Real footage, read-only:
- a 40 s phone vlog excerpt (854x480, music and speech): 12 shots, every one a distinct shot on the contact sheet, no false cuts; handheld moves labelled; no steady tempo (pulse clarity 0.09), and only 18% of cuts on onsets against a 41% chance rate, which correctly says this edit is not cut to the music. About 5 s.
- a 21 s rendered perspective-illusion loop (one continuous camera move, repeating every 7 s): no false cuts despite large, fast changes of view. The camera phases came out the same in the second and third loop.

Shake is measured against a local quadratic fit of the camera path (Savitzky-Golay, 0.5 s), so smooth speed-ups are not counted as shake. "Handheld" also needs the motion to change direction on at least 30% of samples; a jerky but deliberate move (common in animation) is called "jerky moves" instead.

## Limits

- Cut detection works on whole-frame change. A cut between two nearly identical shots (same framing, a jump cut of a still subject) can be missed; a sudden full-frame light change (a lamp turning on) can be called a cut. The contact sheet shows both.
- Zooms and pushes at under 2% per second, and pans under 3% of the width per second, are called static on purpose.
- The camera model is one flat motion per frame. A shot that orbits a subject reads as a pan with high parallax. A shot where the subject fills the frame and moves reads as camera motion; look at the frames.
- Beat tracking assumes one steady tempo. Songs with tempo changes, or speech-only audio, give low pulse clarity; use the onsets then.
- Very fast whip pans blur too much to track; they are labelled as such, without numbers.
- Long shots are split into phases (one kind of move each, at least 0.6 s). Animated 3D moves that change in under half a second, like a flat-to-deep perspective snap, get folded into a coarse phase. Read the per-shot camera path in `breakdown.json` for these.
- A selfie or a subject close to the lens moving toward the camera reads as a zoom. The truck/arc versus pan split and the push versus zoom split rely on the parallax number, which was checked on the synthetic clip (near 0 for 2D moves) but has no synthetic 3D test.
- Long clips: frames are held in memory (see above). Analyse long videos in parts.
