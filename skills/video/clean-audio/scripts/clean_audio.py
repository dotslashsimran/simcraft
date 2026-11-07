"""Clean up voice audio: rumble, hum, background noise, mud, harsh S sounds, uneven volume, loudness.

Usage:
  python clean_audio.py <video or workspace> [--target -14] [--strength light|medium|strong] [--no-eq] [--sample]
    a video file  -> writes <name>_clean.<ext> next to it (video copied untouched, audio replaced)
    a workspace   -> cleans each raw/ clip's audio into audio/<clip>.clean.wav (rough-cut's render.py uses these)
  --sample        -> only write 10 s before/after wav files of the noisiest talking moment, for an A/B listen

Order (the standard voice chain): high-pass -> hum notch -> denoise -> EQ -> de-ess -> compress -> loudness.
Every step is decided from measurements of the actual audio, and the report says what was found and done.
"""
import json, re, subprocess, sys
from pathlib import Path
import numpy as np
from common import ffmpeg, clips, load_wav, Voice, HOP, tc

arg = lambda k, d: sys.argv[sys.argv.index(k) + 1] if k in sys.argv else d
src = Path(sys.argv[1]).expanduser()
TARGET = float(arg("--target", -14))            # LUFS. -14 for YouTube/Instagram/TikTok, -16 for podcasts
STRENGTH = arg("--strength", "medium")
# denoise levels, strongest first. Measured on real footage: one denoiser alone keeps words intact;
# stacking two (afftdn + anlmdn at s>=1) removed more noise but made Whisper lose 4 of 26 words.
LEVELS = ["strong", "medium", "light", "off"]
LEARNED_NR = {"strong": 28, "medium": 20, "light": 12}
FF = ffmpeg()


def wav_of(video, out, sr):
    subprocess.run([FF, "-v", "error", "-y", "-i", str(video), "-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(sr),
                    str(out)], check=True)


def analyse(video, tmp):
    """Noise floor, the cleanest pause to learn the noise from, hum, clipping."""
    w16 = tmp / "a16.wav"; wav_of(video, w16, 16000)
    pcm = load_wav(w16); w16.unlink()
    v = Voice(pcm)
    # pauses to learn the noise from: longest silent stretches, away from the edges of speech
    sil = v.silences(0, len(pcm) / 16000, 0.4)
    sil = sorted(((b - a, a + 0.08, b - 0.08) for a, b in sil if b - a > 0.4), reverse=True)
    learn = [(a, b) for _, a, b in sil[:1]]
    # hum: narrow peaks at 50/60 Hz and their harmonics in the silent parts
    hum = hum_db = None
    quiet = np.concatenate([pcm[int(a * 16000):int(b * 16000)] for _, a, b in sil[:8]] or [pcm[:16000]])
    if len(quiet) > 16000:
        spec = np.abs(np.fft.rfft(quiet[:16000 * 8] * np.hanning(min(len(quiet), 16000 * 8))))
        f = np.fft.rfftfreq(min(len(quiet), 16000 * 8), 1 / 16000)
        db = 20 * np.log10(spec + 1e-9)

        def peak(hz):
            i = np.argmin(abs(f - hz)); nb = (abs(f - hz) > 3) & (abs(f - hz) < 15)
            return db[max(0, i - 2):i + 3].max() - np.median(db[nb])
        for base in (50, 60):
            strength = np.mean([peak(base * k) for k in (1, 2, 3)])
            if strength > 10:
                hum = base; hum_db = round(float(strength), 1)
    # clipping, at full rate
    w48 = tmp / "a48.wav"; wav_of(video, w48, 48000)
    full = load_wav(w48); w48.unlink()
    clipped = float((np.abs(full) > 0.985).mean())
    speech_db = float(np.percentile(v.db[v.on], 50)) if v.on.any() else -30.0
    return {"duration": len(full) / 48000, "floor_db": round(v.floor, 1), "speech_db": round(speech_db, 1), "snr_db": round(speech_db - v.floor, 1),
            "learn": learn, "hum": hum, "hum_db": hum_db, "clipped_pct": round(100 * clipped, 3), "voice": v}


