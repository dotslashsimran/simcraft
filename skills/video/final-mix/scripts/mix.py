"""Final mix: voice + background music, music ducked under speech, one loudness pass at the very end.

Usage:
  python mix.py <video> --music <file> [--bed -12] [--duck 8] [--music-start 0] [--fade-in 1.5] [--fade-out 3]
                [--target -14] [--no-loop] [--out <path>] [--sample]
  <video>        the edited video (its first audio track is the voice; it should already be cleaned)
  --bed          music level while nobody talks, in LU relative to the voice (default -12)
  --duck         how much further the music drops while the voice is on, in dB (default 8)
  --music-start  where in the music file to start, seconds (skip a slow intro)
  --sample       only write a 15 s mix of the busiest talking moment, to judge the balance

Writes <name>_mix.mp4 (video stream copied untouched) and <name>_mix.md (what was measured and done).

Order matters: the voice is not re-normalised here (clean-audio already did that), the music is set relative to the
voice, the music is ducked with the voice as the key, the two are summed, and only then does the loudness pass run, so
the final file lands on target with the music in it. Normalising before adding music is why mixes end up 1-2 LU hot.
"""
import subprocess, sys
from pathlib import Path
import numpy as np
from media import ffmpeg, probe, loudness, normalize_chain, tc

arg = lambda k, d=None: sys.argv[sys.argv.index(k) + 1] if k in sys.argv else d
if len(sys.argv) < 2 or "--music" not in sys.argv:
    raise SystemExit(__doc__)
video = Path(sys.argv[1]).expanduser(); music = Path(arg("--music")).expanduser()
BED, DUCK = float(arg("--bed", -12)), float(arg("--duck", 8))
M0, FIN, FOUT = float(arg("--music-start", 0)), float(arg("--fade-in", 1.5)), float(arg("--fade-out", 3))
TARGET = float(arg("--target", -14))
FF = ffmpeg()
info = probe(video)
if not info["audio"]:
    raise SystemExit(f"{video.name} has no audio track: nothing to mix the music under.")
D = info["duration"]

# 1. levels: music set BED LU under the voice (both measured, so a loud master and a quiet one end up the same)
voice = loudness(video)
mus = loudness(music, f"atrim=start={M0}")
mgain = voice["I"] + BED - mus["I"]

# 2. ducking: sidechain compressor keyed by the voice. Threshold sits ~15 dB under the voice's level, so every word
#    pulls the music down; ratio is set so a word at normal level gives about DUCK dB of reduction.
key_thr = 10 ** ((voice["I"] - 15) / 20)
ratio = max(1.5, min(20.0, 15 / max(1.0, 15 - DUCK)))
loop = [] if "--no-loop" in sys.argv else ["-stream_loop", "-1"]
music_chain = (f"[1:a]atrim=start={M0},asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo,"
               f"volume={mgain:.2f}dB,atrim=0:{D:.3f},afade=t=in:d={FIN},afade=t=out:st={max(0, D - FOUT):.3f}:d={FOUT}[m]")
voice_chain = "[0:a]aresample=48000,aformat=channel_layouts=stereo,asplit=2[v][key]"
duck = (f"[m][key]sidechaincompress=threshold={key_thr:.5f}:ratio={ratio:.2f}:attack=15:release=350:knee=4:"
        f"makeup=1:link=maximum:detection=rms,asplit=2[md][stem]")
mixf = "[v][md]amix=inputs=2:duration=first:normalize=0[mix]"
graph = ";".join([voice_chain, music_chain, duck, mixf])

out = Path(arg("--out", video.with_name(f"{video.stem}_mix.mp4"))).expanduser()
tmp = out.with_suffix(".premix.wav")
stem = out.with_suffix(".musicstem.wav")  # the ducked music alone, to measure the balance against the voice


def render_mix(start=None, dur=None):
    cut = ["-ss", f"{start:.3f}", "-t", f"{dur:.3f}"] if start is not None else []
    subprocess.run([FF, "-v", "error", "-y", "-i", str(video), *loop, "-i", str(music), "-filter_complex", graph,
                    "-map", "[mix]", *cut, "-c:a", "pcm_s24le", str(tmp),
                    "-map", "[stem]", *cut, "-c:a", "pcm_s24le", str(stem)], check=True)


