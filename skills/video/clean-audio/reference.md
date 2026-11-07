# clean-audio: how it works and why

## The chain, in order

This is the standard voice chain: clean first, then shape, then control the level.

| # | Step | Filter | When |
|---|---|---|---|
| 1 | Repair clipping | `adeclip` | Only if more than 0.01% of samples are clipped |
| 2 | Cut rumble | `highpass` 80 Hz | Always. Below 80 Hz is traffic, AC, mic handling, never voice. |
| 3 | Remove hum | `bandreject` at 50/60 Hz and 3 harmonics | Only if hum is measured: a narrow peak 10 dB+ above the noise around it, in the pauses |
| 4 | Remove noise | `afftdn` with a noise print learned from the longest pause | Strength picked by the clarity check |
| 5 | EQ | -2 dB at 250 Hz (boxy, muddy), +1.5 dB at 4.5 kHz (clarity) | Unless `--no-eq` |
| 6 | De-ess | `deesser` | Always, gentle. Placed before compression, because compression makes S sounds louder. |
| 7 | Compress | `acompressor` 3:1, -21 dB threshold | Evens out loud and quiet words |
| 8 | Soft gate | `agate`, -12 dB, threshold 6 dB above the measured noise floor | Compression lifts the background with the quiet words, and this pulls the pauses back down |
| 9 | Loudness | Two-pass `loudnorm` to -14 LUFS, -1 dBTP | Two-pass is exact. One-pass pumps the volume. |
| 10 | Sync | Shift by the measured delay, pad to the exact original length | The spectral denoiser delays the sound about 25 ms |

## Why each choice (measured on real footage)

The test clips were 3 minutes of raw DJI talking-head footage and 90 seconds of a noisy phone recording.

**Denoiser choice.** The metric is the gap between the speech level and the background level, in dB. Bigger means cleaner.

| Setting | Gap | Words kept by Whisper | Confidence |
|---|---|---|---|
| raw | 22.3 | 26/26 | 0.966 |
| afftdn tracking noise on its own | 24.3 | 26 | 0.953 |
| afftdn learned from a pause, nr 20 | 32.4 | 26 | 0.958 |
| anlmdn s=2 | 38.4 | 26 | 0.928 |
| anlmdn s=5 | 40.2 | **22** | 0.888 |
| learned + anlmdn stacked | 49.1 | **22** | 0.865 |

The strongest settings measured cleanest but lost words. The learned noise print gives most of the benefit with no damage, so it's the default. anlmdn is used only when there is no pause to learn from, and only at low strength.

**The compressor undoes denoising.** Denoise alone gave a 24 dB gap. Adding the compressor brought it back to 23. The soft gate after the compressor fixes that, and the full chain reached 30 dB on the talking-head test clip with confidence 0.970, which is higher than the original 0.963.

**The clarity check.** Noise removal can sound fine in a short listen while making words mushy. Before applying, the script transcribes the busiest 20 seconds at each strength, strongest first. It keeps the first one that loses no words and stays within 0.015 confidence of the original. On the noisy clip, medium failed (0.873 vs 0.891) and light passed.

**Lip sync.** The cleaned audio measured 25 ms late. The script finds the delay for each video by lining up 20 seconds of the result against the original, then shifts the audio back. After the fix, the offset was 0.00 ms at 10 s, 75 s and 160 s, and the length matched sample for sample.

**What it can't do.** Background voices, music and traffic overlap the voice in frequency, so filters can't separate them without damaging speech. On the noisy test clip, the gap only went from 16.5 to 24.8 dB. When the gap after cleanup is still under 25 dB, the report says so and suggests an AI voice isolator, such as Adobe Podcast Enhance or ElevenLabs Voice Isolator. DeepFilterNet would be the open-source option, but it doesn't build on Python 3.12 (it needs Rust).

## Loudness targets

- YouTube, Instagram, TikTok: -14 LUFS integrated, -1 dBTP true peak. These platforms turn louder videos down, so going louder gains nothing.
- Podcasts (Spotify, Apple): -16 LUFS.
