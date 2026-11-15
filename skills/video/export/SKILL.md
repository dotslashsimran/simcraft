---
name: export
description: Final export of an edited video in one encode: match the camera original's settings (codec, bit depth, frame rate, bitrate, HDR) or a platform preset (youtube, reels for Instagram/TikTok/Shorts), tone-map HDR to SDR when needed, burn captions and swap in the final mix in the same pass, then check the output. Use when the user says "export", "render the final", "same settings as the original", "for Instagram/TikTok/YouTube".
---

# Export

Runs with plain `python3` (needs ffmpeg with libx264, libx265, zscale, libass).

## Steps
1. `python3 scripts/export.py <edited video> --like <camera original> --preset match [--ass captions.ass] [--audio <name>_mix.mp4]`
2. Read `<out>.md`. It lists the target against what came out (codec, bit depth, transfer, size, fps, duration, loudness, true peak). Anything that doesn't match is in bold. Don't hand over a file with a bold line unexplained.

## Presets
| preset | video | when |
|---|---|---|
| match | same codec family and bit depth; HDR stays 10-bit HEVC HDR; bitrate at least the source's | archive, or wherever the original would go |
| youtube | H.264 SDR, or HEVC HDR if the source is HDR | YouTube (it supports HLG/PQ) |
| reels | H.264 8-bit SDR (HDR tone-mapped), max 1080 on the short side, 30 fps (60 kept) | Instagram, TikTok, Shorts |
| sdr | match, but HDR tone-mapped to Rec.709 | anything that shows HDR wrong |

## What it handles
- **Phone footage is variable frame rate**, which causes A/V drift in editors. The output is constant frame rate, snapped to the nearest standard rate.
- **HDR (iPhone HLG, HDR10 PQ)** is either kept as real 10-bit HEVC with correct tags, or tone-mapped with `mobius` (SDR-range tones pass through, highlights roll off). Never 8-bit with HDR tags.
- **Captions burned into HDR** render at HDR peak white and can be glaring. The report warns; `--preset sdr` avoids it.
- **One encode:** captions and audio go in here, not as extra renders.
