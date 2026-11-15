---
name: captions
description: Word-timed captions for a talking video, with the spoken word lit up and chosen words emphasised (colour + pop), short readable chunks, and safe placement for 9:16 or 16:9. Writes a styled .ass to burn in and a plain .srt to upload. Use when the user says "add captions", "subtitles", "word-by-word captions", "highlight these words", or after the rough cut is approved.
---

# Captions

Runs with plain `python3` (needs ffmpeg/ffprobe). Word timings come from rough-cut, so no second transcription is needed.

## Steps
1. **Get timings for the exact video being captioned.**
   - For a rough-cut render: `python3 scripts/captions.py <ws>/renders/vN_W.mp4 --workspace <ws>`. It uses verify's transcript of that render if it exists (most accurate), else maps `audio/words.json` through `cuts/edl.json`, speed included.
   - For any other video: `python3 scripts/captions.py <video>`. This transcribes it with rough-cut's Whisper env.
2. **Ask the user which words to emphasise.** Usually 1 every 5–10 seconds: the number, the brand, the punchline. Pass them with `--emphasis "pipeline,whole thing"`. The report flags any phrase that isn't in the transcript.
3. **Look before burning.** Run with `--stills` and open the PNGs (one per emphasised word). Check size, position, and that nothing covers the face. Use `--preview` for a quick 480p burned-in check.
4. **Burn at the final encode**, not as an extra render: `export.py ... --ass <name>.ass`. Upload the `.srt` too, so platforms get real closed captions.

## Style options
`--font` (with `--fontsdir` for a brand-kit font), `--size` (fraction of the short side: 0.075 for 9:16, 0.06 for 16:9), `--pos` (0.72 for 9:16, which keeps clear of the Reels/TikTok UI; 0.88 for 16:9), `--max-words` (3 for 9:16, 5 for 16:9), `--upper`, `--color`/`--active`/`--emph` as hex, `--no-active` for static chunks.

## Rules
- Fillers are never captioned, even if they were left in the cut.
- A caption never spans a pause over 0.3s or a sentence end. No one-word orphans at sentence ends.
- Captions stay up at least 0.5s and come down 0.25s after the last word.
- If a word is misheard, fix it in `<name>_words.json` and re-run with `--words <name>_words.json`.
