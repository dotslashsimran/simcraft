---
name: final-mix
description: Add background music under a voice video, ducked automatically while the person talks, with one loudness pass at the end so the file lands exactly on -14 LUFS (YouTube/Instagram/TikTok). Reports how far the voice sits above the music. Use when the user says "add music", "background track", "duck the music", "final mix", or "loudness".
---

# Final mix

Runs with plain `python3` (needs numpy, ffmpeg). The voice should already be cleaned (clean-audio).

## Steps
1. **Music:** the user picks the track (royalty-free sites, or their own). Ask where it should start if the intro is slow (`--music-start`).
2. **Judge the balance on a sample first:** `python3 scripts/mix.py <video> --music <track> --sample` writes 15s of the busiest talking moment. Play it for the user.
3. **Full mix:** `python3 scripts/mix.py <video> --music <track> [--bed -12] [--duck 8]` writes `<name>_mix.mp4` (video copied untouched) and `<name>_mix.md`.
4. **Read the report.** The voice should sit 15–25 dB over the music while talking. Under 12 dB, the music fights the words: lower `--bed` or raise `--duck`. The final loudness line is the *measured* value.

## How it works
- The music is set relative to the voice's measured loudness (`--bed` LU under it). A quiet master and a loud one end up the same.
- Ducking is a sidechain compressor keyed by the voice (attack 15ms, release 350ms). In a tight rough cut the pauses are shorter than the release, so the music stays down through the whole talk and only comes up in real gaps. That's intended.
- Loudness is done last, on the full mix, with gain plus a lookahead limiter, measured and corrected until within 0.3 LU. Plain two-pass `loudnorm` stops short on peaky audio (tested: -16.8 for a -14 target).
