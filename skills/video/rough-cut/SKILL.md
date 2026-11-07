---
name: rough-cut
description: Trim the fat from raw talking footage. Turns raw video into a tight first cut by removing dead air, ums and uhs, stutters, false starts and bad takes (keeping the last good take), then checks the result by listening to it again. Use when the user says "rough cut", "first cut", "trim the fat", "cut the pauses", "remove the ums", "cut my retakes", "make it snappy", or hands over raw footage to edit.
---

# Rough cut: trim the fat

Editors call this the **assembly** and the **rough cut**. Every usable line goes in, in order, and everything that doesn't earn its place comes out. It is not the finished video. It is the version where every second is the speaker saying the line well, so the later passes (pacing, colour, graphics, sound) work on good material.

The idea behind it: **edit the words, not the waveform.** Get a transcript where every word has a start and end time, decide what to keep by reading it, and the cuts follow from the timings. Descript and the text-based editing in Premiere and Resolve work this way. This skill adds two things they don't do: it snaps every cut to the real sound, and it checks the render by listening to it again.

## Setup (once)

```
sh ~/simcraft/skills/video/rough-cut/setup.sh
```
Creates `.venv` next to this file with Whisper (mlx-whisper on Apple Silicon, faster-whisper elsewhere). Needs ffmpeg: `ffmpeg` on PATH, or set `$FFMPEG` to its path. Run every script below with `~/simcraft/skills/video/rough-cut/.venv/bin/python`.

## Workspace

One folder per video. If the user hasn't made one, make `~/Movies/edits/YYYY-MM-DD-<slug>/` and **copy** (never move) the raw files into `raw/`. Scripts write `audio/`, `cuts/` and `renders/` next to it. Nothing in `raw/` is ever changed.

## The pass, in order

### 1. Transcribe
```
python scripts/transcribe.py <workspace> [--lang en]
```
Writes `audio/words.json` (every word: start, end, text, confidence) and `audio/transcript.txt` (one line per take). About 1 minute per 2 minutes of footage on an M-series Mac.

Pass `--lang en` when it's English (more accurate). Leave it off for Hindi/English mixed speech.

How it stays accurate (each one fixes a failure we hit on real footage):
- **Every take is transcribed on its own.** The audio is split at every pause of 0.5s or more. Transcribing a whole file at once, Whisper writes a line said five times only once, so the extra takes have no words and can't be cut.
- **Fillers are kept in the transcript.** Whisper drops "um" and "uh" by default. A hesitation-filled prompt brings them back so they can be cut.
- **Transcription loops are dropped.** Whisper sometimes repeats a phrase dozens of times over noise ("there you go there you go ..."). More than 7 words a second is impossible, so those lines are thrown out.

### 2. Find the cuts
```
python scripts/find_cuts.py <workspace> [--gap 0.35] [--keep-fillers]
```
Writes `cuts/edl.json` (pieces to keep, in order) and `cuts/cut_sheet.md` (every cut and why, plus low-confidence words to check).

| Cut | Rule |
|---|---|
| Dead air | Any pause longer than `--gap` (0.35s). Head and tail silence too. |
| Fillers | um, uh, erm, hmm, mm, ah. |
| Repeats | A word or chunk of up to 4 words said twice in a row ("so the so the"): keeps the second. |
| Retakes | The same line said again within 20s, matched by letters so mishearings still match ("Northwind" / "North wind", "1" / "one"): keeps the **last** take. |
| False starts | A line abandoned partway and then said in full ("A 300 billion." then "A 300 million dollar business."). |
| Same ending | A line rephrased at the start but ending the same way ("That's, it's Northwind's own founder" then "And that's not me saying it, it's Northwind's own founder"). |
| Ghost words | A word on its own with no voice under it. Whisper invents these in silence ("Thank you.", "hallelujah"). |

