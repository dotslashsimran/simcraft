"""Render cuts/edl.json to a video. Preview first, always.

Usage: python render.py <workspace> [--w 854] [--v 1] [--aspect 16:9|9:16|1:1] [--speed 1.0] [--raw-audio]
Uses audio/<clip>.clean.wav (from the clean-audio skill) when it exists; --raw-audio ignores it.
--w 854 is the 480p preview (fast). Use the source width (e.g. 1920, 3840) only after the user approves the preview.
Writes renders/v<N>_<w>.mp4.

How it stays in sync: each piece is rendered on its own with an exact frame count and uncompressed audio,
with 12-20 ms audio fades so cuts don't click, then all pieces are joined by decoding (not stream copy,
which drops frames at the joins and drifts the audio over a long video).
"""
import json, shutil, subprocess, sys
from pathlib import Path
from common import ffmpeg, info

ws = Path(sys.argv[1]).expanduser()
arg = lambda k, d: sys.argv[sys.argv.index(k) + 1] if k in sys.argv else d
W = int(arg("--w", 854)); V = arg("--v", "1")
edl = json.load(open(ws / "cuts/edl.json"))
speed = float(arg("--speed", edl.get("speed", 1.0)))
first = info(ws / "raw" / edl["keep"][0]["clip"])
if "--aspect" in sys.argv:
    a, b = map(int, arg("--aspect", "16:9").split(":")); H = round(W * b / a / 2) * 2
else:
    H = round(W * first["h"] / first["w"] / 2) * 2
FPS = round(first["fps"]) or 30
FF = ffmpeg(); hi = W > 854


def transfer(path):
    """HDR transfer of a clip: 'arib-std-b67' (HLG, iPhone default), 'smpte2084' (PQ/HDR10) or None (SDR)."""
    probe = shutil.which("ffprobe") or str(Path(FF).with_name("ffprobe"))
    r = subprocess.run([probe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=color_transfer",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    t = r.stdout.strip()
    return t if t in ("arib-std-b67", "smpte2084") else None


# HDR: a final render of all-HDR footage stays real 10-bit HEVC HDR (8-bit with HDR tags bands and plays wrong).
# Previews, and any mix of HDR and SDR clips, are tone-mapped to SDR so they look right everywhere.
trcs = {c: transfer(ws / "raw" / c) for c in {p["clip"] for p in edl["keep"]}}
hdr = set(trcs.values()) - {None}
mixed = bool(hdr) and None in trcs.values()
KEEP_HDR = hi and bool(hdr) and not mixed and len(hdr) == 1
OUT_TRC = next(iter(hdr)) if KEEP_HDR else None
if mixed or len(hdr) > 1:
    print("warning: clips mix HDR and SDR (or HLG and PQ): everything is tone-mapped to SDR Rec.709")
elif hdr and not hi:
    print(f"HDR ({'HLG' if 'arib-std-b67' in hdr else 'PQ'}) source: preview tone-mapped to SDR")


def tonemap(trc):
    return (f"setparams=color_primaries=bt2020:color_trc={trc}:colorspace=bt2020nc:range=tv,zscale=t=linear:npl=100,"
            f"format=gbrpf32le,zscale=p=bt709,tonemap=mobius:param=0.8:desat=0,zscale=t=bt709:m=bt709:r=tv,")


if KEEP_HDR:
    PIX = "yuv420p10le"
    xp = f"log-level=error:colorprim=bt2020:transfer={OUT_TRC}:colormatrix=bt2020nc:range=limited"
    enc = ["-c:v", "libx265", "-crf", "20", "-preset", "medium", "-pix_fmt", PIX, "-tag:v", "hvc1", "-x265-params", xp,
           "-color_primaries", "bt2020", "-color_trc", OUT_TRC, "-colorspace", "bt2020nc", "-color_range", "tv"]
else:
    PIX = "yuv420p"
    enc = ["-c:v", "libx264", "-crf", "18" if hi else "20", "-preset", "medium" if hi else "veryfast", "-pix_fmt", PIX]
    if hdr:
        enc += ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv"]

tmp = ws / "renders" / f".parts_{V}_{W}"
shutil.rmtree(tmp, ignore_errors=True); tmp.mkdir(parents=True)
parts = []
for k, p in enumerate(edl["keep"]):
    sp = float(p.get("speed", speed))
    n = max(1, round((p["out"] - p["in"]) / sp * FPS)); d = n / FPS
    # cleaned audio from the clean-audio skill, if it has been run on this workspace
    clean = ws / "audio" / f"{Path(p['clip']).stem}.clean.wav"
    clean = clean if clean.exists() and "--raw-audio" not in sys.argv else None
    # fit into the frame; if the shape differs, sit it on a blurred copy of itself (no black bars)
    tm = tonemap(trcs[p["clip"]]) if trcs[p["clip"]] and not KEEP_HDR else ""
    fc = (f"[0:v]{tm}setpts=(PTS-STARTPTS)/{sp},fps={FPS},setsar=1,split[a][b];"
          f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=20:2[bg];"
          f"[b]scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,format={PIX}[v];"
          f"[{1 if clean else 0}:a]aresample=48000,aformat=channel_layouts=stereo" + (f",atempo={sp}" if sp != 1 else "") +
          f",apad,atrim=0:{d:.4f},afade=t=in:d=0.012,afade=t=out:st={max(0, d - 0.02):.4f}:d=0.02[au]")
    out = tmp / f"{k:04d}.mov"
    subprocess.run([FF, "-v", "error", "-y", "-ss", f"{p['in']:.3f}", "-i", str(ws / "raw" / p["clip"]),
                    *(["-ss", f"{p['in']:.3f}", "-i", str(clean)] if clean else []),
                    "-filter_complex", fc, "-map", "[v]", "-map", "[au]", "-frames:v", str(n), *enc,
                    "-c:a", "pcm_s16le", str(out)], check=True)
    parts.append(out)
    print(f"\r{k + 1}/{len(edl['keep'])}", end="", flush=True)

def join(items, out, final_enc):
    ins = [x for q in items for x in ("-i", str(q))]
    fc = "".join(f"[{i}:v]setsar=1[v{i}];" for i in range(len(items))) + \
         "".join(f"[v{i}][{i}:a]" for i in range(len(items))) + f"concat=n={len(items)}:v=1:a=1[v][au]"
    subprocess.run([FF, "-v", "error", "-y", *ins, "-filter_complex", fc, "-map", "[v]", "-map", "[au]", "-r", str(FPS),
                    *final_enc, str(out)], check=True)


# a long video can have hundreds of pieces: join in batches (lossless-ish intermediates), then join the batches
BATCH = 150
if len(parts) > BATCH:
    mids = []
    for k in range(0, len(parts), BATCH):
        m = tmp / f"batch_{k // BATCH:03d}.mov"
        mid_enc = ([*enc[:enc.index("-crf")], "-crf", "12", *enc[enc.index("-crf") + 2:]] if KEEP_HDR else
                   ["-c:v", "libx264", "-crf", "12" if hi else "18", "-preset", "veryfast", "-pix_fmt", "yuv420p"])
        join(parts[k:k + BATCH], m, [*mid_enc, "-c:a", "pcm_s16le"])
        mids.append(m)
    parts = mids
final = ws / "renders" / f"v{V}_{W}.mp4"
join(parts, final, [*enc, "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"])
shutil.rmtree(tmp)
print(f"\nwrote {final}")