def chain(info, level, eq=True):
    f, notes = [], []
    if info["clipped_pct"] > 0.01:
        f.append("adeclip"); notes.append(f"repaired clipping ({info['clipped_pct']}% of samples)")
    f.append("highpass=f=80:p=2"); notes.append("cut rumble below 80 Hz")
    if info["hum"]:
        h = info["hum"]
        f += [f"bandreject=f={h * k}:width_type=q:w=30" for k in (1, 2, 3, 4)]
        notes.append(f"removed {h} Hz electrical hum and its harmonics (it stood {info['hum_db']} dB above the noise around it)")
    if level != "off":
        if info["learn"]:  # learn the actual room noise from a pause, like a noise print in iZotope/Audacity
            a, b = info["learn"][0]
            f.append(f"asendcmd=c='{a:.2f} afftdn@dn sample_noise start; {b:.2f} afftdn@dn sample_noise stop'")
            f.append(f"afftdn@dn=nr={LEARNED_NR[level]}:nf={max(-80, min(-20, info['floor_db']))}:gs=8")
            notes.append(f"removed background noise ({level}), learned from the pause at {tc(a)}")
        else:  # no pause long enough to learn from: non-local means, kept gentle
            f.append(f"anlmdn=s={ {'strong': 2, 'medium': 1, 'light': 0.5}[level] }")
            notes.append(f"removed background hiss ({level}, no pause to learn the noise from)")
    else:
        notes.append("no noise removal: every denoise level made the words less clear, so it was skipped")
    if eq:
        f += ["equalizer=f=250:t=q:w=1.2:g=-2", "equalizer=f=4500:t=q:w=1:g=1.5"]
        notes.append("light EQ: less boxy/muddy (-2 dB at 250 Hz), a bit clearer (+1.5 dB at 4.5 kHz)")
    f.append("deesser=i=0.4:m=0.5:f=0.5:s=o"); notes.append("softened harsh S sounds")
    f.append("acompressor=threshold=-21dB:ratio=3:attack=8:release=150:makeup=2:knee=4")
    notes.append("evened out loud and quiet words (3:1 compression)")
    # compression lifts the background along with quiet words (in testing it undid the denoising),
    # so a soft gate pulls pauses back down 12 dB. Not to silence: dead-silent gaps sound fake.
    thr = 10 ** ((info["floor_db"] + 6) / 20)
    f.append(f"agate=threshold={thr:.5f}:ratio=3:range=0.25:attack=5:release=200")
    notes.append("kept pauses quiet after compression (soft gate, a little room sound left on purpose)")
    return f, notes


_whisper = None
def clarity(video, af, t0, tmp):
    """How well Whisper understands 20 s of the result: (words, mean confidence). None if no Whisper installed."""
    global _whisper
    try:
        import mlx_whisper as _w
        run = lambda pcm: [w["probability"] for s in _w.transcribe(pcm, path_or_hf_repo="mlx-community/whisper-large-v3-turbo",
                           language=None, word_timestamps=True, verbose=None, condition_on_previous_text=False)["segments"] for w in s["words"]]
    except ImportError:
        try:
            from faster_whisper import WhisperModel
            _whisper = _whisper or WhisperModel("small", device="cpu", compute_type="int8")
            run = lambda pcm: [w.probability for s in _whisper.transcribe(pcm, word_timestamps=True)[0] for w in s.words or []]
        except ImportError:
            return None
    x = tmp / "clar.wav"
    subprocess.run([FF, "-v", "error", "-y", "-i", str(video), "-map", "0:a:0", "-af",
                    f"{af},atrim={t0}:{t0 + 20},asetpts=PTS-STARTPTS", "-ac", "1", "-ar", "16000", str(x)], check=True)
    ps = run(load_wav(x)); x.unlink()
    return len(ps), float(np.mean(ps)) if ps else 0.0