**Where each cut lands:**
- Every edge is snapped to where the voice really starts and stops, measured from loudness in 10ms steps with a threshold that adapts to the room. Whisper's word times can be off by 0.1–0.3s.
- An edge only moves later by up to 0.12s, because clipping a word is worse than keeping a breath.
- 0.06s of room is left before the voice and 0.10s after.
- When there is no real gap, such as an "uh" glued onto the next word, the cut goes at the quietest 10ms point between them.
- A pause hiding inside one long Whisper "word" is still found and cut.

### 3. Judgement pass (the real edit, never skip it)
Read `cuts/cut_sheet.md` next to `audio/transcript.txt` and fix `cuts/edl.json` by hand. A script can't judge these:

- **Which take is best.** The last take is kept by default, but the best one is the one with energy, a clean ending and no laugh breaking it. Sometimes the first half of one take plus the end of another is best.
- **Fillers that are part of the joke.** A deliberate "uhh" before a punchline stays.
- **"like", "you know", "I mean", "basically".** These are never cut automatically. Cut only the ones that are pure stalling. Cutting all of them makes the speaker sound unlike themselves.
- **Pauses that are beats.** A pause before a punchline, a reaction, or a look to camera stays. Not every silence is dead air.
- **Saying the same point twice in different words.** Cut whole sentences.
- **Non-speech moments**, like a song, a visual gag or a reaction shot. Only speech is kept, so add these pieces by hand.
- **Order.** Pieces follow file order. Move the strongest line to the top for a cold open if the user wants one.
- **The "check these words" list.** Low confidence often means mumbling. If the line is unclear, look for a cleaner take.

Each piece is `{"clip", "in", "out", "text"}`, and `"speed"` is optional. Keep cuts between words.

### 4. Pace (ask first)
The cut sheet reports words per second. Snappy talking-head content sits around 3.3–3.8. If the speaker is slower, suggest a speed of 1.05–1.2 and set `"speed"` in edl.json for the whole video, or per piece. Pitch stays the same. Never speed up music or reactions.

### 5. Preview render
```
python scripts/render.py <workspace> --w 854 --v 1 [--aspect 9:16]
```
A 480p preview goes to `renders/v1_854.mp4`. Bump `--v` for every round of notes so old versions stay.
- Each piece is rendered with an exact frame count and uncompressed audio, then all pieces are joined by decoding. Stream-copy joins drop frames at cuts and the audio drifts about 2s over a few minutes.
- Every piece gets a 12ms fade in and a 20ms fade out, so cuts don't click.
- Clips with a different shape sit on a blurred copy of themselves instead of black bars.

### 6. Verify (always, before showing the user)
```
python scripts/verify.py <workspace> --v 1 [--lang en]
```
It listens to the render again, piece by piece, compares it with the cut list, and writes `cuts/verify_v1.md`. The report has four sections:
- **Cuts that land while the voice is still going:** a sound test that doesn't depend on Whisper. This is the real clipped-word detector. Fix each one by moving that edge in edl.json.
- **Missing words:** expected but not heard.
- **Unexpected words:** a filler or a bit of a retake leaking through.
- **Mishearings:** the two transcripts disagree on wording. That's usually mumbling, not a cut problem.

On clean audio the target is 100% of words present, 0 cuts inside sound and 0 unexpected words. Noisy or mumbled audio gives some missing/unexpected pairs that are really mishearings. Trust the sound test over them.

Fix what it finds, render again, and verify again.

### 7. Show the user, then the final
- Tell the user: the length before and after, the number of cuts, the verify result, and any judgement calls you made (which take you kept, a pause you kept on purpose).
- Log their notes before making the next version.
- Only after they approve the preview: `python scripts/render.py <workspace> --w <source width> --v N`.

## Results on real footage

On 3 minutes of raw talking-head footage with heavy retakes (9 takes of one line, 4 of another), 2:55 of talking became 0:36. It kept the last clean take of every line, and verify found 100% of words present, 0 cuts inside sound and 0 leaks.

## The user's taste

- Snappy. Jump cuts on word gaps are good; constant alternating punch-in zooms are not.
- Keep swears, banter and the funny bits. Never clean up the personality.
- No whooshes, and no colour grade at this stage.
- When the user asks for "only the good bits, X minutes tops", cut whole moments, not just pauses.
