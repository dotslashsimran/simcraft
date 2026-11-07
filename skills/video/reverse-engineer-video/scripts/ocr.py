"""On-screen text timing with tesseract, only if the `tesseract` command exists. Samples frames a few
times a second, reads words with confidence, and merges repeated readings into spans."""
import difflib, os, shutil, subprocess, tempfile
from pathlib import Path
from common import run_frames


def available():
    return shutil.which(os.environ.get("TESSERACT", "tesseract")) is not None


def _read(png):
    exe = os.environ.get("TESSERACT", "tesseract")
    r = subprocess.run([exe, str(png), "stdout", "--psm", "11", "tsv"], capture_output=True, text=True)
    if r.returncode != 0:
        return ""
    lines = {}
    for row in r.stdout.splitlines()[1:]:
        c = row.split("\t")
        if len(c) < 12 or not c[11].strip():
            continue
        try:
            conf = float(c[10])
        except ValueError:
            continue
        word = c[11].strip()
        if conf < 60 or len(word) < 2 and not word.isalnum():
            continue
        key = (c[2], c[3], c[4])  # block, paragraph, line
        lines.setdefault(key, []).append(word)
    text = " / ".join(" ".join(w) for _, w in sorted(lines.items()))
    return text if sum(ch.isalnum() for ch in text) >= 3 else ""


def text_spans(src, rate=4, long_side=1280):
    """[{start, end, text}] for text that stays on screen; [] if nothing found. None if no tesseract."""
    if not available():
        return None
    tmp = Path(tempfile.mkdtemp(prefix="ocr_"))
    try:
        scale = f"scale='if(gte(iw,ih),{long_side},-2)':'if(gte(iw,ih),-2,{long_side})'"
        run_frames(["-i", str(src), "-an", "-vf", f"fps={rate},{scale}"], [str(tmp / "f_%05d.png")])
        reads = [(i / rate, _read(p)) for i, p in enumerate(sorted(tmp.glob("f_*.png")))]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    spans = []
    for t, text in reads:
        if spans and text and difflib.SequenceMatcher(None, spans[-1]["text"], text).ratio() > 0.75 \
                and t - spans[-1]["end"] <= 1.5 / rate:
            spans[-1]["end"] = round(t + 1 / rate, 2)
            if len(text) > len(spans[-1]["text"]):
                spans[-1]["text"] = text  # keep the fullest reading (text that types on grows)
        elif text:
            spans.append({"start": round(t, 2), "end": round(t + 1 / rate, 2), "text": text})
    return spans