def busiest(win=15.0):
    """Start of the window with the most speech, from the voice track's loudness."""
    r = subprocess.run([FF, "-v", "error", "-i", str(video), "-map", "0:a:0", "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
                       capture_output=True, check=True).stdout
    x = np.frombuffer(r, np.int16).astype(np.float32) / 32768
    fr = x[: len(x) // 80 * 80].reshape(-1, 80)  # 10 ms
    on = (20 * np.log10(np.sqrt((fr ** 2).mean(1)) + 1e-9) > voice["I"] - 12).astype(float)
    n = int(win * 100)
    return 0.0 if len(on) <= n else float(np.argmax(np.convolve(on, np.ones(n), "valid"))) / 100


def balance():
    """Voice over music while talking (energy average of 10 ms frames where the voice is on), and music level in pauses.
    Both measured before the final gain, which applies to both equally, so the gap is what the listener hears."""
    def pcm(p):
        # approximate K-weighting (the curve LUFS uses), so this gap is on the same scale as --bed
        r = subprocess.run([FF, "-v", "error", "-i", str(p), "-map", "0:a:0", "-af", "highpass=f=60:p=2,highshelf=f=1500:g=4",
                            "-ac", "1", "-ar", "16000", "-f", "s16le", "-"], capture_output=True, check=True).stdout
        return np.frombuffer(r, np.int16).astype(np.float32) / 32768
    v, mu = pcm(video), pcm(stem)
    n = min(len(v), len(mu)) // 160 * 160
    db = lambda a: 20 * np.log10(np.sqrt((a[:n].reshape(-1, 160) ** 2).mean(1)) + 1e-9)
    vd, md = db(v), db(mu)
    on = vd > voice["I"] - 12
    pw = lambda d: float(10 * np.log10(np.mean(10 ** (d / 10))))  # energy average, like LUFS (a median ignores the hits)
    talk_gap = pw(vd[on]) - pw(md[on]) if on.any() else float("nan")
    pause_gap = pw(vd[on]) - pw(md[~on]) if on.any() and (~on).any() else float("nan")
    return talk_gap, pause_gap


lines = [f"# Final mix: {video.name}", "",
         f"- voice {voice['I']:.1f} LUFS; music {mus['I']:.1f} LUFS -> set {BED:+.0f} LU under the voice ({mgain:+.1f} dB)",
         f"- ducking: about {DUCK:.0f} dB more under speech (sidechain, threshold {voice['I'] - 15:.0f} dBFS, ratio {ratio:.1f}:1, "
         f"attack 15 ms, release 350 ms)",
         f"- music fades in over {FIN:g} s and out over the last {FOUT:g} s" + ("" if loop else "; music loops if shorter than the video")]
try:
    if "--sample" in sys.argv:
        t0 = busiest()
        render_mix(t0, min(15.0, D))
        chain, m = normalize_chain(tmp, [], TARGET)
        smp = out.with_name(f"{video.stem}_mix_sample.wav")
        subprocess.run([FF, "-v", "error", "-y", "-i", str(tmp), "-af", ",".join(chain), "-c:a", "pcm_s16le", str(smp)], check=True)
        print("\n".join(lines) + f"\nsample from {tc(t0)}: {smp}")
        sys.exit()
    render_mix()
    chain, m = normalize_chain(tmp, [], TARGET)
    n = round(D * 48000)
    subprocess.run([FF, "-v", "error", "-y", "-i", str(video), "-i", str(tmp), "-map", "0:v:0?", "-map", "1:a:0",
                    "-c:v", "copy", "-af", ",".join(chain + [f"apad=whole_len={n}", f"atrim=end_sample={n}"]),
                    "-c:a", "aac", "-b:a", "256k", "-movflags", "+faststart", str(out)], check=True)
    final = loudness(out)
    gap, pause = balance()
    ok = abs(final["I"] - TARGET) <= 0.5 and final["TP"] <= -0.9
    lines += [f"- final loudness **{final['I']:.1f} LUFS** (target {TARGET:g}), true peak {final['TP']:.1f} dBTP "
              f"{'OK' if ok else '<- OFF TARGET, check the music for a very loud hit'}",
              f"- while talking the voice sits about {gap:.0f} dB over the music (15-25 dB is the usual range; under 12 the "
              f"music fights the words)",
              f"- in pauses the music comes back up to {pause:.0f} dB under the voice's level", "", f"wrote {out}"]
    if gap == gap and gap < 12:
        lines.insert(-2, "- **music too loud under speech**: lower --bed or raise --duck")
    out.with_suffix(".md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
finally:
    tmp.unlink(missing_ok=True); stem.unlink(missing_ok=True)
