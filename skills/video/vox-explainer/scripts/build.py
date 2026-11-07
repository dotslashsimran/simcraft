"""Turn a scene file + a voiceover into a finished Vox-style explainer.

Usage: python build.py <scene.json> [--preview] [--stills 1,4.5,9] [--words words.json]
  scene.json   beats + voiceover path (format in SKILL.md)
  --preview    half-size render (fast) -> <scene>_preview.mp4. Without it: full size -> <scene>.mp4
  --stills     only render JPEG stills at those seconds, to check layouts
  --words      use an existing word-timing file (rough-cut's audio/words.json or verify words) instead of transcribing

Every beat starts on the word it is pinned to, every highlight sweeps on the word it marks, every list item lands on
its word. Timing comes from the voice, never from guesses. Then a motion check fails the build if any stretch
of the video sits still for more than 0.5 s.
"""
import json, os, re, subprocess, sys, shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROUGH = HERE.parents[1] / "rough-cut"
PY = ROUGH / ".venv/bin/python"
arg = lambda k, d=None: sys.argv[sys.argv.index(k) + 1] if k in sys.argv else d
scene_p = Path(sys.argv[1]).resolve()
scene = json.load(open(scene_p))
out_dir = scene_p.parent
norm = lambda w: re.sub(r"[^\w']", "", w.lower())
FF = os.environ.get("FFMPEG") or shutil.which("ffmpeg") or "ffmpeg"

# ---------- word timings ----------
audio = (scene_p.parent / scene["audio"]).resolve() if scene.get("audio") else None
words_p = Path(arg("--words")) if "--words" in sys.argv else out_dir / f".{scene_p.stem}_words.json"
if "--words" not in sys.argv and (not words_p.exists() or words_p.stat().st_mtime < audio.stat().st_mtime):
    print("transcribing the voiceover for word timings...")
    subprocess.run([str(PY), str(ROUGH / "scripts/transcribe.py"), "--file", str(audio), "--out", str(words_p)]
                   + (["--lang", scene["lang"]] if scene.get("lang") else []), check=True)
W = json.load(open(words_p))
if isinstance(W, dict): W = next(iter(W.values()))  # rough-cut's words.json is {clip: [...]}
toks = [(norm(w[2]), w[0] * 1000) for w in W if norm(w[2])]
NUM = {"1": "one", "2": "two", "3": "three", "4": "four", "5": "five"}


def find(phrase, after_ms, before_ms=1e12):
    """Time (ms) of the first word of `phrase`, at or after after_ms."""
    ph = [NUM.get(norm(x), norm(x)) for x in str(phrase).split() if norm(x)]
    for i, (t, ms) in enumerate(toks):
        if ms < after_ms - 1 or ms >= before_ms: continue
        if all(i + k < len(toks) and NUM.get(toks[i + k][0], toks[i + k][0]) == p for k, p in enumerate(ph)):
            return ms
    return None


def when(at, after_ms, label):
    if isinstance(at, (int, float)): return at * 1000
    ms = find(at, after_ms)
    if ms is None: raise SystemExit(f'beat {label}: the word(s) "{at}" are not in the voiceover after {after_ms / 1000:.1f}s. '
                                    f'Check spelling against the transcript, or use seconds.')
    return ms


# ---------- real screenshots: a beat with "url" and no "img" gets one captured ----------
for i, b in enumerate(scene["beats"]):
    if b.get("url") and not b.get("img"):
        img = out_dir / f"shot_{i + 1}.png"
        if not img.exists():
            print("screenshot", b["url"])
            subprocess.run(["node", str(HERE / "shot.mjs"), b["url"], str(img)], check=True)
        b["img"] = img.name

# ---------- resolve beats ----------
cursor = 0.0
beats = scene["beats"]
for i, b in enumerate(beats):
    b["_start"] = 0.0 if i == 0 and b.get("at") is None else when(b.get("at", 0), cursor, i + 1) - scene.get("lead_ms", 120)
    b["_start"] = max(b["_start"], cursor)
    cursor = b["_start"] + 1
end_ms = (W[-1][1] * 1000 + scene.get("tail_ms", 700)) if W else 10000
for i, b in enumerate(beats):
    nxt = beats[i + 1]["_start"] if i + 1 < len(beats) else end_ms
    for key, out in (("mark", "_marks_ms"), ("headmark", "_marks_ms"), ("submark", "_marks_ms")):
        if b.get(key):
            ms = find(b.get(key + "_at", b[key]).split()[0], b["_start"], nxt)
            b.setdefault(out, []).append(ms if ms is not None else b["_start"] + 600)
    if b["device"] == "list":
        b["_items_ms"] = [(find(a, b["_start"], nxt) or b["_start"] + 500 * k) for k, a in enumerate(b.get("items_at", b["items"]))]
    for tg in b.get("tags", []):
        if tg.get("at"): tg["_ms"] = find(tg["at"], b["_start"], nxt)
    dur = nxt - b["_start"]
    if dur > 6000:
        print(f"note: beat {i + 1} ({b['device']}) runs {dur / 1000:.1f}s. Vox pace is ~2.5-3.5s a beat; consider splitting it.")

# ---------- brand theme (from the brand-kit skill) ----------
theme = None
if scene.get("brand"):
    bdir = Path(scene["brand"]).expanduser() if "/" in scene["brand"] else Path.home() / ".local/share/simcraft/brands" / scene["brand"]
    if not (bdir / "brand.json").exists():
        raise SystemExit(f'brand "{scene["brand"]}" has no kit yet. Run the brand-kit skill first: it must exist before a brand video.')
    bk = json.load(open(bdir / "brand.json"))
    v, F = bk["vox"], bk["fonts"]
    files = [{"family": f["family"], "url": (bdir / f["file"]).as_uri(), "weight": f.get("weight"), "style": f.get("style")}
             for k in ("display", "body", "mono", "serif") for f in F.get(k, {}).get("files", [])]
    theme = {**v, "muted": bk["colors"].get("muted"), "radius": bk["shape"].get("card_radius"),
             "display": F["display"], "quote": F.get(v.get("quote_font", "body")) or F["body"], "mono": F["mono"], "font_files": files}
    print(f"brand: {bk['name']} ({bdir})")

vertical = scene.get("format", "16:9") == "9:16"
spec = {"w": 1080 if vertical else 1920, "h": 1920 if vertical else 1080, "fps": scene.get("fps", 30),
        "twos": scene.get("twos", True), "dur": end_ms, "beats": beats, **({"theme": theme} if theme else {})}
spec_p = out_dir / f".{scene_p.stem}_spec.json"
spec_p.write_text(json.dumps(spec))

# ---------- render ----------
node = ["node", str(HERE / "render.mjs"), str(spec_p)]
if "--stills" in sys.argv:
    subprocess.run(node + [str(out_dir / "x.mp4"), "--stills=" + arg("--stills")], check=True); sys.exit()
preview = "--preview" in sys.argv
silent = out_dir / f".{scene_p.stem}_silent.mp4"
subprocess.run(node + [str(silent)] + (["--scale", "0.5"] if preview else []), check=True)
final = out_dir / f"{scene_p.stem}{'_preview' if preview else ''}.mp4"
subprocess.run([FF, "-v", "error", "-y", "-i", str(silent), "-i", str(audio), "-map", "0:v", "-map", "1:a:0", "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", str(final)], check=True)
silent.unlink()
print("wrote", final)
r = subprocess.run([sys.executable, str(HERE / "motion_check.py"), str(final)])
sys.exit(r.returncode)