def pick_level(video, info, tmp, eq):
    """Strongest denoise that doesn't cost clarity: no lost words, confidence within 0.015 of the original."""
    v = info["voice"]; n = int(20 / HOP)
    t0 = max(0.0, float(np.argmax(np.convolve(v.on.astype(float), np.ones(min(n, len(v.on))), "valid"))) * HOP)
    base = clarity(video, "highpass=f=80:p=2", t0, tmp)
    start = LEVELS.index(STRENGTH)
    if base is None:
        return LEVELS[start], ["(no Whisper installed, so the clarity check was skipped)"]
    log = [f"clarity check on {tc(t0)}-{tc(t0 + 20)}: original {base[0]} words, confidence {base[1]:.3f}"]
    for level in LEVELS[start:]:
        if level == "off":
            return level, log
        f, _ = chain(info, level, eq)
        got = clarity(video, ",".join(f), t0, tmp)
        ok = got[0] >= base[0] and got[1] >= base[1] - 0.015
        log.append(f"  {level}: {got[0]} words, confidence {got[1]:.3f} -> {'kept' if ok else 'too strong, stepping down'}")
        if ok:
            return level, log
    return "off", log


def loudnorm_pass(inp, pre, extra_in=()):
    """Measure, then normalise exactly (two-pass loudnorm is accurate; one-pass pumps the volume)."""
    fc = ",".join(pre + [f"loudnorm=I={TARGET}:TP=-1.0:LRA=11:print_format=json"])
    err = subprocess.run([FF, "-hide_banner", *extra_in, "-i", str(inp), "-map", "0:a:0", "-af", fc, "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    m = json.loads(err[err.rindex("{"):err.rindex("}") + 1])
    return (f"loudnorm=I={TARGET}:TP=-1.0:LRA=11:measured_I={m['input_i']}:measured_TP={m['input_tp']}:"
            f"measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true"), m


def measure(path):
    err = subprocess.run([FF, "-hide_banner", "-i", str(path), "-map", "0:a:0", "-af",
                          "loudnorm=print_format=json", "-f", "null", "-"], capture_output=True, text=True).stderr
    m = json.loads(err[err.rindex("{"):err.rindex("}") + 1])
    return float(m["input_i"]), float(m["input_tp"])


def delay(video, af, info, tmp):
    """Some filters (the spectral denoiser) hold the sound back ~25 ms. Measure it by lining the result up with the
    original, so the cleaned audio can be shifted back into lip sync."""
    v = info["voice"]; n = int(20 / HOP)
    t0 = max(0.0, float(np.argmax(np.convolve(v.on.astype(float), np.ones(min(n, len(v.on))), "valid"))) * HOP)
    outs = []
    for tag, chain_ in (("a", "anull"), ("b", af)):
        x = tmp / f"dly_{tag}.wav"
        subprocess.run([FF, "-v", "error", "-y", "-i", str(video), "-map", "0:a:0", "-af",
                        f"{chain_},atrim=0:{t0 + 22}", "-ac", "1", "-ar", "16000", str(x)], check=True)
        outs.append(load_wav(x)); x.unlink()
    a, b = outs
    i0, i1 = int(t0 * 16000), int((t0 + 20) * 16000)
    lags = range(0, 1601, 2)  # 0-100 ms late
    best = max(lags, key=lambda k: float(np.dot(a[i0:i1], b[i0 + k:i1 + k])))
    return best / 16000


def clean(video, out_audio_or_video, mode, tmp):
    info = analyse(video, tmp)
    eq = "--no-eq" not in sys.argv
    level, log = pick_level(video, info, tmp, eq)
    f, notes = chain(info, level, eq)
    notes += log
    ln, m = loudnorm_pass(video, f)
    notes.append(f"loudness {float(m['input_i']):.1f} -> {TARGET:.0f} LUFS, peaks capped at -1 dBTP")
    lag = delay(video, ",".join(f + [ln]), info, tmp)
    sync = [f"atrim=start={lag:.4f}", "asetpts=PTS-STARTPTS"] if lag > 0.001 else []
    if sync:
        notes.append(f"shifted the cleaned audio {lag * 1000:.0f} ms earlier to keep perfect lip sync")
    ns = round(info["duration"] * 48000)  # exactly as long as the original, sample for sample
    af = ",".join(f + [ln] + sync + ["aresample=48000", f"apad=whole_len={ns}", f"atrim=end_sample={ns}"])
    if mode == "video":
        subprocess.run([FF, "-v", "error", "-y", "-i", str(video), "-map", "0:v:0", "-map", "0:a:0", "-c:v", "copy",
                        "-af", af, "-c:a", "aac", "-b:a", "256k", "-movflags", "+faststart", str(out_audio_or_video)],
                       check=True)
    else:
        subprocess.run([FF, "-v", "error", "-y", "-i", str(video), "-map", "0:a:0", "-vn", "-af", af,
                        "-c:a", "pcm_s24le", str(out_audio_or_video)], check=True)
    return info, notes, af


def label(gap):
    return "clean" if gap > 35 else "some noise" if gap > 25 else "noisy"


def report(name, info, notes, out):
    i, tp = measure(out)
    w16 = out.with_suffix(".check16.wav"); wav_of(out, w16, 16000)
    after = Voice(load_wav(w16)); w16.unlink()
    sp = float(np.percentile(after.db[after.on], 50)) if after.on.any() else 0.0
    gap = sp - after.floor
    return [f"## {name}",
            f"- speech vs background: {info['snr_db']} dB ({label(info['snr_db'])}) -> {gap:.1f} dB ({label(gap)})",
            f"- loudness {i:.1f} LUFS, true peak {tp:.1f} dBTP",
            *[f"- {n}" for n in notes],
            *(["- what's left is probably voices, music or traffic behind you. Filters like these can't separate that "
               "from your voice without damaging it. For that, an AI voice isolator works (Adobe Podcast Enhance, "
               "ElevenLabs Voice Isolator)."] if gap < 25 else []), ""]


def sample(video, tmp):
    """10 s before/after of the busiest talking moment, cut from the real full cleanup (so it sounds exactly like the result)."""
    full = tmp / "after_full.wav"
    info, notes, _ = clean(video, full, "wav", tmp)
    v = info["voice"]; n = int(10 / HOP)
    t0 = max(0.0, float(np.argmax(np.convolve(v.on.astype(float), np.ones(n), "valid"))) * HOP)
    base = video.with_suffix("")
    # "before" is volume-matched to the target, so the A/B compares quality, not loudness
    ln, _ = loudnorm_pass(video, [])
    subprocess.run([FF, "-v", "error", "-y", "-i", str(video), "-map", "0:a:0", "-af",
                    f"{ln},atrim={t0}:{t0 + 10},asetpts=PTS-STARTPTS", f"{base}_before.wav"], check=True)
    subprocess.run([FF, "-v", "error", "-y", "-i", str(full), "-af", f"atrim={t0}:{t0 + 10},asetpts=PTS-STARTPTS",
                    f"{base}_after.wav"], check=True)
    print("\n".join(report(video.name, info, notes, full)))
    print(f"A/B from {tc(t0)}: {base}_before.wav  {base}_after.wav")


tmp = (src if src.is_dir() else src.parent) / ".clean_tmp"; tmp.mkdir(exist_ok=True)
try:
    if "--sample" in sys.argv:
        sample(src, tmp)
    elif src.is_dir():
        lines = ["# Audio cleanup", ""]
        for clip in clips(src / "raw"):
            out = src / "audio" / f"{clip.stem}.clean.wav"; out.parent.mkdir(exist_ok=True)
            info, notes, _ = clean(clip, out, "wav", tmp)
            lines += report(clip.name, info, notes, out); print("cleaned", clip.name)
        (src / "audio" / "cleanup.md").write_text("\n".join(lines))
        print("\n".join(lines))
    else:
        out = src.with_name(f"{src.stem}_clean{src.suffix}")
        info, notes, _ = clean(src, out, "video", tmp)
        print("\n".join(report(src.name, info, notes, out)) + f"\nwrote {out}")
finally:
    for p in tmp.glob("*"): p.unlink()
    tmp.rmdir()
