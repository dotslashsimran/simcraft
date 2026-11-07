"""Verbatim, word-timed transcript of every clip in raw/ (or one file).

Usage: python transcribe.py <workspace> [--model <name>] [--lang en|hi|...]
       python transcribe.py --file some.mp4 --out words.json [--windows w.json]   (used by verify.py)
Writes audio/<clip>.wav, audio/words.json  {clip_name: [[start, end, word, prob], ...]}
and audio/transcript.txt (one line per take, with timecodes).

Accuracy choices, each one fixes a real failure:
- Every take is transcribed on its own (audio split at pauses of 0.5 s+). Transcribing the whole file at once,
  Whisper writes a line said five times only once, so the extra takes have no words and can't be found or cut.
- Whisper drops "um" and "uh" by default. A hesitation-filled prompt and condition_on_previous_text=False keep them.
- No VAD filter: it trims soft word edges and swallows fillers. Pauses are found by loudness instead (common.Voice).

Backend: mlx-whisper on Apple Silicon (GPU, about 2x faster, large-v3-turbo by default), otherwise faster-whisper on CPU
(medium by default).
"""
import json, sys
from pathlib import Path
from common import clips, extract_wav, load_wav, tc, Voice, HOP

arg = lambda k, d=None: sys.argv[sys.argv.index(k) + 1] if k in sys.argv else d
lang = arg("--lang")
PROMPT = "Umm, so, uh, I was like, you know, hmm, I mean, uh, let me, umm, okay so, uh."

try:
    import mlx_whisper
    BACKEND, model_name = "mlx", arg("--model", "mlx-community/whisper-large-v3-turbo")
except ImportError:
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise SystemExit("no Whisper installed. Run setup.sh in the rough-cut skill folder.")
    BACKEND, model_name = "faster", arg("--model", "medium")
    fw = WhisperModel(model_name, device="cpu", compute_type="int8")


def utterances(pcm, sr=16000, split=0.5, pad=0.2, longest=28.0):
    """Cut the audio at every pause of `split` seconds or more; one piece per take."""
    on = Voice(pcm, sr).on
    regs, start, last = [], None, None
    for i, v in enumerate(on):
        if v:
            if start is None: start = i
            elif (i - last) * HOP >= split:
                regs.append([start * HOP, (last + 1) * HOP]); start = i
            last = i
    if start is not None: regs.append([start * HOP, (last + 1) * HOP])
    regs = [r for r in regs if r[1] - r[0] >= 0.12]
    out = []
    for a, b in regs:  # whisper's window is 30 s; split anything longer evenly
        n = int((b - a) // longest) + 1
        out += [(a + k * (b - a) / n, a + (k + 1) * (b - a) / n) for k in range(n)]
    end = len(pcm) / sr
    return [(max(0, a - pad), min(end, b + pad)) for a, b in out]


def transcribe_chunk(chunk):
    """[(seg_start, seg_end, text, [(start, end, word, prob)])], times relative to the chunk."""
    if BACKEND == "mlx":
        r = mlx_whisper.transcribe(chunk, path_or_hf_repo=model_name, language=lang, word_timestamps=True,
                                   initial_prompt=PROMPT, condition_on_previous_text=False, verbose=None)
        return [(s["start"], s["end"], s["text"], [(w["start"], w["end"], w["word"], w.get("probability", 1.0))
                                                   for w in s.get("words", [])]) for s in r["segments"]]
    ss, _ = fw.transcribe(chunk, language=lang, word_timestamps=True, initial_prompt=PROMPT,
                          condition_on_previous_text=False, vad_filter=False, beam_size=5)
    return [(s.start, s.end, s.text, [(w.start, w.end, w.word, w.probability) for w in s.words or []]) for s in ss]


def run(wav, windows=None):
    pcm = load_wav(wav)
    words, lines = [], []
    for a, b in windows or utterances(pcm):
        for s0, s1, text, ws_ in transcribe_chunk(pcm[int(a * 16000):int(b * 16000)]):
            ws_ = [w for w in ws_ if a + w[0] < b]
            # nobody says 7+ words a second: that's whisper stuck in a loop ("there you go there you go ...")
            if len(ws_) >= 6 and len(ws_) / max(0.1, ws_[-1][1] - ws_[0][0]) > 7:
                lines.append(f"[{tc(a + s0)}] (dropped a transcription loop: {text.strip()[:60]}...)")
                continue
            for w0, w1, w, p in ws_:
                words.append([round(a + w0, 3), round(min(a + w1, b), 3), w.strip(), round(p, 3)])
            if ws_:
                lines.append(f"[{tc(a + s0)} - {tc(min(a + s1, b))}] {text.strip()}")
    return words, lines


if "--file" in sys.argv:
    src = Path(arg("--file")); wav = Path(arg("--out")).with_suffix(".16k.wav")
    extract_wav(src, wav)
    win = json.load(open(arg("--windows"))) if "--windows" in sys.argv else None  # exact pieces to hear, from verify.py
    words, _ = run(wav, win); wav.unlink()
    json.dump(words, open(arg("--out"), "w"))
    sys.exit()

ws = Path(sys.argv[1]).expanduser()
audio = ws / "audio"; audio.mkdir(exist_ok=True)
allw, lines = {}, []
print(f"whisper: {BACKEND} {model_name}")
for clip in clips(ws / "raw"):
    wav = audio / f"{clip.stem}.wav"
    extract_wav(clip, wav)
    words, ls = run(wav)
    allw[clip.name] = words
    lines += [f"\n## {clip.name}"] + ls
    print(clip.name, len(words), "words")
json.dump(allw, open(audio / "words.json", "w"))
(audio / "transcript.txt").write_text("\n".join(lines).strip() + "\n")
print("wrote", audio / "words.json", "and transcript.txt")
